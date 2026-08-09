"""Service compatibility exports without eager application imports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "FlowClient",
    "ProxyManager",
    "LoadBalancer",
    "ConcurrencyManager",
    "TokenManager",
    "GenerationHandler",
    "RunwayService",
]

_EXPORTS = {
    "FlowClient": ("flow_client", "FlowClient"),
    "ProxyManager": ("proxy_manager", "ProxyManager"),
    "LoadBalancer": ("load_balancer", "LoadBalancer"),
    "ConcurrencyManager": ("concurrency_manager", "ConcurrencyManager"),
    "TokenManager": ("token_manager", "TokenManager"),
    "GenerationHandler": ("generation_handler", "GenerationHandler"),
    "RunwayService": ("runway_service", "RunwayService"),
}


def __getattr__(name: str) -> Any:
    export = _EXPORTS.get(name)
    if export is None:
        raise AttributeError(name)
    module_name, attribute_name = export
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value
