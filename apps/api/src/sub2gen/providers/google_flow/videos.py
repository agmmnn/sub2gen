"""Video capability resource for Google Flow."""

from __future__ import annotations

from typing import Any

from .base import FlowResource


class FlowVideosResource(FlowResource):
    async def call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        implementation = getattr(self.client, f"_{operation}_impl")
        return await implementation(*args, **kwargs)
