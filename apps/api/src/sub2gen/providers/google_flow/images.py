"""Image capability resource for Google Flow."""

from __future__ import annotations

from typing import Any

from .base import FlowResource


class FlowImagesResource(FlowResource):
    async def upload(self, *args: Any, **kwargs: Any) -> str:
        return await self.client._upload_image_impl(*args, **kwargs)

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        return await self.client._generate_image_impl(*args, **kwargs)

    async def upsample(self, *args: Any, **kwargs: Any) -> Any:
        return await self.client._upsample_image_impl(*args, **kwargs)
