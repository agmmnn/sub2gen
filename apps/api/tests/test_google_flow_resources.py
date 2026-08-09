from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sub2gen_provider_google_flow import (
    FlowAuthResource,
    FlowImagesResource,
    FlowMediaResource,
    FlowModelResource,
    FlowProjectsResource,
    FlowTransport,
    FlowVideosResource,
)
from sub2gen.services.flow_client import FlowClient


def test_flow_client_composes_capability_resources() -> None:
    client = FlowClient(proxy_manager=None)

    assert isinstance(client.transport, FlowTransport)
    assert isinstance(client.auth, FlowAuthResource)
    assert isinstance(client.projects, FlowProjectsResource)
    assert isinstance(client.media, FlowMediaResource)
    assert isinstance(client.images, FlowImagesResource)
    assert isinstance(client.videos, FlowVideosResource)
    assert isinstance(client.models, FlowModelResource)


@pytest.mark.asyncio
async def test_auth_project_and_media_calls_use_shared_transport() -> None:
    client = FlowClient(proxy_manager=None)
    request = AsyncMock(
        side_effect=[
            {"access_token": "at"},
            {"credits": 12},
            {"result": {"data": {"json": {"result": {"projectId": "project-1"}}}}},
            {},
            {"name": "media-1"},
        ]
    )
    client.transport.request = request

    assert await client.st_to_at("st") == {"access_token": "at"}
    assert await client.get_credits("at") == {"credits": 12}
    assert await client.create_project("st", "Title") == "project-1"
    await client.delete_project("st", "project-1")
    assert await client.get_media("at", "media/1") == {"name": "media-1"}
    assert request.await_count == 5


@pytest.mark.asyncio
async def test_image_and_video_facades_dispatch_to_implementation_seams() -> None:
    client = FlowClient(proxy_manager=None)
    with (
        patch.object(client, "_generate_image_impl", new=AsyncMock(return_value={"image": True})) as image,
        patch.object(
            client,
            "_generate_video_text_impl",
            new=AsyncMock(return_value={"video": True}),
        ) as video,
    ):
        assert await client.generate_image("at", "project", "prompt") == {"image": True}
        assert await client.generate_video_text("at", "project", "prompt") == {"video": True}
        image.assert_awaited_once()
        video.assert_awaited_once()


def test_model_resource_preserves_identifiers_and_mime_detection() -> None:
    models = FlowModelResource()

    assert models.generate_session_id().startswith(";")
    assert models.generate_scene_id()
    assert models.detect_image_mime_type(b"\x89PNG" + b"x" * 12) == "image/png"
    assert models.detect_image_mime_type(b"GIF89a" + b"x" * 12) == "image/gif"
