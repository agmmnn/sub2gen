"""Thin provider-SDK adapter around the existing Google Flow execution boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from sub2gen_provider_sdk import (
    GenerationKind,
    GenerationRequest,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorCode,
    ProviderEvent,
    ProviderEventKind,
    ProviderExecutionContext,
    ProviderHealth,
    ProviderResult,
    await_with_execution_context,
)


class GoogleFlowBackend(Protocol):
    async def health(self) -> ProviderHealth: ...

    async def generate(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> ProviderResult: ...

    def stream(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> AsyncIterator[ProviderEvent]: ...

    async def cancel(self, provider_job_id: str) -> None: ...


class GoogleFlowProvider:
    """Expose Flow without moving its existing transport or account logic yet."""

    def __init__(self, backend: GoogleFlowBackend) -> None:
        self._backend = backend
        self._capabilities = ProviderCapabilities(
            provider_id="google-flow",
            generation_kinds=frozenset({GenerationKind.IMAGE, GenerationKind.VIDEO}),
            supports_references=True,
            supports_streaming=True,
            supports_cancellation=True,
            supports_resumability=True,
            max_references=14,
            execution_location="server",
            credential_kinds=frozenset({"session_token"}),
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def health(self) -> ProviderHealth:
        health = await self._backend.health()
        if health.provider_id != self.capabilities.provider_id:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Flow backend returned the wrong provider identity")
        return health

    def _validate(self, request: GenerationRequest, context: ProviderExecutionContext) -> None:
        if context.resolved.provider_id != self.capabilities.provider_id:
            raise ProviderError(ProviderErrorCode.POLICY, "execution was not resolved to Google Flow")
        if len(request.references) > (self.capabilities.max_references or 0):
            raise ProviderError(ProviderErrorCode.INVALID_INPUT, "too many reference images")

    async def generate(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> ProviderResult:
        self._validate(request, context)
        result = await await_with_execution_context(self._backend.generate(request, context), context)
        if result.resolved != context.resolved:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Flow backend changed resolved execution")
        return result

    async def stream(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> AsyncIterator[ProviderEvent]:
        self._validate(request, context)
        completed = False
        async for event in self._backend.stream(request, context):
            if context.cancellation.cancelled:
                raise ProviderError(ProviderErrorCode.CANCELLED, "provider execution cancelled")
            if event.kind is ProviderEventKind.COMPLETED:
                completed = True
                if event.result is None or event.result.resolved != context.resolved:
                    raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Flow stream changed resolved execution")
            yield event
        if not completed:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Flow stream ended without completion")

    async def cancel(self, provider_job_id: str) -> None:
        await self._backend.cancel(provider_job_id)
