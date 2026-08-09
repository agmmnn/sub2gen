import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from fastapi import WebSocket

from ..core.config import config
from ..core.logger import debug_logger
from ..workers.extension.captcha import ExtensionCaptchaJobs
from ..workers.extension.models import (
    DedicatedWorkerStats,
    ExtensionConnection,
    ExtensionStRefreshResult,
    NoExtensionGenerationWorkerError,
    normalize_extension_captcha_user_agent,
)
from ..workers.extension.jobs import ExtensionJobBroker
from ..workers.extension.generation import ExtensionGenerationJobs
from ..workers.extension.routing import ExtensionWorkerRouting
from ..workers.extension.registry import ExtensionConnectionRegistry
from ..workers.extension.refresh import ExtensionRefreshJobs
from ..workers.extension.uploads import GenerationUploadStore


class ExtensionCaptchaService:
    _instance: Optional["ExtensionCaptchaService"] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        self.db = db
        self.connection_registry = ExtensionConnectionRegistry()
        self.active_connections = self.connection_registry.connections
        self.job_broker = ExtensionJobBroker()
        # Compatibility aliases while request call sites move behind the broker.
        self.pending_requests = self.job_broker.pending_captcha
        self.pending_generation_requests = self.job_broker.pending_generation
        self._upstream_verdict_targets = self.job_broker.upstream_verdict_targets
        self._token_user_agents = self.job_broker.token_user_agents
        self._connection_changed = self.connection_registry.changed
        self._queue_waiters = self.connection_registry.waiters
        self._state_lock = self.job_broker.lock
        # Round-robin cursor per managed API key (see _queue_key). Lock-free counter:
        # concurrent picks may occasionally duplicate; modulo on read keeps indices valid.
        self._rr_cursor = self.connection_registry.managed_round_robin
        self.worker_routing = ExtensionWorkerRouting()
        # Compatibility aliases for in-flight callers during the strangler migration.
        self._dedicated_worker_stats = self.worker_routing.worker_stats
        self._dedicated_hybrid_rr = self.worker_routing.round_robin
        self._dedicated_stats_lock = self.worker_routing.lock
        self.generation_uploads = GenerationUploadStore()
        self.captcha_jobs = ExtensionCaptchaJobs(
            self.job_broker,
            self.worker_routing,
            log_info=debug_logger.log_info,
            log_error=debug_logger.log_error,
        )
        self.generation_jobs = ExtensionGenerationJobs(self.job_broker, self.generation_uploads)
        self.refresh_jobs = ExtensionRefreshJobs(self.job_broker)

    async def register_generation_upload_slot(
        self, *, req_id: str, max_body_bytes: int, ttl_seconds: int
    ) -> tuple[str, str]:
        return await self.generation_uploads.register(
            req_id=req_id,
            max_body_bytes=max_body_bytes,
            ttl_seconds=ttl_seconds,
        )

    async def ingest_generation_upload_body(self, upload_id: str, upload_secret: str, body: bytes) -> tuple[bool, str]:
        return await self.generation_uploads.ingest(upload_id, upload_secret, body)

    async def resolve_generation_upload_for_ws(
        self, *, req_id: str, upload_id: str, base_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self.generation_uploads.resolve(req_id=req_id, upload_id=upload_id, base_payload=base_payload)

    def _dedicated_stats(self, worker_session_id: str) -> DedicatedWorkerStats:
        return self.worker_routing.stats(worker_session_id)

    def _prune_timestamps(self, stamps: List[float], now: float, window: float) -> None:
        self.worker_routing.prune_timestamps(stamps, now, window)

    def _dedicated_worker_score(self, stats: DedicatedWorkerStats, now: float) -> float:
        return self.worker_routing.score(stats, now)

    def _pick_dedicated_connection_hybrid(
        self,
        pool: List[ExtensionConnection],
        preferred_token_id: int,
        *,
        exclude_worker_session_ids: Optional[Set[str]] = None,
        selection_meta_out: Optional[Dict[str, Any]] = None,
        eligible=None,
        hybrid_rr_suffix: str = "captcha",
    ) -> Optional[ExtensionConnection]:
        return self.worker_routing.pick_dedicated(
            pool,
            preferred_token_id,
            exclude_worker_session_ids=exclude_worker_session_ids,
            selection_meta_out=selection_meta_out,
            eligible=eligible or self._conn_eligible_for_captcha,
            hybrid_rr_suffix=hybrid_rr_suffix,
            log_info=debug_logger.log_info,
        )

    def _pick_captcha_worker_global_hybrid(
        self,
        pool: List[ExtensionConnection],
        *,
        exclude_worker_session_ids: Optional[Set[str]] = None,
        selection_meta_out: Optional[Dict[str, Any]] = None,
    ) -> Optional[ExtensionConnection]:
        return self.worker_routing.pick_global_captcha_worker(
            pool,
            exclude_worker_session_ids=exclude_worker_session_ids,
            selection_meta_out=selection_meta_out,
            eligible=self._conn_eligible_for_captcha,
            log_info=debug_logger.log_info,
        )

    def _dedicated_record_failure_locked(self, stats: DedicatedWorkerStats, now: float, *, is_timeout: bool) -> None:
        self.worker_routing.record_failure(stats, now, is_timeout=is_timeout)

    def _dedicated_record_success_locked(self, stats: DedicatedWorkerStats, latency_ms: float) -> None:
        self.worker_routing.record_success(stats, latency_ms)

    @classmethod
    async def get_instance(cls, db=None) -> "ExtensionCaptchaService":
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db=db)
        elif db is not None and cls._instance.db is None:
            cls._instance.db = db
        return cls._instance

    def _queue_key(self, managed_api_key_id: Optional[int]) -> str:
        return f"key:{managed_api_key_id}" if managed_api_key_id is not None else "unscoped"

    async def _notify_connection_change(self) -> None:
        await self.connection_registry.notify_changed()

    async def _load_persisted_binding(self, route_key: str) -> Tuple[Optional[int], str]:
        normalized = (route_key or "").strip()
        if not normalized or not self.db or not hasattr(self.db, "get_extension_worker_binding_for_route_key"):
            return None, "none"
        try:
            binding = await self.db.get_extension_worker_binding_for_route_key(normalized)
            if binding and binding.get("api_key_id") is not None:
                return int(binding["api_key_id"]), "persisted"
        except Exception as exc:
            debug_logger.log_warning(f"[Extension Captcha] Failed to load binding for route_key={normalized}: {exc}")
        return None, "none"

    async def _resolve_claimed_managed_key(self, raw_value: Any) -> Optional[int]:
        if raw_value in (None, "", "null"):
            return None
        try:
            api_key_id = int(raw_value)
        except (TypeError, ValueError):
            raise ValueError("managed_api_key_id must be an integer")
        if api_key_id <= 0:
            raise ValueError("managed_api_key_id must be positive")
        if not self.db or not hasattr(self.db, "get_api_key_detail"):
            raise ValueError("Managed API key lookup is not available")
        detail = await self.db.get_api_key_detail(api_key_id)
        if not detail:
            raise ValueError(f"Managed API key {api_key_id} does not exist")
        return api_key_id

    async def _apply_route_binding_to_connection(
        self,
        conn: ExtensionConnection,
        *,
        claimed_managed_api_key_id: Any = None,
    ) -> None:
        claimed_key: Optional[int] = None
        claimed = False
        if claimed_managed_api_key_id not in (None, "", "null"):
            claimed = True
            claimed_key = await self._resolve_claimed_managed_key(claimed_managed_api_key_id)
            if conn.route_key and self.db and hasattr(self.db, "upsert_extension_worker_binding"):
                await self.db.upsert_extension_worker_binding(conn.route_key, claimed_key)
            conn.managed_api_key_id = claimed_key
            conn.binding_source = "claimed"
            return

        persisted_key, source = await self._load_persisted_binding(conn.route_key)
        conn.managed_api_key_id = persisted_key
        conn.binding_source = source if source != "none" else ("claimed" if claimed else "none")

    async def connect(
        self,
        websocket: WebSocket,
        *,
        authenticated_managed_api_key_id: Optional[int] = None,
        authenticated_captcha_worker: Optional[Dict[str, Any]] = None,
        refresh_token_id: Optional[int] = None,
    ):
        await websocket.accept()
        conn = ExtensionConnection(
            websocket=websocket,
            instance_id=(websocket.query_params.get("instance_id") or "").strip(),
            route_key="",
            client_label=(websocket.query_params.get("client_label") or "").strip(),
        )
        await self.connection_registry.replace_instance(conn, disconnect=self.disconnect)
        if authenticated_captcha_worker:
            conn.captcha_worker_id = int(authenticated_captcha_worker.get("id"))
            conn.captcha_worker_key_label = str(authenticated_captcha_worker.get("label") or "").strip()
            conn.captcha_worker_key_prefix = str(authenticated_captcha_worker.get("key_prefix") or "").strip()
            conn.binding_source = "captcha_worker_key"
            conn.allow_captcha = True
            conn.allow_session_refresh = False
            conn.allow_generation = False
            try:
                if self.db and hasattr(self.db, "update_captcha_worker_key"):
                    await self.db.update_captcha_worker_key(
                        conn.captcha_worker_id,
                        last_instance_id=conn.instance_id or None,
                        mark_seen=True,
                        last_error="",
                    )
            except Exception as exc:
                debug_logger.log_warning(f"[Extension Captcha] Failed to persist captcha worker heartbeat: {exc}")
        elif authenticated_managed_api_key_id is not None:
            conn.managed_api_key_id = int(authenticated_managed_api_key_id)
            conn.binding_source = "authenticated"
        else:
            conn.binding_source = "none"
        if refresh_token_id is not None:
            conn.refresh_token_id = int(refresh_token_id)
            conn.binding_source = "refresh_token_id"
            conn.allow_captcha = False
            conn.allow_session_refresh = True
            conn.allow_generation = False
        await self.connection_registry.add(conn)
        debug_logger.log_info(
            f"[Extension Captcha] Client connected. Total: {len(self.active_connections)}, "
            f"worker_session_id={conn.worker_session_id}, "
            f"instance_id={conn.instance_id or '-'}, "
            f"label={conn.client_label or '-'}, "
            f"managed_api_key_id={conn.managed_api_key_id}, captcha_worker_id={conn.captcha_worker_id}, "
            f"source={conn.binding_source}"
        )

    def disconnect(self, websocket: WebSocket):
        conn = self.connection_registry.remove(websocket)
        if conn is not None:
            self.job_broker.disconnect(websocket)
            debug_logger.log_info(
                f"[Extension Captcha] Client disconnected. Total: {len(self.active_connections)}, "
                f"worker_session_id={conn.worker_session_id}, label={conn.client_label or '-'}"
            )
            if conn.managed_api_key_id is not None:
                self.connection_registry.clear_managed_cursor_if_unused(int(conn.managed_api_key_id))
            self._dedicated_worker_stats.pop(conn.worker_session_id, None)
            try:
                asyncio.get_running_loop().create_task(self._notify_connection_change())
            except Exception:
                pass

    def _find_connection(self, websocket: WebSocket) -> Optional[ExtensionConnection]:
        return self.connection_registry.find(websocket)

    @staticmethod
    def _conn_eligible_for_captcha(conn: ExtensionConnection) -> bool:
        return conn.refresh_token_id is None and bool(conn.allow_captcha)

    @staticmethod
    def _conn_eligible_for_session_refresh(conn: ExtensionConnection) -> bool:
        return conn.refresh_token_id is not None and bool(conn.allow_session_refresh)

    @staticmethod
    def _conn_eligible_for_generation(conn: ExtensionConnection) -> bool:
        return conn.refresh_token_id is None

    def has_generation_worker_for_token(self, token_id: Optional[int]) -> bool:
        if token_id is None:
            return False
        tid = int(token_id)
        return any(
            c.refresh_token_id is not None and int(c.refresh_token_id) == tid and self._conn_eligible_for_generation(c)
            for c in self.active_connections
        )

    def _connection_pool(self, *, exclude_dedicated_token_id: Optional[int] = None) -> list[ExtensionConnection]:
        """Active connections, optionally excluding dedicated worker(s) bound to a token."""
        if exclude_dedicated_token_id is None:
            return list(self.active_connections)
        tid = int(exclude_dedicated_token_id)
        out: list[ExtensionConnection] = []
        for conn in self.active_connections:
            did = conn.refresh_token_id
            if did is None:
                out.append(conn)
                continue
            if int(did) != tid:
                out.append(conn)
        return out

    def _finalize_managed_rr_cursor_after_pick(
        self,
        conn: ExtensionConnection,
        *,
        route_key: str,
        managed_api_key_id: Optional[int],
        preferred_token_id: Optional[int],
        exclude_dedicated_token_id: Optional[int],
    ) -> None:
        """Advance RR cursor after dispatch picks a connection (not while polling in wait loop)."""
        if managed_api_key_id is None:
            return
        if preferred_token_id is not None and conn.refresh_token_id is not None:
            if int(conn.refresh_token_id) == int(preferred_token_id):
                return
        pool = self._connection_pool(exclude_dedicated_token_id=exclude_dedicated_token_id)
        candidate_connections = [c for c in pool if c.managed_api_key_id == managed_api_key_id]
        if not candidate_connections:
            return
        sorted_candidates = sorted(candidate_connections, key=lambda c: c.worker_session_id)
        try:
            idx = sorted_candidates.index(conn)
        except ValueError:
            return
        queue_key = self._queue_key(managed_api_key_id)
        n = len(sorted_candidates)
        self._rr_cursor[queue_key] = (idx + 1) % n

    def _select_connection(
        self,
        route_key: str,
        managed_api_key_id: Optional[int],
        preferred_token_id: Optional[int] = None,
        *,
        exclude_dedicated_token_id: Optional[int] = None,
        exclude_worker_session_ids: Optional[Set[str]] = None,
        use_dedicated_hybrid: bool = True,
        selection_meta_out: Optional[Dict[str, Any]] = None,
        for_captcha: bool = False,
        for_session_refresh: bool = False,
        for_extension_generation: bool = False,
    ) -> Optional[ExtensionConnection]:
        pool = self._connection_pool(exclude_dedicated_token_id=exclude_dedicated_token_id)
        if for_captcha:
            pool = [c for c in pool if self._conn_eligible_for_captcha(c)]
            picked_captcha_worker = self._pick_captcha_worker_global_hybrid(
                pool,
                exclude_worker_session_ids=exclude_worker_session_ids,
                selection_meta_out=selection_meta_out,
            )
            if picked_captcha_worker is not None:
                return picked_captcha_worker
        if preferred_token_id is not None:
            if use_dedicated_hybrid:
                picked = self._pick_dedicated_connection_hybrid(
                    pool,
                    int(preferred_token_id),
                    exclude_worker_session_ids=exclude_worker_session_ids,
                    selection_meta_out=selection_meta_out,
                    eligible=self._conn_eligible_for_generation if for_extension_generation else None,
                    hybrid_rr_suffix="generation" if for_extension_generation else "captcha",
                )
                if picked is not None:
                    return picked
                if for_session_refresh:
                    return None
                if for_extension_generation:
                    return None
            else:
                for conn in pool:
                    if exclude_worker_session_ids and conn.worker_session_id in exclude_worker_session_ids:
                        continue
                    if conn.refresh_token_id is not None and conn.refresh_token_id == int(preferred_token_id):
                        if for_session_refresh and not self._conn_eligible_for_session_refresh(conn):
                            continue
                        return conn
                if for_session_refresh:
                    return None
        candidate_connections = pool
        if managed_api_key_id is not None:
            candidate_connections = [
                conn for conn in candidate_connections if conn.managed_api_key_id == managed_api_key_id
            ]
            if not candidate_connections:
                return None
            sorted_candidates = sorted(candidate_connections, key=lambda c: c.worker_session_id)
            queue_key = self._queue_key(managed_api_key_id)
            n = len(sorted_candidates)
            idx = self._rr_cursor.get(queue_key, 0) % n
            chosen = sorted_candidates[idx]
            if selection_meta_out is not None:
                selection_meta_out.clear()
                selection_meta_out["pool_size"] = n
                selection_meta_out["rr_idx"] = idx
            return chosen
        return None

    def _describe_routes(self) -> str:
        labels = []
        for conn in self.active_connections:
            if conn.captcha_worker_id is not None:
                labels.append(f"captcha-worker:{conn.captcha_worker_id}#{conn.binding_source}")
                continue
            label = conn.client_label or "worker"
            if conn.client_label:
                label = conn.client_label
            if conn.managed_api_key_id is not None:
                label = f"{label}@key{conn.managed_api_key_id}"
            if conn.binding_source:
                label = f"{label}#{conn.binding_source}"
            labels.append(label)
        return ", ".join(labels)

    def _describe_workers_verbose(self) -> str:
        if not self.active_connections:
            return "none"
        parts = []
        for conn in self.active_connections:
            if conn.captcha_worker_id is not None:
                parts.append(
                    f"captcha_worker_id={conn.captcha_worker_id}, label={conn.captcha_worker_key_label or '-'}, "
                    f"binding={conn.binding_source or 'captcha_worker_key'}"
                )
                continue
            label = conn.client_label or "-"
            managed = str(conn.managed_api_key_id) if conn.managed_api_key_id is not None else "unbound"
            source = conn.binding_source or "none"
            parts.append(f"label={label}, managed_key={managed}, binding={source}")
        return " | ".join(parts)

    def describe_routes(self) -> str:
        return self._describe_routes()

    async def _send_ack(self, websocket: WebSocket, payload: Dict[str, Any]):
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception:
            pass

    async def _resolve_route_key(self, token_id: Optional[int]) -> str:
        return ""

    def _has_connection_for_route_key(self, route_key: str, managed_api_key_id: Optional[int]) -> bool:
        return self._select_connection("", managed_api_key_id, for_captcha=True) is not None

    async def has_connection_for_managed_key(self, managed_api_key_id: Optional[int]) -> bool:
        if managed_api_key_id is None:
            return False
        if any(
            conn.captcha_worker_id is not None and self._conn_eligible_for_captcha(conn)
            for conn in self.active_connections
        ):
            return True
        return any(conn.managed_api_key_id == int(managed_api_key_id) for conn in self.active_connections)

    async def has_connection_for_dedicated_token(self, token_id: Optional[int]) -> bool:
        if token_id is None:
            return False
        target_token_id = int(token_id)
        return any(
            conn.refresh_token_id == target_token_id and self._conn_eligible_for_captcha(conn)
            for conn in self.active_connections
        )

    async def has_any_authenticated_connection_for_key(self, managed_api_key_id: Optional[int]) -> bool:
        if managed_api_key_id is None:
            return False
        return any(
            conn.managed_api_key_id == int(managed_api_key_id)
            and conn.binding_source in {"authenticated", "manual", "claimed"}
            for conn in self.active_connections
        )

    async def has_connection_for_token(
        self,
        token_id: Optional[int],
        managed_api_key_id: Optional[int] = None,
    ) -> tuple[bool, str]:
        route_key = ""
        if managed_api_key_id is not None:
            has_connection = await self.has_connection_for_managed_key(managed_api_key_id)
            if not has_connection:
                has_connection = await self.has_connection_for_dedicated_token(token_id)
            return has_connection, route_key
        return self._has_connection_for_route_key("", managed_api_key_id), route_key

    async def _wait_for_connection(
        self,
        *,
        route_key: str,
        managed_api_key_id: Optional[int],
        preferred_token_id: Optional[int] = None,
        timeout: float,
        exclude_dedicated_token_id: Optional[int] = None,
        exclude_worker_session_ids: Optional[Set[str]] = None,
        use_dedicated_hybrid: bool = True,
        selection_meta_out: Optional[Dict[str, Any]] = None,
        for_captcha: bool = False,
        for_extension_generation: bool = False,
    ) -> Optional[ExtensionConnection]:
        deadline = time.time() + max(0.0, float(timeout))
        queue_key = self._queue_key(managed_api_key_id)
        await self.connection_registry.begin_wait(queue_key)
        try:
            while True:
                conn = self._select_connection(
                    route_key,
                    managed_api_key_id,
                    preferred_token_id=preferred_token_id,
                    exclude_dedicated_token_id=exclude_dedicated_token_id,
                    exclude_worker_session_ids=exclude_worker_session_ids,
                    use_dedicated_hybrid=use_dedicated_hybrid,
                    selection_meta_out=selection_meta_out,
                    for_captcha=for_captcha,
                    for_extension_generation=for_extension_generation,
                )
                if conn is not None:
                    self._finalize_managed_rr_cursor_after_pick(
                        conn,
                        route_key=route_key,
                        managed_api_key_id=managed_api_key_id,
                        preferred_token_id=preferred_token_id,
                        exclude_dedicated_token_id=exclude_dedicated_token_id,
                    )
                    return conn
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                await self.connection_registry.wait_for_change(min(remaining, 1.5))
        finally:
            await self.connection_registry.end_wait(queue_key)

    async def handle_message(self, websocket: WebSocket, data: str):
        try:
            payload = json.loads(data)
            message_type = payload.get("type")

            if message_type == "client_shutdown":
                conn = self._find_connection(websocket)
                requested_session_id = str(payload.get("worker_session_id") or "").strip()
                if conn and (not requested_session_id or requested_session_id == conn.worker_session_id):
                    debug_logger.log_info(
                        f"[Extension Captcha] Client shutdown requested. "
                        f"worker_session_id={conn.worker_session_id}, instance_id={conn.instance_id or '-'}"
                    )
                    self.disconnect(websocket)
                    try:
                        await websocket.close(code=1000, reason="Client shutdown")
                    except Exception:
                        pass
                elif requested_session_id:
                    killed = await self.kill_worker(requested_session_id)
                    debug_logger.log_info(
                        f"[Extension Captcha] Client shutdown for detached session "
                        f"worker_session_id={requested_session_id}, killed={killed}"
                    )
                return

            if message_type == "register":
                conn = self._find_connection(websocket)
                if conn:
                    conn.client_label = (payload.get("client_label") or conn.client_label or "").strip()
                    conn.instance_id = (payload.get("instance_id") or conn.instance_id or "").strip()
                    register_error = None
                    if conn.captcha_worker_id is not None:
                        conn.route_key = ""
                        conn.client_label = conn.client_label or conn.captcha_worker_key_label
                        conn.managed_api_key_id = None
                        conn.binding_source = "captcha_worker_key"
                        conn.allow_captcha = True
                        conn.allow_session_refresh = False
                        conn.allow_generation = False
                        try:
                            if self.db and hasattr(self.db, "update_captcha_worker_key"):
                                await self.db.update_captcha_worker_key(
                                    conn.captcha_worker_id,
                                    last_instance_id=conn.instance_id or None,
                                    mark_seen=True,
                                    last_error="",
                                )
                        except Exception as exc:
                            register_error = str(exc)
                            debug_logger.log_warning(
                                f"[Extension Captcha] Failed to update captcha worker heartbeat: {register_error}"
                            )
                    elif conn.binding_source == "authenticated" and conn.managed_api_key_id is not None:
                        conn.route_key = ""
                    debug_logger.log_info(
                        f"[Extension Captcha] Client registered label={conn.client_label or '-'}, "
                        f"managed_api_key_id={conn.managed_api_key_id}, source={conn.binding_source}"
                    )
                    await self._send_ack(
                        websocket,
                        {
                            "type": "register_ack",
                            "worker_session_id": conn.worker_session_id,
                            "client_label": conn.client_label,
                            "instance_id": conn.instance_id,
                            "managed_api_key_id": conn.managed_api_key_id,
                            "binding_source": conn.binding_source,
                            "captcha_worker_id": conn.captcha_worker_id,
                            "captcha_worker_key_label": conn.captcha_worker_key_label,
                            "captcha_worker_key_prefix": conn.captcha_worker_key_prefix,
                            "refresh_token_id": conn.refresh_token_id,
                            "allow_captcha": conn.allow_captcha,
                            "allow_session_refresh": conn.allow_session_refresh,
                            "allow_generation": conn.allow_generation,
                            "status": "error" if register_error else "ok",
                            "error": register_error,
                        },
                    )
                    await self._notify_connection_change()
                return

            req_id = payload.get("req_id")
            if req_id:
                captcha_match, future = self.job_broker.match_response("captcha", req_id, websocket)
                if captcha_match == "wrong_owner":
                    debug_logger.log_warning(
                        f"[Extension Captcha] Ignoring captcha response from non-owner connection: {req_id}"
                    )
                    return
                if captcha_match == "matched" and future is not None:
                    if not future.done():
                        future.set_result(payload)
                    return
                generation_match, future = self.job_broker.match_response("generation", req_id, websocket)
                if generation_match == "wrong_owner":
                    debug_logger.log_warning(
                        f"[Extension Captcha] Ignoring generation response from non-owner connection: {req_id}"
                    )
                    return
                if generation_match == "matched" and future is not None:
                    if future.done():
                        return
                    if str(payload.get("status") or "") == "success" and payload.get("large_response_upload_id"):
                        upload_id = str(payload.get("large_response_upload_id") or "").strip()
                        merged = await self.resolve_generation_upload_for_ws(
                            req_id=req_id,
                            upload_id=upload_id,
                            base_payload=payload,
                        )
                        debug_logger.log_info(
                            f"[EXT-GEN] req_id={req_id} upstream_status={str(payload.get('response_status') or 0)} upload_status={str(merged.get('upload_status') or 'unknown')}"
                        )
                        future.set_result(merged)
                    else:
                        debug_logger.log_info(
                            f"[EXT-GEN] req_id={req_id} forwarded_without_upload upstream_status={str(payload.get('response_status') or 0)}"
                        )
                        future.set_result(payload)
                    return
        except Exception as e:
            debug_logger.log_error(f"[Extension Captcha] Error handling message: {e}")

    async def _generation_request_once(
        self,
        conn: ExtensionConnection,
        *,
        message_type: str,
        request_payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        return await self.generation_jobs.execute(
            conn,
            message_type=message_type,
            request_payload=request_payload,
            timeout=timeout,
            large_upload_enabled=bool(config.extension_generation_large_upload_enabled),
            upload_ttl_seconds=int(config.extension_generation_upload_ttl_seconds),
            upload_max_bytes=int(config.extension_generation_upload_max_bytes),
            upload_threshold_bytes=int(config.extension_generation_upload_threshold_bytes),
            force_upsample_upload=bool(config.extension_generation_upload_force_upsample_image),
        )

    async def submit_generation_via_extension(
        self,
        *,
        url: str,
        method: str = "POST",
        headers: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
        token_id: Optional[int] = None,
        managed_api_key_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        route_key = ""
        if managed_api_key_id is None and token_id is not None:
            if not self.has_generation_worker_for_token(token_id):
                raise NoExtensionGenerationWorkerError(
                    f"No extension worker with generation enabled for token_id={token_id}"
                )
        queue_wait_timeout = 20
        if self.db and hasattr(self.db, "get_captcha_config"):
            try:
                captcha_config = await self.db.get_captcha_config()
                queue_wait_timeout = int(getattr(captcha_config, "extension_queue_wait_timeout_seconds", 20) or 20)
            except Exception as exc:
                debug_logger.log_warning(
                    f"[Extension Captcha] Failed to load queue timeout for generation submit: {exc}"
                )
        queue_wait_timeout = max(1, min(120, queue_wait_timeout))
        selection_meta: Dict[str, Any] = {}
        conn = await self._wait_for_connection(
            route_key=route_key,
            managed_api_key_id=managed_api_key_id,
            preferred_token_id=token_id,
            timeout=queue_wait_timeout,
            exclude_dedicated_token_id=None,
            selection_meta_out=selection_meta,
            for_captcha=False,
            for_extension_generation=True,
        )
        if conn is None:
            raise RuntimeError("No extension worker available for generation submit")
        selection_source = "dedicated" if selection_meta.get("dedicated_hybrid") else "managed_key"
        debug_logger.log_info(
            f"[EXT-GEN] submit worker selected: source={selection_source}, worker_session_id={conn.worker_session_id}, "
            f"token_id={token_id}, managed_api_key_id={managed_api_key_id}"
        )
        payload = {
            "url": str(url or "").strip(),
            "method": str(method or "POST").strip().upper(),
            "headers": dict(headers or {}),
            "json_data": json_data if isinstance(json_data, dict) else {},
        }
        async with conn.dispatch_lock:
            return await self._generation_request_once(
                conn,
                message_type="submit_generation",
                request_payload=payload,
                timeout=timeout,
            )

    async def poll_generation_via_extension(
        self,
        *,
        url: str,
        method: str = "POST",
        headers: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: int = 45,
        token_id: Optional[int] = None,
        managed_api_key_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        route_key = ""
        if managed_api_key_id is None and token_id is not None:
            if not self.has_generation_worker_for_token(token_id):
                raise NoExtensionGenerationWorkerError(
                    f"No extension worker with generation enabled for token_id={token_id}"
                )
        queue_wait_timeout = 20
        if self.db and hasattr(self.db, "get_captcha_config"):
            try:
                captcha_config = await self.db.get_captcha_config()
                queue_wait_timeout = int(getattr(captcha_config, "extension_queue_wait_timeout_seconds", 20) or 20)
            except Exception as exc:
                debug_logger.log_warning(f"[Extension Captcha] Failed to load queue timeout for generation poll: {exc}")
        queue_wait_timeout = max(1, min(120, queue_wait_timeout))
        selection_meta: Dict[str, Any] = {}
        conn = await self._wait_for_connection(
            route_key=route_key,
            managed_api_key_id=managed_api_key_id,
            preferred_token_id=token_id,
            timeout=queue_wait_timeout,
            exclude_dedicated_token_id=None,
            selection_meta_out=selection_meta,
            for_captcha=False,
            for_extension_generation=True,
        )
        if conn is None:
            raise RuntimeError("No extension worker available for generation polling fallback")
        selection_source = "dedicated" if selection_meta.get("dedicated_hybrid") else "managed_key"
        debug_logger.log_info(
            f"[EXT-GEN] poll worker selected: source={selection_source}, worker_session_id={conn.worker_session_id}, "
            f"token_id={token_id}, managed_api_key_id={managed_api_key_id}"
        )
        payload = {
            "url": str(url or "").strip(),
            "method": str(method or "POST").strip().upper(),
            "headers": dict(headers or {}),
            "json_data": json_data if isinstance(json_data, dict) else {},
        }
        async with conn.dispatch_lock:
            return await self._generation_request_once(
                conn,
                message_type="poll_generation",
                request_payload=payload,
                timeout=timeout,
            )

    async def _extension_recaptcha_token_once(
        self,
        conn: ExtensionConnection,
        *,
        project_id: str,
        action: str,
        route_key: str,
        managed_api_key_id: Optional[int],
        timeout: int,
        selection_meta: Optional[Dict[str, Any]] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        return await self.captcha_jobs.execute(
            conn,
            project_id=project_id,
            action=action,
            managed_api_key_id=managed_api_key_id,
            timeout=timeout,
            selection_meta=selection_meta,
        )

    def consume_token_user_agent(self, req_id: Optional[str]) -> Optional[str]:
        """Consume validated solver metadata without changing get_token()'s tuple contract."""
        rid = str(req_id or "").strip()
        if not rid:
            return None
        return self.job_broker.consume_user_agent(rid)

    async def notify_upstream_verdict(
        self,
        req_id: Optional[str],
        *,
        accepted: bool,
        captcha_rejected: bool,
        detail: Optional[str] = None,
    ) -> None:
        """Tell the extension whether Flow accepted the reCAPTCHA token (same WebSocket as get_token)."""
        rid = (req_id or "").strip()
        if not rid:
            return
        websocket = await self.job_broker.take_upstream_verdict(rid)
        if websocket is None:
            return
        payload = {
            "type": "captcha_upstream_verdict",
            "req_id": rid,
            "accepted": bool(accepted),
            "captcha_rejected": bool(captcha_rejected),
            "detail": (detail or "")[:500],
        }
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception as exc:
            debug_logger.log_warning(f"[Extension Captcha] Failed to send upstream verdict: {exc}")

    async def abandon_upstream_verdict(self, req_id: Optional[str]) -> None:
        """Remove pending verdict routing without notifying (e.g. request failed before HTTP response)."""
        rid = (req_id or "").strip()
        if not rid:
            return
        await self.job_broker.abandon_upstream_verdict(rid)

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        timeout: int = 20,
        token_id: Optional[int] = None,
        managed_api_key_id: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        route_key = ""
        queue_wait_timeout = 20
        fallback_to_managed = False
        if self.db and hasattr(self.db, "get_captcha_config"):
            try:
                captcha_config = await self.db.get_captcha_config()
                queue_wait_timeout = int(getattr(captcha_config, "extension_queue_wait_timeout_seconds", 20) or 20)
                fallback_to_managed = bool(
                    getattr(captcha_config, "extension_fallback_to_managed_on_dedicated_failure", False)
                )
            except Exception as exc:
                debug_logger.log_warning(f"[Extension Captcha] Failed to load queue timeout: {exc}")
        queue_wait_timeout = max(1, min(120, queue_wait_timeout))
        sel_meta: Dict[str, Any] = {}
        conn = await self._wait_for_connection(
            route_key=route_key,
            managed_api_key_id=managed_api_key_id,
            preferred_token_id=token_id if token_id is not None else None,
            timeout=queue_wait_timeout,
            exclude_dedicated_token_id=None,
            selection_meta_out=sel_meta,
            for_captcha=True,
        )
        if conn is None:
            available = self._describe_routes() or "none"
            workers_verbose = self._describe_workers_verbose()
            qkey = self._queue_key(managed_api_key_id)
            waiting_count = self._queue_waiters.get(qkey, 0)
            raise RuntimeError(
                f"No Chrome Extension connection matched this request after waiting {queue_wait_timeout}s: "
                f"managed_api_key_id={managed_api_key_id}, token_id={token_id}, "
                f"queue={qkey}, queue_waiters={waiting_count}. "
                f"Available workers: {available}. Active workers: {workers_verbose}"
            )

        async with conn.dispatch_lock:
            token, ext_req_id = await self._extension_recaptcha_token_once(
                conn,
                project_id=project_id,
                action=action,
                route_key=route_key,
                managed_api_key_id=managed_api_key_id,
                timeout=timeout,
                selection_meta=sel_meta if sel_meta else None,
            )
        if token:
            return token, ext_req_id

        # One-shot retry on another general captcha worker before falling back to managed-key routing.
        if conn.captcha_worker_id is not None:
            sel_meta_cw_alt: Dict[str, Any] = {}
            conn_cw_alt = self._select_connection(
                route_key,
                managed_api_key_id,
                preferred_token_id=token_id if token_id is not None else None,
                exclude_dedicated_token_id=None,
                exclude_worker_session_ids={conn.worker_session_id},
                use_dedicated_hybrid=False,
                selection_meta_out=sel_meta_cw_alt,
                for_captcha=True,
            )
            if (
                conn_cw_alt is not None
                and conn_cw_alt.websocket is not conn.websocket
                and conn_cw_alt.captcha_worker_id is not None
            ):
                async with conn_cw_alt.dispatch_lock:
                    token_cw_alt, ext_req_id_cw_alt = await self._extension_recaptcha_token_once(
                        conn_cw_alt,
                        project_id=project_id,
                        action=action,
                        route_key=route_key,
                        managed_api_key_id=managed_api_key_id,
                        timeout=timeout,
                        selection_meta=sel_meta_cw_alt if sel_meta_cw_alt else None,
                    )
                if token_cw_alt:
                    return token_cw_alt, ext_req_id_cw_alt

        # One-shot retry on another dedicated worker for the same token (before managed fallback).
        if token_id is not None and conn.refresh_token_id is not None and int(conn.refresh_token_id) == int(token_id):
            sel_meta_alt: Dict[str, Any] = {}
            conn_alt = self._select_connection(
                route_key,
                managed_api_key_id,
                preferred_token_id=token_id,
                exclude_dedicated_token_id=None,
                exclude_worker_session_ids={conn.worker_session_id},
                use_dedicated_hybrid=True,
                selection_meta_out=sel_meta_alt,
                for_captcha=True,
            )
            if conn_alt is not None and conn_alt.websocket is not conn.websocket:
                async with conn_alt.dispatch_lock:
                    token_alt, ext_req_id_alt = await self._extension_recaptcha_token_once(
                        conn_alt,
                        project_id=project_id,
                        action=action,
                        route_key=route_key,
                        managed_api_key_id=managed_api_key_id,
                        timeout=timeout,
                        selection_meta=sel_meta_alt if sel_meta_alt else None,
                    )
                if token_alt:
                    return token_alt, ext_req_id_alt

        use_fallback = (
            fallback_to_managed
            and managed_api_key_id is not None
            and token_id is not None
            and conn.refresh_token_id is not None
            and int(conn.refresh_token_id) == int(token_id)
        )
        if not use_fallback:
            return None, None

        sel_meta2: Dict[str, Any] = {}
        conn2 = await self._wait_for_connection(
            route_key=route_key,
            managed_api_key_id=managed_api_key_id,
            preferred_token_id=None,
            timeout=queue_wait_timeout,
            exclude_dedicated_token_id=int(token_id),
            for_captcha=True,
            selection_meta_out=sel_meta2,
        )
        if conn2 is None or conn2.websocket is conn.websocket:
            return None, None
        debug_logger.log_info(
            "[Extension Captcha] Retrying reCAPTCHA on managed-key end-user extension "
            f"after dedicated worker failure (token_id={token_id}, managed_api_key_id={managed_api_key_id})"
        )
        async with conn2.dispatch_lock:
            return await self._extension_recaptcha_token_once(
                conn2,
                project_id=project_id,
                action=action,
                route_key=route_key,
                managed_api_key_id=managed_api_key_id,
                timeout=timeout,
                selection_meta=sel_meta2 if sel_meta2 else None,
            )

    async def _extension_refresh_st_once(
        self,
        conn: ExtensionConnection,
        *,
        token_id: int,
        timeout: int,
    ) -> Optional[str]:
        try:
            return await self.refresh_jobs.execute(conn, token_id=token_id, timeout=timeout)
        except Exception as exc:
            debug_logger.log_warning(f"[Extension Captcha] refresh_st failed for token_id={token_id}: {exc}")
            return None

    async def _classify_extension_st_refresh_no_connection(self, token_id: int) -> str:
        """Reason code when no eligible token-ID refresh worker exists for ST refresh."""
        tid = int(token_id)
        workers_for_token = [
            c for c in self.active_connections if c.refresh_token_id is not None and int(c.refresh_token_id) == tid
        ]
        if workers_for_token and not any(self._conn_eligible_for_session_refresh(c) for c in workers_for_token):
            return "extension_session_refresh_disabled"
        return "extension_worker_offline"

    async def refresh_session_token(
        self,
        *,
        token_id: int,
        timeout: int = 45,
    ) -> ExtensionStRefreshResult:
        """ST refresh is always sent to the extension bound for this token ID.

        Intentionally no fallback to other extension connections: session cookies must not be
        read or refreshed on a different browser profile than the account's refresh worker.
        """
        if token_id is None:
            return ExtensionStRefreshResult(failure_code="extension_no_worker_or_empty")
        conn = self._select_connection(
            route_key="",
            managed_api_key_id=None,
            preferred_token_id=token_id,
            use_dedicated_hybrid=False,
            for_session_refresh=True,
        )
        if conn is None:
            code = await self._classify_extension_st_refresh_no_connection(int(token_id))
            return ExtensionStRefreshResult(failure_code=code)
        async with conn.dispatch_lock:
            st = await self._extension_refresh_st_once(conn, token_id=token_id, timeout=timeout)
        if not st:
            return ExtensionStRefreshResult(failure_code="extension_no_worker_or_empty")
        return ExtensionStRefreshResult(session_token=st)

    async def report_flow_error(self, project_id: str, error_reason: str, error_message: str = ""):
        _ = project_id, error_message
        debug_logger.log_warning(f"[Extension Captcha] Flow error reported (ignoring): {error_reason}")

    async def list_active_workers(self) -> list[Dict[str, Any]]:
        workers: list[Dict[str, Any]] = []
        for conn in self.active_connections:
            workers.append(
                {
                    "worker_session_id": conn.worker_session_id,
                    "instance_id": conn.instance_id,
                    "client_label": conn.client_label,
                    "managed_api_key_id": conn.managed_api_key_id,
                    "binding_source": conn.binding_source,
                    "captcha_worker_id": conn.captcha_worker_id,
                    "captcha_worker_key_label": conn.captcha_worker_key_label,
                    "captcha_worker_key_prefix": conn.captcha_worker_key_prefix,
                    "refresh_token_id": conn.refresh_token_id,
                    "allow_captcha": conn.allow_captcha,
                    "allow_session_refresh": conn.allow_session_refresh,
                    "allow_generation": conn.allow_generation,
                    "connected_at": conn.connected_at,
                }
            )
        return workers

    async def kill_worker(self, worker_session_id: str) -> bool:
        target_id = (worker_session_id or "").strip()
        if not target_id:
            return False
        target: Optional[ExtensionConnection] = None
        for conn in self.active_connections:
            if conn.worker_session_id == target_id:
                target = conn
                break
        if target is None:
            return False
        try:
            await target.websocket.close(code=1000, reason="Worker terminated by admin")
        except Exception:
            pass
        self.disconnect(target.websocket)
        return True

    async def kill_captcha_worker_sessions_for_key(self, key_id: int) -> int:
        kid = int(key_id)
        killed = 0
        for conn in list(self.active_connections):
            if conn.captcha_worker_id is None or int(conn.captcha_worker_id) != kid:
                continue
            try:
                await conn.websocket.close(
                    code=1000,
                    reason="Captcha worker sessions terminated by admin",
                )
            except Exception:
                pass
            self.disconnect(conn.websocket)
            killed += 1
        return killed

    async def kill_managed_api_key_sessions(self, key_id: int) -> int:
        """Terminate extension sessions authenticated by a deleted managed API key."""
        kid = int(key_id)
        killed = 0
        for conn in list(self.active_connections):
            if conn.managed_api_key_id is None or int(conn.managed_api_key_id) != kid:
                continue
            try:
                await conn.websocket.close(
                    code=1000,
                    reason="Managed API key deleted by admin",
                )
            except Exception:
                pass
            self.disconnect(conn.websocket)
            killed += 1
        return killed

    async def bind_route_key(self, route_key: str, managed_api_key_id: int) -> None:
        normalized_route = (route_key or "").strip()
        if not normalized_route:
            raise ValueError("route_key is required")
        if not self.db or not hasattr(self.db, "upsert_extension_worker_binding"):
            raise ValueError("Binding persistence is unavailable")
        managed_api_key_id = await self._resolve_claimed_managed_key(managed_api_key_id)
        await self.db.upsert_extension_worker_binding(normalized_route, managed_api_key_id)
        for conn in self.active_connections:
            if conn.route_key == normalized_route:
                conn.managed_api_key_id = managed_api_key_id
                conn.binding_source = "manual"
        await self._notify_connection_change()

    async def unbind_route_key(self, route_key: str) -> None:
        normalized_route = (route_key or "").strip()
        if not normalized_route:
            raise ValueError("route_key is required")
        if not self.db or not hasattr(self.db, "delete_extension_worker_binding"):
            raise ValueError("Binding persistence is unavailable")
        await self.db.delete_extension_worker_binding(normalized_route)
        for conn in self.active_connections:
            if conn.route_key == normalized_route:
                conn.managed_api_key_id = None
                conn.binding_source = "none"
        await self._notify_connection_change()

    def get_queue_stats(self) -> Dict[str, int]:
        return dict(self._queue_waiters)
