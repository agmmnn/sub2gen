"""Provider-neutral administration for accounts, workers, models, and jobs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...bootstrap.container import AppContainer
from ...bootstrap.dependencies import get_container

CONTROL_PLANE_PREFIX = "/api/admin/control-plane"


class PairingCodeRequest(BaseModel):
    ttl_seconds: int = Field(default=300, ge=60, le=1800)


class WorkerMutation(BaseModel):
    enabled: bool | None = None
    capabilities: list[str] | None = None


class ConfirmMutation(BaseModel):
    confirm: str


class AccountMutation(BaseModel):
    enabled: bool


def _public_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "billing_pool",
        "browser",
        "browser_version",
        "chrome_relay",
        "codex_oauth",
        "login_state",
        "platform",
        "provider_project",
        "session_state",
        "supported_tools",
        "version",
    }
    return {key: value for key, value in metadata.items() if key in allowed}


def _job_payload(job: Any) -> dict[str, Any]:
    execution = dict(job.resolved_execution or {})
    return {
        "id": job.id,
        "request_id": job.request_id,
        "job_kind": job.job_kind,
        "status": job.status.value,
        "requested_model": job.requested_model,
        "resolved_model": execution.get("resolved_model"),
        "provider": execution.get("provider_id"),
        "billing_pool": execution.get("billing_pool"),
        "account_id": job.provider_account_id or execution.get("provider_account_id"),
        "worker_id": job.worker_id or execution.get("worker_id"),
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "terminal_at": job.terminal_at,
    }


def build_control_plane_router(admin_dependency: Callable[..., Any]) -> APIRouter:
    router = APIRouter(
        prefix=CONTROL_PLANE_PREFIX,
        tags=["admin-control-plane"],
        dependencies=[Depends(admin_dependency)],
    )

    @router.get("/overview")
    async def overview(
        limit: int = Query(default=100, ge=1, le=500),
        container: AppContainer = Depends(get_container),
    ) -> dict[str, Any]:
        accounts = await container.repositories.provider_accounts.list()
        bindings = await container.repositories.credential_bindings.list_metadata()
        workers = await container.repositories.workers.list_devices()
        jobs = await container.repositories.generation_jobs.list_recent(limit=limit)
        audits = await container.repositories.operator_audit.list_recent(limit=50)
        bindings_by_account: dict[str, list[Any]] = defaultdict(list)
        for binding in bindings:
            bindings_by_account[binding.provider_account_id].append(binding)

        account_rows = []
        for account in accounts:
            account_bindings = bindings_by_account[account.id]
            errors = [binding.last_error for binding in account_bindings if binding.last_error]
            account_rows.append(
                {
                    "id": account.id,
                    "provider": account.provider_key,
                    "label": account.label,
                    "enabled": account.enabled,
                    "health": "paused" if not account.enabled else ("error" if errors else "ready"),
                    "credential_locations": sorted({binding.storage_kind.value for binding in account_bindings}),
                    "bindings": [
                        {
                            "id": binding.id,
                            "key": binding.binding_key,
                            "credential_type": binding.credential_type,
                            "storage_kind": binding.storage_kind.value,
                            "worker_id": binding.worker_id,
                            "enabled": binding.enabled,
                            "expires_at": binding.expires_at,
                            "last_validated_at": binding.last_validated_at,
                            "last_error": binding.last_error,
                        }
                        for binding in account_bindings
                    ],
                    "metadata": _public_metadata(account.metadata),
                    "updated_at": account.updated_at,
                }
            )

        worker_rows = [
            {
                "id": worker.id,
                "kind": worker.kind,
                "label": worker.label,
                "enabled": worker.enabled,
                "connected": container.worker_runtime.is_connected(worker.id),
                "status": (
                    "revoked"
                    if worker.revoked_at
                    else "paused"
                    if not worker.enabled
                    else "online"
                    if container.worker_runtime.is_connected(worker.id)
                    else "offline"
                ),
                "capabilities": list(worker.approved_capabilities),
                "credential_expires_at": worker.credential_expires_at,
                "revoked_at": worker.revoked_at,
                "last_seen_at": worker.last_seen_at,
                "metadata": _public_metadata(worker.metadata),
            }
            for worker in workers
        ]

        models = [
            {
                "id": model.model_id,
                "provider": model.provider_id,
                "resolved_model": model.resolved_model,
                "kind": model.kind.value,
                "billing_pool": model.billing_pool,
                "capability": model.capability,
                "credential_kinds": sorted(model.credential_kinds),
                "execution_location": model.execution_location,
                "aliases": list(model.aliases),
            }
            for model in container.model_registry.list()
        ]
        providers = []
        provider_ids = sorted({model["provider"] for model in models} | {account.provider_key for account in accounts})
        for provider_id in provider_ids:
            provider_accounts = [row for row in account_rows if row["provider"] == provider_id]
            provider_models = [row for row in models if row["provider"] == provider_id]
            providers.append(
                {
                    "id": provider_id,
                    "account_count": len(provider_accounts),
                    "enabled_accounts": sum(1 for row in provider_accounts if row["enabled"]),
                    "model_count": len(provider_models),
                    "execution_locations": sorted({row["execution_location"] for row in provider_models}),
                    "billing_pools": sorted({row["billing_pool"] for row in provider_models}),
                }
            )

        chatgpt_workers = [row for row in worker_rows if any("chatgpt" in cap for cap in row["capabilities"])]
        oauth_bindings = [binding for binding in bindings if binding.credential_type.lower() == "oauth"]
        diagnostics = {
            "chrome_relay": {
                "status": "ready" if any(row["connected"] for row in chatgpt_workers) else "offline",
                "connected_workers": sum(1 for row in chatgpt_workers if row["connected"]),
            },
            "login_state": [
                {"worker_id": row["id"], "state": row["metadata"].get("login_state", "unknown")}
                for row in chatgpt_workers
            ],
            "codex_oauth": {
                "configured": bool(oauth_bindings),
                "healthy": any(binding.enabled and not binding.last_error for binding in oauth_bindings),
            },
            "supported_local_tools": sorted(
                {
                    str(tool)
                    for row in chatgpt_workers
                    for tool in row["metadata"].get("supported_tools", [])
                    if isinstance(tool, str)
                }
            ),
        }
        warnings = []
        now = datetime.now(timezone.utc)
        for binding in bindings:
            if not binding.expires_at:
                continue
            try:
                expires = datetime.fromisoformat(binding.expires_at.replace("Z", "+00:00"))
            except ValueError:
                warnings.append({"kind": "session", "target_id": binding.id, "message": "Invalid expiry timestamp"})
                continue
            if expires <= now:
                warnings.append({"kind": "session", "target_id": binding.id, "message": "Credential expired"})

        return {
            "providers": providers,
            "accounts": account_rows,
            "workers": worker_rows,
            "models": models,
            "jobs": [_job_payload(job) for job in jobs],
            "diagnostics": diagnostics,
            "warnings": warnings,
            "audit": list(audits),
        }

    @router.post("/pairing-codes")
    async def create_pairing_code(
        payload: PairingCodeRequest,
        container: AppContainer = Depends(get_container),
    ) -> dict[str, Any]:
        code = container.worker_pairing.create_pairing_code(ttl_seconds=payload.ttl_seconds)
        await container.repositories.operator_audit.record(
            action="worker.pairing_code_created",
            target_type="worker",
            target_id="pending",
            detail={"ttl_seconds": payload.ttl_seconds},
        )
        return {"code": code, "expires_in_seconds": payload.ttl_seconds}

    @router.patch("/workers/{worker_id}")
    async def update_worker(
        worker_id: str,
        payload: WorkerMutation,
        container: AppContainer = Depends(get_container),
    ) -> dict[str, Any]:
        worker = await container.repositories.workers.get_device_for_auth(worker_id)
        if worker is None:
            raise HTTPException(status_code=404, detail="Worker not found")
        detail: dict[str, Any] = {}
        if payload.enabled is not None:
            if worker.revoked_at and payload.enabled:
                raise HTTPException(status_code=409, detail="A revoked worker cannot be resumed")
            await container.repositories.workers.set_device_enabled(worker_id, payload.enabled)
            detail["enabled"] = payload.enabled
        if payload.capabilities is not None:
            capabilities = tuple(item.strip() for item in payload.capabilities if item.strip())
            await container.repositories.workers.set_device_capabilities(worker_id, capabilities)
            detail["capabilities"] = sorted(set(capabilities))
        await container.repositories.operator_audit.record(
            action="worker.updated", target_type="worker", target_id=worker_id, detail=detail
        )
        return {"success": True}

    @router.delete("/workers/{worker_id}")
    async def revoke_worker(
        worker_id: str,
        payload: ConfirmMutation,
        container: AppContainer = Depends(get_container),
    ) -> dict[str, Any]:
        if payload.confirm != worker_id:
            raise HTTPException(status_code=400, detail="Confirmation must match the worker ID")
        if not await container.worker_pairing.revoke(worker_id):
            raise HTTPException(status_code=404, detail="Worker not found or already revoked")
        container.worker_runtime.disconnect(worker_id)
        await container.repositories.operator_audit.record(
            action="worker.revoked", target_type="worker", target_id=worker_id
        )
        return {"success": True}

    @router.patch("/accounts/{account_id}")
    async def update_account(
        account_id: str,
        payload: AccountMutation,
        container: AppContainer = Depends(get_container),
    ) -> dict[str, Any]:
        if not await container.repositories.provider_accounts.set_enabled(account_id, payload.enabled):
            raise HTTPException(status_code=404, detail="Account not found")
        await container.repositories.operator_audit.record(
            action="account.resumed" if payload.enabled else "account.paused",
            target_type="provider_account",
            target_id=account_id,
        )
        return {"success": True}

    @router.get("/jobs/{job_id}")
    async def get_job(
        job_id: str,
        container: AppContainer = Depends(get_container),
    ) -> dict[str, Any]:
        job = await container.repositories.generation_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        attempts = await container.repositories.generation_attempts.list_for_job(job_id)
        artifacts = await container.repositories.generation_artifacts.list_for_job(job_id)
        return {
            **_job_payload(job),
            "attempts": [
                {
                    "id": attempt.id,
                    "attempt": attempt.attempt,
                    "status": attempt.status.value,
                    "started_at": attempt.started_at,
                    "finished_at": attempt.finished_at,
                    "error_code": attempt.error_code,
                    "error_detail": attempt.error_detail,
                }
                for attempt in attempts
            ],
            "artifacts": [
                {
                    "id": artifact.id,
                    "position": artifact.position,
                    "filename": artifact.filename,
                    "media_type": artifact.media_type,
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                }
                for artifact in artifacts
            ],
        }

    return router
