from __future__ import annotations

import asyncio

import pytest

from sub2gen_provider_chatgpt import ChatGPTWebProvider
from sub2gen_provider_sdk import (
    Artifact,
    CancellationToken,
    GenerationRequest,
    ProviderError,
    ProviderErrorCode,
    ProviderExecutionContext,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderResult,
    ReferenceInput,
    ResolvedExecution,
)
from sub2gen_provider_sdk.testing import exercise_provider


class _ChatGPTBackend:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.active = 0
        self.maximum_active = 0
        self.cancelled: list[str] = []

    async def health(self) -> ProviderHealth:
        return ProviderHealth("chatgpt-web", ProviderHealthStatus.READY, "relay connected")

    async def generate(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> ProviderResult:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            if self.block:
                await asyncio.Event().wait()
            await asyncio.sleep(0.01)
            reference_bytes = b"".join(reference.read_bytes() for reference in request.references)
            return ProviderResult(
                artifacts=(Artifact("image/png", data=b"chatgpt-image:" + reference_bytes),),
                resolved=context.resolved,
            )
        finally:
            self.active -= 1

    async def cancel(self, provider_job_id: str) -> None:
        self.cancelled.append(provider_job_id)


def _context() -> ProviderExecutionContext:
    return ProviderExecutionContext(
        resolved=ResolvedExecution(
            requested_model="chatgpt/gpt-image-web",
            resolved_model="chatgpt/gpt-image-web",
            provider_id="chatgpt-web",
            provider_account_id="browser-profile",
            worker_id="local-worker",
            billing_pool="chatgpt-subscription",
        ),
        cancellation=CancellationToken(),
        timeout_seconds=1,
    )


def _request(request_id: str = "request-1") -> GenerationRequest:
    return GenerationRequest(
        request_id=request_id,
        prompt="draw",
        model="chatgpt/gpt-image-web",
        references=(ReferenceInput("image/png", data=b"reference"),),
        provider_options={"project": "sub2gen"},
    )


@pytest.mark.asyncio
async def test_chatgpt_adapter_passes_conformance_as_terminal_local_provider() -> None:
    provider = ChatGPTWebProvider(_ChatGPTBackend())

    report = await exercise_provider(provider, _request(), _context())

    assert report.provider_id == "chatgpt-web"
    assert provider.capabilities.supports_streaming is False
    assert provider.capabilities.execution_location == "local-worker"
    assert provider.capabilities.credential_kinds == frozenset({"browser_session"})


@pytest.mark.asyncio
async def test_chatgpt_adapter_serializes_the_browser_surface() -> None:
    backend = _ChatGPTBackend()
    provider = ChatGPTWebProvider(backend)

    await asyncio.gather(
        provider.generate(_request("one"), _context()),
        provider.generate(_request("two"), _context()),
    )

    assert backend.maximum_active == 1


@pytest.mark.asyncio
async def test_chatgpt_adapter_propagates_cancellation_to_running_work() -> None:
    backend = _ChatGPTBackend(block=True)
    provider = ChatGPTWebProvider(backend)
    context = _context()
    task = asyncio.create_task(provider.generate(_request(), context))
    await asyncio.sleep(0)
    context.cancellation.cancel()

    with pytest.raises(ProviderError) as caught:
        await task
    assert caught.value.code is ProviderErrorCode.CANCELLED
