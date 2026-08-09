"""Provider protocol and execution helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from typing import Protocol, TypeVar, runtime_checkable

from .contracts import (
    GenerationRequest,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorCode,
    ProviderEvent,
    ProviderEventKind,
    ProviderExecutionContext,
    ProviderHealth,
    ProviderResult,
)

T = TypeVar("T")


async def await_with_execution_context(
    operation: Awaitable[T],
    context: ProviderExecutionContext,
) -> T:
    """Run an operation with cooperative cancellation and a strict timeout."""

    if context.cancellation.cancelled:
        if hasattr(operation, "close"):
            operation.close()  # type: ignore[attr-defined]
        raise ProviderError(ProviderErrorCode.CANCELLED, "provider execution cancelled")

    operation_task = asyncio.ensure_future(operation)
    cancellation_task = asyncio.create_task(context.cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            {operation_task, cancellation_task},
            timeout=context.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            raise ProviderError(ProviderErrorCode.CANCELLED, "provider execution cancelled")
        if operation_task not in done:
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            raise ProviderError(ProviderErrorCode.TIMEOUT, "provider execution timed out", retryable=True)
        return operation_task.result()
    finally:
        cancellation_task.cancel()
        await asyncio.gather(cancellation_task, return_exceptions=True)


@runtime_checkable
class GenerationProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

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


class TerminalStreamingProvider:
    """Default event mapping for providers with only a terminal response."""

    async def generate(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> ProviderResult:
        raise NotImplementedError

    async def stream(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(kind=ProviderEventKind.ACCEPTED)
        result = await self.generate(request, context)
        for artifact in result.artifacts:
            yield ProviderEvent(kind=ProviderEventKind.ARTIFACT, artifact=artifact)
        yield ProviderEvent(kind=ProviderEventKind.COMPLETED, progress=1.0, result=result)

    async def cancel(self, provider_job_id: str) -> None:
        del provider_job_id
