"""Feature ownership for the legacy admin HTTP surface."""

from __future__ import annotations

from fastapi import APIRouter

from . import api_keys, auth, cache, geminigen, logs, projects, runway, settings, system, tokens, workers


FEATURES = (
    projects,
    workers,
    api_keys,
    runway,
    geminigen,
    tokens,
    cache,
    logs,
    auth,
    settings,
    system,
)


def build_admin_router(legacy_router: APIRouter) -> APIRouter:
    """Partition existing route objects by feature while preserving handler behavior."""
    for feature in FEATURES:
        feature.router.routes.clear()

    for route in legacy_router.routes:
        path = str(getattr(route, "path", ""))
        owner = next((feature for feature in FEATURES if feature.matches(path)), system)
        owner.router.routes.append(route)

    aggregate = APIRouter()
    for feature in FEATURES:
        aggregate.include_router(feature.router)
    return aggregate
