"""Canonical protocol-v1 dispatch for Chrome-hosted Flow capabilities."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

CAPTCHA_CAPABILITY = "captcha.solve:google-flow"
REFRESH_CAPABILITY = "session.refresh:google-flow"
RELAY_CAPABILITY = "http.relay:google-flow"


class CanonicalBrowserWorkerService:
    def __init__(self, runtime, repositories) -> None:
        self.runtime = runtime
        self.repositories = repositories
        self._user_agents: dict[str, str] = {}

    async def _worker_id(
        self,
        capability: str,
        *,
        token_id: int | None,
        managed_api_key_id: int | None,
    ) -> str:
        account_ids: list[str] = []
        if token_id is not None:
            account = await self.repositories.provider_accounts.get_by_legacy(
                "google-flow", "tokens", str(token_id)
            )
            if account is not None and account.enabled:
                account_ids.append(account.id)
        elif managed_api_key_id is not None:
            account_ids.extend(await self.repositories.provider_accounts.list_ids_for_api_key(managed_api_key_id))

        workers = {worker.id: worker for worker in await self.repositories.workers.list_devices()}
        for account_id in account_ids:
            for binding in await self.repositories.credential_bindings.list_metadata(account_id):
                worker = workers.get(binding.worker_id or "")
                if (
                    binding.enabled
                    and worker is not None
                    and worker.enabled
                    and worker.revoked_at is None
                    and capability in worker.approved_capabilities
                    and self.runtime.is_connected(worker.id)
                ):
                    return worker.id
        if token_id is None and managed_api_key_id is None:
            for worker in workers.values():
                if (
                    worker.enabled
                    and worker.revoked_at is None
                    and capability in worker.approved_capabilities
                    and self.runtime.is_connected(worker.id)
                ):
                    return worker.id
        raise RuntimeError(f"No protocol-v1 worker is available for {capability}")

    async def _dispatch(
        self,
        capability: str,
        input_payload: dict[str, Any],
        *,
        timeout: int,
        token_id: int | None,
        managed_api_key_id: int | None,
    ) -> tuple[str, dict[str, Any]]:
        worker_id = await self._worker_id(
            capability,
            token_id=token_id,
            managed_api_key_id=managed_api_key_id,
        )
        request_id = f"worker_job_{uuid4().hex}"
        terminal = await self.runtime.dispatch(
            worker_id=worker_id,
            job_id=request_id,
            job_kind=capability,
            attempt=1,
            capability=capability,
            input=input_payload,
            timeout_seconds=float(timeout),
            artifact_content_types=("application/octet-stream",),
            artifact_max_bytes=1,
            upload_base_url="http://127.0.0.1",
        )
        if terminal.error is not None:
            raise RuntimeError(terminal.error.message)
        return request_id, dict(terminal.output or {})

    async def get_token(
        self,
        *,
        project_id: str,
        action: str,
        timeout: int,
        token_id: int | None,
        managed_api_key_id: int | None,
    ) -> tuple[str | None, str | None]:
        request_id, output = await self._dispatch(
            CAPTCHA_CAPABILITY,
            {"project_id": project_id, "action": action},
            timeout=timeout,
            token_id=token_id,
            managed_api_key_id=managed_api_key_id,
        )
        token = str(output.get("token") or "").strip()
        user_agent = str(output.get("user_agent") or "").strip()
        if user_agent:
            self._user_agents[request_id] = user_agent
        return (token or None), (request_id if token else None)

    def consume_user_agent(self, request_id: str) -> str | None:
        return self._user_agents.pop(request_id, None)

    async def refresh_session(self, *, token_id: int, timeout: int) -> str | None:
        _, output = await self._dispatch(
            REFRESH_CAPABILITY,
            {"token_id": token_id},
            timeout=timeout,
            token_id=token_id,
            managed_api_key_id=None,
        )
        return str(output.get("session_token") or "").strip() or None

    async def relay(
        self,
        *,
        url: str,
        method: str,
        headers: dict[str, Any],
        json_data: dict[str, Any],
        timeout: int,
        token_id: int | None,
        managed_api_key_id: int | None,
    ) -> dict[str, Any]:
        _, output = await self._dispatch(
            RELAY_CAPABILITY,
            {"url": url, "method": method, "headers": headers, "json_data": json_data, "timeout_ms": timeout * 1000},
            timeout=timeout,
            token_id=token_id,
            managed_api_key_id=managed_api_key_id,
        )
        return {"status": "success", **output}
