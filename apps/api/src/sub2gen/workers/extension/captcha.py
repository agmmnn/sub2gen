"""Extension CAPTCHA request executor."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from .jobs import ExtensionJobBroker
from .models import ExtensionConnection, normalize_extension_captcha_user_agent
from .routing import ExtensionWorkerRouting


class ExtensionCaptchaJobs:
    def __init__(
        self,
        broker: ExtensionJobBroker,
        routing: ExtensionWorkerRouting,
        *,
        log_info: Callable[[str], None] | None = None,
        log_error: Callable[[str], None] | None = None,
    ) -> None:
        self.broker = broker
        self.routing = routing
        self.log_info = log_info or (lambda _message: None)
        self.log_error = log_error or (lambda _message: None)

    async def execute(
        self,
        connection: ExtensionConnection,
        *,
        project_id: str,
        action: str,
        managed_api_key_id: int | None,
        timeout: int,
        selection_meta: dict[str, Any] | None = None,
    ) -> tuple[str | None, str | None]:
        track_health = connection.refresh_token_id is not None or connection.captcha_worker_id is not None
        started_at = time.time()
        if track_health:
            async with self.routing.lock:
                self.routing.stats(connection.worker_session_id).inflight_count += 1

        request_id = f"req_{uuid.uuid4().hex}"
        future = self.broker.register("captcha", request_id, connection.websocket)
        request = {
            "type": "get_token",
            "req_id": request_id,
            "action": action,
            "project_id": project_id,
            "managed_api_key_id": managed_api_key_id,
        }
        try:
            self.log_info(
                "[Extension Captcha] Dispatching token request via "
                + ", ".join(self._dispatch_details(connection, project_id, action, managed_api_key_id, selection_meta))
            )
            await connection.websocket.send_text(json.dumps(request))
            result = await asyncio.wait_for(future, timeout=timeout)
            latency_ms = (time.time() - started_at) * 1000.0
            if result.get("status") != "success":
                self.log_error(f"[Extension Captcha] Error from extension: {result.get('error')}")
                await self._record_failure(connection, track_health=track_health, is_timeout=False)
                return None, None

            token = result.get("token")
            if not isinstance(token, str) or not token.strip():
                await self._record_failure(connection, track_health=track_health, is_timeout=False)
                return None, None

            user_agent = normalize_extension_captcha_user_agent(result.get("user_agent"))
            if user_agent is None:
                user_agent = normalize_extension_captcha_user_agent(result.get("userAgent"))
            if user_agent:
                self.broker.capture_user_agent(request_id, user_agent)
            if track_health:
                async with self.routing.lock:
                    self.routing.record_success(self.routing.stats(connection.worker_session_id), latency_ms)
            await self.broker.bind_upstream_verdict(request_id, connection.websocket)
            return token.strip(), request_id
        except TimeoutError:
            self.log_error(f"[Extension Captcha] Timeout waiting for token (req_id: {request_id})")
            await self._record_failure(connection, track_health=track_health, is_timeout=True)
            return None, None
        except Exception as error:
            self.log_error(f"[Extension Captcha] Communication error: {error}")
            await self._record_failure(connection, track_health=track_health, is_timeout=False)
            return None, None
        finally:
            if track_health:
                async with self.routing.lock:
                    stats = self.routing.stats(connection.worker_session_id)
                    stats.inflight_count = max(0, stats.inflight_count - 1)
            self.broker.remove("captcha", request_id)

    async def _record_failure(
        self,
        connection: ExtensionConnection,
        *,
        track_health: bool,
        is_timeout: bool,
    ) -> None:
        if not track_health:
            return
        async with self.routing.lock:
            self.routing.record_failure(
                self.routing.stats(connection.worker_session_id),
                time.time(),
                is_timeout=is_timeout,
            )

    @staticmethod
    def _dispatch_details(
        connection: ExtensionConnection,
        project_id: str,
        action: str,
        managed_api_key_id: int | None,
        selection_meta: dict[str, Any] | None,
    ) -> list[str]:
        details = [
            f"label={connection.client_label or '-'}",
            f"worker_session_id={connection.worker_session_id}",
            f"project_id={project_id}",
            f"action={action}",
            f"managed_api_key_id={managed_api_key_id}",
        ]
        if not selection_meta:
            return details
        if selection_meta.get("captcha_worker_pool"):
            details.extend(
                [
                    f"captcha_worker_id={connection.captcha_worker_id}",
                    f"captcha_worker_score={selection_meta.get('captcha_worker_score', '-')}",
                    f"captcha_worker_rr_idx={selection_meta.get('captcha_worker_rr_idx', '-')}",
                ]
            )
        if "pool_size" in selection_meta:
            details.append(f"pool_size={selection_meta['pool_size']}")
        if "rr_idx" in selection_meta:
            details.append(f"rr_idx={selection_meta['rr_idx']}")
        if selection_meta.get("dedicated_hybrid"):
            details.extend(
                [
                    f"dedicated_score={selection_meta.get('dedicated_score', '-')}",
                    f"dedicated_rr_idx={selection_meta.get('dedicated_rr_idx', '-')}",
                ]
            )
        return details
