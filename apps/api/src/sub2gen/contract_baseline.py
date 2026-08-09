"""Deterministic contract snapshots used to guard architecture migrations."""

from __future__ import annotations

from .core.geminigen_manifest import GEMINIGEN_MANIFEST_VERSION, GEMINIGEN_MODEL_MANIFEST
from .core.model_resolver import get_base_model_aliases
from .core.runway_manifest import RUNWAY_MANIFEST_VERSION, RUNWAY_MODEL_MANIFEST
from .services.generation_handler import MODEL_CONFIG


def build_model_catalog_snapshot() -> dict[str, object]:
    """Return the complete catalog inputs used before provider unification."""
    return {
        "snapshot_version": 1,
        "native": {
            "models": MODEL_CONFIG,
            "aliases_without_4k": get_base_model_aliases(include_4k=False),
            "aliases_with_4k": get_base_model_aliases(include_4k=True),
        },
        "runway": {
            "manifest_version": RUNWAY_MANIFEST_VERSION,
            "models": RUNWAY_MODEL_MANIFEST,
        },
        "geminigen": {
            "manifest_version": GEMINIGEN_MANIFEST_VERSION,
            "models": GEMINIGEN_MODEL_MANIFEST,
        },
    }
