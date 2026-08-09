"""Reusable provider conformance helpers with no pytest or application dependency."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    GenerationRequest,
    ProviderEventKind,
    ProviderExecutionContext,
    ProviderHealthStatus,
)
from .provider import GenerationProvider


@dataclass(frozen=True, slots=True)
class ProviderConformanceReport:
    provider_id: str
    artifact_count: int
    event_kinds: tuple[ProviderEventKind, ...]


async def exercise_provider(
    provider: GenerationProvider,
    request: GenerationRequest,
    context: ProviderExecutionContext,
) -> ProviderConformanceReport:
    """Exercise health, terminal generation, references, and event streaming."""

    health = await provider.health()
    assert health.provider_id == provider.capabilities.provider_id
    assert health.status in {
        ProviderHealthStatus.READY,
        ProviderHealthStatus.DEGRADED,
        ProviderHealthStatus.UNAVAILABLE,
    }
    assert request.kind in provider.capabilities.generation_kinds
    if request.references:
        assert provider.capabilities.supports_references
        maximum = provider.capabilities.max_references
        assert maximum is None or len(request.references) <= maximum

    result = await provider.generate(request, context)
    assert result.resolved.provider_id == provider.capabilities.provider_id
    assert result.artifacts
    assert all(artifact.read_bytes() for artifact in result.artifacts)

    events = [event async for event in provider.stream(request, context)]
    kinds = tuple(event.kind for event in events)
    assert kinds[0] is ProviderEventKind.ACCEPTED
    assert ProviderEventKind.ARTIFACT in kinds
    assert kinds[-1] is ProviderEventKind.COMPLETED
    assert events[-1].result is not None

    return ProviderConformanceReport(
        provider_id=provider.capabilities.provider_id,
        artifact_count=len(result.artifacts),
        event_kinds=kinds,
    )
