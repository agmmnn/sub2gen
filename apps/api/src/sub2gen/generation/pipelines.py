"""Explicit image and video generation pipelines."""

from __future__ import annotations

from typing import Any, AsyncIterator


class ImageGenerationPipeline:
    def __init__(self, handler: Any) -> None:
        self.handler = handler

    async def run(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async for chunk in self.handler._handle_image_generation(*args, **kwargs):
            yield chunk


class VideoGenerationPipeline:
    def __init__(self, handler: Any) -> None:
        self.handler = handler

    async def run(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async for chunk in self.handler._handle_video_generation(*args, **kwargs):
            yield chunk
