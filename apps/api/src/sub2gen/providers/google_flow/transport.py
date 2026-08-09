"""Shared transport boundary for Google Flow resources."""

from __future__ import annotations

from typing import Any

from .base import FlowResource


class FlowTransport(FlowResource):
    async def request(self, *, method: str, url: str, **kwargs: Any) -> Any:
        return await self.client._make_request(method=method, url=url, **kwargs)

    async def request_text(self, *, method: str, url: str, **kwargs: Any) -> str:
        return await self.client._make_text_request(method=method, url=url, **kwargs)
