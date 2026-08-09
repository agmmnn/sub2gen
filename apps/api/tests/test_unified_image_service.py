from __future__ import annotations

import hashlib

import pytest

from sub2gen.bootstrap.container import build_container
from sub2gen.core.api_key_manager import AuthContext
from sub2gen.core.database import Database
from sub2gen.persistence import CredentialBindingRecord, CredentialStorageKind, ProviderAccountRecord, WorkerDeviceRecord
from sub2gen.services.file_cache import FileCache
from sub2gen.services.provider_execution import ProviderArtifactCommitter, ProviderExecutionService
from sub2gen.services.unified_images import WorkerChatGPTBackend
from sub2gen.services.worker_artifacts import WorkerArtifactInbox
from sub2gen.services.worker_runtime import WorkerRuntimeError
from sub2gen_provider_sdk import CancellationToken, GenerationRequest, ProviderError, ProviderErrorCode, ProviderExecutionContext, ReferenceInput, ResolvedExecution
from sub2gen_worker_protocol.codec import decode_envelope, make_envelope
from sub2gen_worker_protocol.generated import JobDecisionPayload, JobResultPayload, MessageType


async def configured_container(tmp_path):
    database = Database(str(tmp_path / "images.db"))
    await database.init_db()
    container = build_container(database=database)
    cache = FileCache(
        cache_dir=str(tmp_path / "cache"),
        db=database,
        cache_repository=container.repositories.cache,
    )
    container.generation_handler.file_cache = cache
    container.generation_handler.provider_execution = ProviderExecutionService(ProviderArtifactCommitter(cache))
    container.worker_artifact_inbox = WorkerArtifactInbox(container.worker_artifact_grants, tmp_path / "inbox")
    account = await container.repositories.provider_accounts.create(
        ProviderAccountRecord(id="account-chatgpt", provider_key="chatgpt-web", label="ChatGPT")
    )
    worker = await container.repositories.workers.register_device(
        WorkerDeviceRecord(
            id="worker-chatgpt",
            kind="image-worker",
            label="Mac",
            approved_capabilities=("image.generate:chatgpt-web",),
        )
    )
    await container.repositories.credential_bindings.create(
        CredentialBindingRecord(
            provider_account_id=account.id,
            worker_id=worker.id,
            binding_key="browser",
            credential_type="chatgpt-browser-profile",
            storage_kind=CredentialStorageKind.BROWSER_SESSION,
            secret_ref="browser-session://profile/default",
        )
    )
    key_id = await container.repositories.api_keys.create_client_api_key(
        client_name="test",
        label="test",
        key_prefix="s2g_test",
        key_plaintext=None,
        key_hash="2" * 64,
        scopes="generation:create",
        account_ids=[],
        endpoint_limits={},
        expires_at=None,
    )
    await container.repositories.provider_accounts.assign_api_key(account.id, key_id)
    auth = AuthContext(key_id, "test", False, set(), {"generation:create"})
    return container, auth, account, worker


async def test_worker_generation_commits_repeated_references_and_is_idempotent(tmp_path):
    container, auth, account, worker = await configured_container(tmp_path)
    container.worker_coordinator.register(
        worker_id=worker.id,
        worker_session_id="session",
        capabilities=("image.generate:chatgpt-web",),
        approved_capabilities={"image.generate:chatgpt-web"},
        available_slots=1,
    )
    offered_inputs = []
    image = b"generated-image"

    async def sender(frame):
        envelope, offer = decode_envelope(frame)
        if envelope.message_type is not MessageType.JOB_OFFER:
            return
        offered_inputs.append(offer.input)
        grant = offer.artifact_upload_grants[0]
        container.worker_artifact_inbox.ingest(
            grant["grant_id"],
            worker_id=worker.id,
            job_id=envelope.job_id,
            media_type="image/png",
            body=image,
        )
        decision = JobDecisionPayload(attempt=1, lease_id=offer.lease_id)
        await container.worker_runtime.handle(
            make_envelope(
                message_type=MessageType.JOB_ACCEPT,
                worker_id=worker.id,
                job_id=envelope.job_id,
                job_kind=envelope.job_kind,
                payload=decision,
            ),
            decision,
        )
        result = JobResultPayload(
            attempt=1,
            lease_id=offer.lease_id,
            output={
                "artifacts": [
                    {"grant_id": grant["grant_id"], "sha256": hashlib.sha256(image).hexdigest()}
                ]
            },
        )
        await container.worker_runtime.handle(
            make_envelope(
                message_type=MessageType.JOB_RESULT,
                worker_id=worker.id,
                job_id=envelope.job_id,
                job_kind=envelope.job_kind,
                payload=result,
            ),
            result,
        )

    container.worker_runtime.connect(worker.id, sender)
    references = (
        ReferenceInput("image/png", data=b"reference-one"),
        ReferenceInput("image/jpeg", data=b"reference-two"),
    )
    prepared = await container.unified_images.prepare(
        prompt="draw",
        model="chatgpt/gpt-image-web",
        references=references,
        auth=auth,
        base_url="http://127.0.0.1:8000",
        idempotency_key="same-request",
    )

    await container.unified_images.execute(prepared)
    duplicate = await container.unified_images.prepare(
        prompt="draw",
        model="chatgpt/gpt-image-web",
        references=references,
        auth=auth,
        base_url="http://127.0.0.1:8000",
        idempotency_key="same-request",
    )
    job, artifacts = await container.unified_images.get_job(prepared.job.id, auth.key_id)

    assert duplicate.duplicate is True and duplicate.job.id == prepared.job.id
    assert job.status.value == "succeeded"
    assert job.resolved_execution["provider_account_id"] == account.id
    assert len(artifacts) == 1
    assert await container.generation_handler.file_cache.read_bytes(artifacts[0].filename) == image
    assert len(offered_inputs[0]["references"]) == 2
    with pytest.raises(ValueError, match="different request"):
        await container.unified_images.prepare(
            prompt="different prompt",
            model="chatgpt/gpt-image-web",
            references=references,
            auth=auth,
            base_url="http://127.0.0.1:8000",
            idempotency_key="same-request",
        )


async def test_worker_timeout_is_classified_for_retry_policy():
    class Runtime:
        async def dispatch(self, **kwargs):
            raise WorkerRuntimeError("worker job timed out")

    backend = WorkerChatGPTBackend(Runtime(), object(), base_url="http://127.0.0.1:8000")
    resolved = ResolvedExecution(
        "chatgpt/gpt-image-web",
        "chatgpt/gpt-image-web",
        "chatgpt-web",
        "chatgpt:web-subscription",
        worker_id="worker",
    )
    with pytest.raises(ProviderError) as raised:
        await backend.generate(
            GenerationRequest("job", "draw", "chatgpt/gpt-image-web"),
            ProviderExecutionContext(resolved, CancellationToken(), 1),
        )
    assert raised.value.code is ProviderErrorCode.TIMEOUT
