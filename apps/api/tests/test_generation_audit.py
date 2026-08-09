from __future__ import annotations

import asyncio

import pytest

from sub2gen.core.database import Database
from sub2gen.persistence import (
    GenerationAttemptStatus,
    GenerationJobStatus,
    ProviderAccountRecord,
    Repositories,
    WorkerDeviceRecord,
)
from sub2gen.services.generation_audit import GenerationAuditService
from sub2gen_provider_sdk import ProviderError, ProviderErrorCode, ResolvedExecution


def resolved() -> ResolvedExecution:
    return ResolvedExecution(
        requested_model="chatgpt/gpt-image-web",
        resolved_model="chatgpt/gpt-image-web",
        provider_id="chatgpt-web",
        billing_pool="chatgpt:web-subscription",
        provider_account_id="account-a",
        worker_id="worker-a",
    )


async def build_audit(tmp_path):
    database = Database(str(tmp_path / "audit.db"))
    await database.init_db()
    repositories = Repositories.from_database(database)
    await repositories.provider_accounts.create(
        ProviderAccountRecord(id="account-a", provider_key="chatgpt-web", label="ChatGPT")
    )
    await repositories.workers.register_device(
        WorkerDeviceRecord(
            id="worker-a",
            kind="image-worker",
            label="Worker",
            approved_capabilities=("image.generate:chatgpt-web",),
        )
    )
    return repositories, GenerationAuditService(repositories.generation_jobs, repositories.generation_attempts)


@pytest.mark.parametrize(
    ("failure", "job_status", "attempt_status", "error_code"),
    [
        (ProviderError(ProviderErrorCode.UNAVAILABLE, "offline"), GenerationJobStatus.FAILED, GenerationAttemptStatus.FAILED, "unavailable"),
        (ProviderError(ProviderErrorCode.TIMEOUT, "late"), GenerationJobStatus.TIMED_OUT, GenerationAttemptStatus.EXPIRED, "timeout"),
        (ProviderError(ProviderErrorCode.CANCELLED, "stopped"), GenerationJobStatus.CANCELLED, GenerationAttemptStatus.CANCELLED, "cancelled"),
    ],
)
async def test_every_provider_terminal_failure_persists_exact_execution(
    tmp_path, failure, job_status, attempt_status, error_code
):
    repositories, audit = await build_audit(tmp_path)
    job = await audit.queue(
        request_id="request",
        job_kind="image.generate",
        requested_model=resolved().requested_model,
        api_key_id=None,
    )

    async def operation():
        raise failure

    with pytest.raises(ProviderError):
        await audit.run(job=job, resolved=resolved(), operation=operation)

    stored = await repositories.generation_jobs.get(job.id)
    attempts = await repositories.generation_attempts.list_for_job(job.id)
    assert stored is not None and stored.status is job_status
    assert stored.resolved_execution["worker_id"] == "worker-a"
    assert stored.error_code == error_code
    assert attempts[0].status is attempt_status
    assert attempts[0].resolved_execution == stored.resolved_execution


async def test_success_is_audited_without_a_worker_lease(tmp_path):
    repositories, audit = await build_audit(tmp_path)
    job = await audit.queue(
        request_id="request-success",
        job_kind="image.generate",
        requested_model=resolved().requested_model,
        api_key_id=None,
    )

    result = await audit.run(job=job, resolved=resolved(), operation=lambda: asyncio.sleep(0, result="ok"))

    stored = await repositories.generation_jobs.get(job.id)
    attempts = await repositories.generation_attempts.list_for_job(job.id)
    assert result == "ok"
    assert stored is not None and stored.status is GenerationJobStatus.SUCCEEDED
    assert stored.terminal_at is not None
    assert attempts[0].status is GenerationAttemptStatus.SUCCEEDED
    assert attempts[0].finished_at is not None


async def test_startup_reconciliation_closes_non_resumable_chatgpt_jobs(tmp_path):
    repositories, audit = await build_audit(tmp_path)
    job = await audit.queue(
        request_id="interrupted",
        job_kind="image.generate",
        requested_model="chatgpt/gpt-image-web",
        api_key_id=None,
    )

    assert await audit.reconcile_non_resumable_jobs() == 1
    stored = await repositories.generation_jobs.get(job.id)
    assert stored is not None and stored.status is GenerationJobStatus.FAILED
    assert stored.error_code == "process_restart"
