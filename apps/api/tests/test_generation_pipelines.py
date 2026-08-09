from __future__ import annotations

import pytest

from sub2gen.generation import (
    ImageGenerationPipeline,
    VideoGenerationPipeline,
    create_generation_result,
    mark_generation_failed,
    mark_generation_succeeded,
)


class Handler:
    async def _handle_image_generation(self, marker: str):
        yield f"image:{marker}"

    async def _handle_video_generation(self, marker: str):
        yield f"video:{marker}"


@pytest.mark.asyncio
async def test_explicit_generation_pipelines_delegate_and_stream_chunks() -> None:
    handler = Handler()
    image = [chunk async for chunk in ImageGenerationPipeline(handler).run("one")]
    video = [chunk async for chunk in VideoGenerationPipeline(handler).run("two")]

    assert image == ["image:one"]
    assert video == ["video:two"]


def test_generation_outcome_state_is_request_local() -> None:
    first = create_generation_result()
    second = create_generation_result()

    mark_generation_failed(first, "failed", status_code=502, error_extra={"phase": "poll"})
    assert first["error_status_code"] == 502
    assert second["error_message"] is None

    mark_generation_succeeded(first)
    assert first["success"] is True
    assert first["error_extra"] == {}
