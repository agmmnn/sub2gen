"""Provider SDK adapter for direct Google Gemini generation."""

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


class GoogleGeminiBackend(Protocol):
    async def health(self) -> ProviderHealth: ...

    async def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> ProviderResult: ...

    async def cancel(self, provider_job_id: str) -> None: ...


class GoogleGeminiProvider(TerminalStreamingProvider):
    def __init__(self, backend: GoogleGeminiBackend, *, max_concurrency: int = 2) -> None:
        self._backend = backend
        self._slots = asyncio.Semaphore(max(1, max_concurrency))
        self._capabilities = ProviderCapabilities(
            provider_id="google-gemini",
            generation_kinds=frozenset({GenerationKind.IMAGE}),
            supports_references=True,
            supports_streaming=False,
            supports_cancellation=True,
            supports_resumability=False,
            max_references=14,
            max_concurrency=max(1, max_concurrency),
            execution_location="server",
            credential_kinds=frozenset({"api_key"}),
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def health(self) -> ProviderHealth:
        health = await self._backend.health()
        if health.provider_id != self.capabilities.provider_id:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Gemini backend returned the wrong provider identity")
        return health

    async def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> ProviderResult:
        if context.resolved.provider_id != self.capabilities.provider_id:
            raise ProviderError(ProviderErrorCode.POLICY, "execution was not resolved to Google Gemini")
        if request.kind is not GenerationKind.IMAGE:
            raise ProviderError(ProviderErrorCode.INVALID_INPUT, "Google Gemini adapter supports images only")
        if len(request.references) > (self.capabilities.max_references or 0):
            raise ProviderError(ProviderErrorCode.INVALID_INPUT, "too many reference images")
        async with self._slots:
            result = await await_with_execution_context(self._backend.generate(request, context), context)
        if result.resolved != context.resolved:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Gemini backend changed resolved execution")
        return result

    async def cancel(self, provider_job_id: str) -> None:
        await self._backend.cancel(provider_job_id)
