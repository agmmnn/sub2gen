"""Authentication and account resource for Google Flow."""

from __future__ import annotations

from .base import FlowResource


class FlowAuthResource(FlowResource):
    async def exchange_session_token(self, session_token: str) -> dict:
        return await self.client.transport.request(
            method="GET",
            url=f"{self.client.labs_base_url}/auth/session",
            use_st=True,
            st_token=session_token,
            timeout=self.client._get_control_plane_timeout(),
        )

    async def get_credits(self, access_token: str) -> dict:
        return await self.client.transport.request(
            method="GET",
            url=f"{self.client.api_base_url}/credits",
            use_at=True,
            at_token=access_token,
            timeout=self.client._get_control_plane_timeout(),
        )
