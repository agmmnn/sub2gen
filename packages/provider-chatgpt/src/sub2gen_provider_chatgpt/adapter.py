"""Local-only ChatGPT Web adapter for the provider SDK."""

from __future__ import annotations

import asyncio
from typing import Protocol

from sub2gen_provider_sdk import (
    GenerationKind,
    GenerationRequest,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorCode,
    ProviderExecutionContext,
    ProviderHealth,
    ProviderResult,
    TerminalStreamingProvider,
    await_with_execution_context,
)


class ChatGPTWebBackend(Protocol):
    async def health(self) -> ProviderHealth: ...

    async def generate(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> ProviderResult: ...

    async def cancel(self, provider_job_id: str) -> None: ...


class ChatGPTWebProvider(TerminalStreamingProvider):
    """Serialize ChatGPT Web work across its single logged-in browser surface."""

    def __init__(self, backend: ChatGPTWebBackend) -> None:
        self._backend = backend
        self._slot = asyncio.Lock()
        self._capabilities = ProviderCapabilities(
            provider_id="chatgpt-web",
            generation_kinds=frozenset({GenerationKind.IMAGE}),
            supports_references=True,
            supports_streaming=False,
            supports_cancellation=True,
            supports_resumability=False,
            max_references=14,
            max_concurrency=1,
            execution_location="local-worker",
            credential_kinds=frozenset({"browser_session"}),
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def health(self) -> ProviderHealth:
        health = await self._backend.health()
        if health.provider_id != self.capabilities.provider_id:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "ChatGPT backend returned the wrong provider identity")
        return health

    def _validate(self, request: GenerationRequest, context: ProviderExecutionContext) -> None:
        if context.resolved.provider_id != self.capabilities.provider_id:
            raise ProviderError(ProviderErrorCode.POLICY, "execution was not resolved to ChatGPT Web")
        if request.kind is not GenerationKind.IMAGE:
            raise ProviderError(ProviderErrorCode.INVALID_INPUT, "ChatGPT Web supports image generation only")
        if len(request.references) > (self.capabilities.max_references or 0):
            raise ProviderError(ProviderErrorCode.INVALID_INPUT, "too many reference images")

    async def generate(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> ProviderResult:
        self._validate(request, context)
        async with self._slot:
            result = await await_with_execution_context(self._backend.generate(request, context), context)
        if result.resolved != context.resolved:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "ChatGPT backend changed resolved execution")
        return result

    async def cancel(self, provider_job_id: str) -> None:
        await self._backend.cancel(provider_job_id)
