"""Provider-isolated health, quota, and capacity signals."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RouteHealth(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RouteSignal:
    health: RouteHealth = RouteHealth.READY
    quota_remaining: int | None = None
    available_slots: int | None = None


class RuntimeSignalRegistry:
    """Signals are keyed by the exact provider/account/worker execution target."""

    def __init__(self) -> None:
        self._signals: dict[tuple[str, str | None, str | None], RouteSignal] = {}

    def update(
        self,
        *,
        provider_id: str,
        provider_account_id: str | None,
        worker_id: str | None,
        signal: RouteSignal,
    ) -> None:
        self._signals[(provider_id, provider_account_id, worker_id)] = signal

    def get(self, provider_id: str, provider_account_id: str | None, worker_id: str | None) -> RouteSignal:
        return self._signals.get((provider_id, provider_account_id, worker_id), RouteSignal())
