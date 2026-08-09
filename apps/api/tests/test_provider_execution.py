from __future__ import annotations

import hashlib

import pytest

from sub2gen.services.provider_execution import ProviderArtifactCommitter, ProviderExecutionService
from sub2gen_provider_chatgpt import ChatGPTWebProvider
from sub2gen_provider_google_flow import GoogleFlowProvider
from sub2gen_provider_sdk import (
    Artifact,
    CancellationToken,
    GenerationRequest,
    ProviderExecutionContext,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderResult,
    ResolvedExecution,
)


class MemoryFileCache:
    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str, dict]] = {}

    async def store_bytes(self, filename, content, content_type, **metadata):
        self.files[filename] = (content, content_type, metadata)
        return filename


class Backend:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    async def health(self):
        return ProviderHealth(self.provider_id, ProviderHealthStatus.READY)

    async def generate(self, request, context):
        content = b"provider-image"
        return ProviderResult(
            artifacts=(
                Artifact(
                    media_type="image/png",
                    data=content,
                    filename="result.png",
                    sha256=hashlib.sha256(content).hexdigest(),
                ),
            ),
            resolved=context.resolved,
        )

    async def stream(self, request, context):
        if False:
            yield None

    async def cancel(self, provider_job_id):
        return None


@pytest.mark.parametrize(
    ("provider_id", "provider_type"),
    [("google-flow", GoogleFlowProvider), ("chatgpt-web", ChatGPTWebProvider)],
)
async def test_both_providers_use_the_same_execution_and_commit_lifecycle(provider_id, provider_type):
    cache = MemoryFileCache()
    service = ProviderExecutionService(ProviderArtifactCommitter(cache))
    provider = provider_type(Backend(provider_id))
    resolved = ResolvedExecution("image", "image", provider_id, f"{provider_id}:personal")
    request = GenerationRequest(request_id=f"request/{provider_id}", prompt="draw", model="image")
    context = ProviderExecutionContext(resolved, CancellationToken(), timeout_seconds=5)

    outcome = await service.execute(provider, request, context, api_key_id=7)

    assert outcome.result.resolved is resolved
    assert len(outcome.artifacts) == 1
    committed = outcome.artifacts[0]
    assert committed.filename.startswith(f"request-{provider_id}-1-")
    assert cache.files[committed.filename][0] == b"provider-image"
    assert cache.files[committed.filename][2]["api_key_id"] == 7


async def test_artifact_commit_rejects_a_false_provider_checksum():
    cache = MemoryFileCache()
    committer = ProviderArtifactCommitter(cache)

    with pytest.raises(ValueError, match="checksum"):
        await committer.commit(
            Artifact(media_type="image/png", data=b"image", sha256="0" * 64),
            request_id="request",
            index=0,
        )

    assert cache.files == {}
