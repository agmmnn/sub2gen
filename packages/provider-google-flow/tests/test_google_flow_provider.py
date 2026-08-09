from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from sub2gen_provider_google_flow import GoogleFlowProvider
from sub2gen_provider_sdk import (
    Artifact,
    CancellationToken,
    GenerationRequest,
    ProviderEvent,
    ProviderEventKind,
    ProviderExecutionContext,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderJob,
    ProviderResult,
    ReferenceInput,
    ResolvedExecution,
)
from sub2gen_provider_sdk.testing import exercise_provider


class _FlowBackend:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def health(self) -> ProviderHealth:
        return ProviderHealth("google-flow", ProviderHealthStatus.READY)

    def _result(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> ProviderResult:
        reference_bytes = b"".join(reference.read_bytes() for reference in request.references)
        return ProviderResult(
            artifacts=(Artifact("image/png", data=b"flow-image:" + reference_bytes),),
            resolved=context.resolved,
            provider_job=ProviderJob("flow-job", resumable=True),
        )

    async def generate(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> ProviderResult:
        return self._result(request, context)

    async def stream(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> AsyncIterator[ProviderEvent]:
        result = self._result(request, context)
        yield ProviderEvent(ProviderEventKind.ACCEPTED, provider_job=result.provider_job)
        yield ProviderEvent(ProviderEventKind.PROGRESS, progress=0.5)
        yield ProviderEvent(ProviderEventKind.ARTIFACT, artifact=result.artifacts[0])
        yield ProviderEvent(ProviderEventKind.COMPLETED, progress=1.0, result=result)

    async def cancel(self, provider_job_id: str) -> None:
        self.cancelled.append(provider_job_id)


def _context() -> ProviderExecutionContext:
    return ProviderExecutionContext(
        resolved=ResolvedExecution(
            requested_model="imagen",
            resolved_model="imagen",
            provider_id="google-flow",
            provider_account_id="flow-account",
            billing_pool="flow-subscription",
        ),
        cancellation=CancellationToken(),
        timeout_seconds=1,
    )


@pytest.mark.asyncio
async def test_google_flow_adapter_passes_provider_conformance_with_native_progress() -> None:
    backend = _FlowBackend()
    provider = GoogleFlowProvider(backend)
    request = GenerationRequest(
        request_id="request-1",
        prompt="draw",
        model="imagen",
        references=(ReferenceInput("image/png", data=b"reference"),),
    )

    report = await exercise_provider(provider, request, _context())

    assert report.provider_id == "google-flow"
    assert ProviderEventKind.PROGRESS in report.event_kinds
    assert provider.capabilities.supports_resumability is True
    await provider.cancel("flow-job")
    assert backend.cancelled == ["flow-job"]
