"""Core compatibility exports without eager application composition."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["config", "AuthManager", "verify_api_key_header", "debug_logger"]

_EXPORTS = {
    "config": ("config", "config"),
    "AuthManager": ("auth", "AuthManager"),
    "verify_api_key_header": ("auth", "verify_api_key_header"),
    "debug_logger": ("logger", "debug_logger"),
}


def __getattr__(name: str) -> Any:
    export = _EXPORTS.get(name)
    if export is None:
        raise AttributeError(name)
    module_name, attribute_name = export
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value
