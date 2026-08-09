from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from sub2gen.api import routes
from sub2gen.core.api_key_manager import AuthContext
from sub2gen.core.models import ChatCompletionRequest, ChatMessage


MODEL = "gemini-3.0-pro-image-landscape"
PROJECT_ID = "project-contract"


class CharacterizedGenerationHandler:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def handle_generation(self, **kwargs: Any):
        self.calls.append(kwargs)
        if kwargs["stream"]:
            yield 'data: {"id":"chatcmpl-contract","choices":[{"index":0,"delta":{"content":"ready"}}]}\n\n'
            return
        yield json.dumps(
            {
                "id": "chatcmpl-contract",
                "object": "chat.completion",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ready"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )


def make_auth() -> AuthContext:
    return AuthContext(
        key_id=42,
        key_label="contract-key",
        is_legacy=False,
        allowed_accounts={7, 8},
        scopes={"generate:chat"},
    )


def make_raw_request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"host", b"sub2gen.test")],
            "server": ("sub2gen.test", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


def make_normalized_request() -> routes.NormalizedGenerationRequest:
    return routes.NormalizedGenerationRequest(
        model=MODEL,
        prompt="Characterization prompt",
        images=[],
        messages=[ChatMessage(role="user", content="Characterization prompt")],
        project_id=PROJECT_ID,
    )


async def collect_stream(response: StreamingResponse) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else bytes(chunk).decode())
    return "".join(chunks)


def test_chat_completion_non_stream_contract() -> None:
    handler = CharacterizedGenerationHandler()
    request = ChatCompletionRequest(
        model=MODEL,
        messages=[ChatMessage(role="user", content="Characterization prompt")],
        project_id=PROJECT_ID,
    )

    container = SimpleNamespace(generation_handler=handler)
    with (
        patch.object(
            routes,
            "_normalize_openai_request",
            AsyncMock(return_value=make_normalized_request()),
        ),
        patch.object(
            routes,
            "_select_generation_target",
            AsyncMock(return_value=({7}, PROJECT_ID)),
        ),
    ):
        response = asyncio.run(
            routes.create_chat_completion(request, make_raw_request(), make_auth(), container)
        )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {
        "id": "chatcmpl-contract",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ready"},
                "finish_reason": "stop",
            }
        ],
        "project_id": PROJECT_ID,
    }
    assert handler.calls == [
        {
            "model": MODEL,
            "prompt": "Characterization prompt",
            "images": None,
            "stream": False,
            "base_url_override": "https://sub2gen.test",
            "allowed_token_ids": {7},
            "selection_context": {
                "allowlist_filter_reason_type": "project_pin",
                "key_allowed_account_ids": [7, 8],
                "effective_allowed_token_ids": [7],
                "selected_project_id": PROJECT_ID,
            },
            "api_key_id": 42,
            "requested_project_id": PROJECT_ID,
            "video_media_id": None,
            "poll_task_id": None,
        }
    ]


def test_chat_completion_stream_contract() -> None:
    handler = CharacterizedGenerationHandler()
    request = ChatCompletionRequest(
        model=MODEL,
        messages=[ChatMessage(role="user", content="Characterization prompt")],
        project_id=PROJECT_ID,
        stream=True,
    )

    async def run() -> tuple[StreamingResponse, str]:
        response = await routes.create_chat_completion(
            request, make_raw_request(), make_auth(), container
        )
        assert isinstance(response, StreamingResponse)
        return response, await collect_stream(response)

    container = SimpleNamespace(generation_handler=handler)
    with (
        patch.object(
            routes,
            "_normalize_openai_request",
            AsyncMock(return_value=make_normalized_request()),
        ),
        patch.object(
            routes,
            "_select_generation_target",
            AsyncMock(return_value=({7}, PROJECT_ID)),
        ),
    ):
        response, body = asyncio.run(run())

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    assert body == (
        'data: {"id": "chatcmpl-contract", "choices": [{"index": 0, '
        '"delta": {"content": "ready"}}], "project_id": "project-contract"}\n\n'
        "data: [DONE]\n\n"
    )
    assert handler.calls[0]["stream"] is True
    assert handler.calls[0]["allowed_token_ids"] == {7}
    assert handler.calls[0]["requested_project_id"] == PROJECT_ID
