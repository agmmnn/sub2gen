"""Durable audit lifecycle for provider-neutral generation attempts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sub2gen_provider_sdk import ProviderError, ProviderErrorCode, ResolvedExecution

from ..persistence.domain import (
    GenerationAttemptRecord,
    GenerationAttemptStatus,
    GenerationJobRecord,
    GenerationJobStatus,
)
from ..persistence.unified_repositories import GenerationAttemptRepository, GenerationJobRepository

T = TypeVar("T")


def resolved_execution_dict(resolved: ResolvedExecution) -> dict[str, str | None]:
    return {
        "requested_model": resolved.requested_model,
        "resolved_model": resolved.resolved_model,
        "provider_id": resolved.provider_id,
        "billing_pool": resolved.billing_pool,
        "provider_account_id": resolved.provider_account_id,
        "worker_id": resolved.worker_id,
    }


class GenerationAuditService:
    def __init__(self, jobs: GenerationJobRepository, attempts: GenerationAttemptRepository) -> None:
        self.jobs = jobs
        self.attempts = attempts

    async def queue(
        self,
        *,
        request_id: str,
        job_kind: str,
        requested_model: str,
        api_key_id: int | None,
        idempotency_key: str | None = None,
        deadline_at: str | None = None,
        request_fingerprint: str | None = None,
    ) -> GenerationJobRecord:
        return await self.jobs.create(
            GenerationJobRecord(
                request_id=request_id,
                job_kind=job_kind,
                requested_model=requested_model,
                api_key_id=api_key_id,
                idempotency_key=idempotency_key,
                deadline_at=deadline_at,
                resolved_execution=(
                    {"request_fingerprint": request_fingerprint}
                    if request_fingerprint is not None
                    else None
                ),
            )
        )

    async def reconcile_non_resumable_jobs(self) -> int:
        """Close browser-backed work that cannot survive an API process restart."""
        reconciled = 0
        for job in await self.jobs.list_active():
            execution = dict(job.resolved_execution or {})
            provider_id = str(execution.get("provider_id") or "")
            if provider_id not in {"chatgpt-web", "chatgpt-codex"} and not job.requested_model.startswith("chatgpt/"):
                continue
            attempts = await self.attempts.list_for_job(job.id)
            for attempt in attempts:
                if attempt.finished_at is None:
                    await self.attempts.finish(
                        attempt.id,
                        expected_lease_id=attempt.lease_id,
                        status=GenerationAttemptStatus.FAILED,
                        error_code="process_restart",
                        error_detail="browser-backed generation cannot resume after API restart",
                    )
            changed = await self.jobs.transition(
                job.id,
                expected=(GenerationJobStatus.QUEUED, GenerationJobStatus.OFFERED, GenerationJobStatus.RUNNING),
                status=GenerationJobStatus.FAILED,
                error_code="process_restart",
                error_detail="browser-backed generation cannot resume after API restart",
                terminal=True,
            )
            reconciled += int(changed)
        return reconciled

    async def run(
        self,
        *,
        job: GenerationJobRecord,
        resolved: ResolvedExecution,
        operation: Callable[[], Awaitable[T]],
        attempt_number: int = 1,
        lease_id: str | None = None,
    ) -> T:
        execution = resolved_execution_dict(resolved)
        if job.resolved_execution and job.resolved_execution.get("request_fingerprint"):
            execution["request_fingerprint"] = str(job.resolved_execution["request_fingerprint"])
        started = await self.jobs.transition(
            job.id,
            expected=(GenerationJobStatus.QUEUED, GenerationJobStatus.OFFERED),
            status=GenerationJobStatus.RUNNING,
            provider_account_id=resolved.provider_account_id,
            worker_id=resolved.worker_id,
            resolved_execution=execution,
        )
        if not started:
            raise RuntimeError("generation job could not enter running state")
        attempt = await self.attempts.create(
            GenerationAttemptRecord(
                job_id=job.id,
                attempt=attempt_number,
                status=GenerationAttemptStatus.RUNNING,
                lease_id=lease_id,
                resolved_execution=execution,
            )
        )
        try:
            result = await operation()
        except asyncio.CancelledError:
            await self._finish(job, attempt, lease_id, GenerationJobStatus.CANCELLED, GenerationAttemptStatus.CANCELLED, "cancelled", "generation cancelled")
            raise
        except ProviderError as exc:
            if exc.code is ProviderErrorCode.CANCELLED:
                job_status, attempt_status = GenerationJobStatus.CANCELLED, GenerationAttemptStatus.CANCELLED
            elif exc.code is ProviderErrorCode.TIMEOUT:
                job_status, attempt_status = GenerationJobStatus.TIMED_OUT, GenerationAttemptStatus.EXPIRED
            else:
                job_status, attempt_status = GenerationJobStatus.FAILED, GenerationAttemptStatus.FAILED
            await self._finish(job, attempt, lease_id, job_status, attempt_status, exc.code.value, exc.detail)
            raise
        except TimeoutError as exc:
            await self._finish(job, attempt, lease_id, GenerationJobStatus.TIMED_OUT, GenerationAttemptStatus.EXPIRED, "timeout", str(exc) or "generation timed out")
            raise
        except Exception as exc:
            await self._finish(job, attempt, lease_id, GenerationJobStatus.FAILED, GenerationAttemptStatus.FAILED, "internal", str(exc))
            raise
        await self._finish(job, attempt, lease_id, GenerationJobStatus.SUCCEEDED, GenerationAttemptStatus.SUCCEEDED, None, None)
        return result

    async def _finish(
        self,
        job: GenerationJobRecord,
        attempt: GenerationAttemptRecord,
        lease_id: str | None,
        job_status: GenerationJobStatus,
        attempt_status: GenerationAttemptStatus,
        error_code: str | None,
        error_detail: str | None,
    ) -> None:
        await self.attempts.finish(
            attempt.id,
            expected_lease_id=lease_id,
            status=attempt_status,
            error_code=error_code,
            error_detail=error_detail,
        )
        await self.jobs.transition(
            job.id,
            expected=(GenerationJobStatus.RUNNING,),
            status=job_status,
            error_code=error_code,
            error_detail=error_detail,
            terminal=True,
        )
