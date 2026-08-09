"""Media lookup resource for Google Flow."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .base import FlowResource


class FlowMediaResource(FlowResource):
    async def get(self, access_token: str, media_name: str) -> dict[str, Any]:
        normalized_token = str(access_token or "").strip()
        normalized_name = str(media_name or "").strip()
        if not normalized_token:
            raise ValueError("get_media: AT token is required")
        if not normalized_name:
            raise ValueError("get_media: media_name is required")
        return await self.client.transport.request(
            method="GET",
            url=f"{self.client.api_base_url}/media/{quote(normalized_name, safe='')}",
            headers=self.client._build_labs_request_context_headers(None),
            use_at=True,
            at_token=normalized_token,
            timeout=max(60, int(self.client.timeout or 120)),
        )
