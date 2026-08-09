from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastapi import WebSocket

from sub2gen.workers.extension.generation import ExtensionGenerationJobs
from sub2gen.workers.extension.jobs import ExtensionJobBroker
from sub2gen.workers.extension.models import ExtensionConnection
from sub2gen.workers.extension.uploads import GenerationUploadStore


class RespondingWebSocket:
    def __init__(self, broker: ExtensionJobBroker) -> None:
        self.broker = broker
        self.messages: list[dict[str, Any]] = []

    async def send_text(self, raw: str) -> None:
        message = json.loads(raw)
        self.messages.append(message)
        status, future = self.broker.match_response("generation", message["req_id"], cast(WebSocket, cast(Any, self)))
        assert status == "matched" and future is not None
        future.set_result({"status": "success", "response_status": 200})


@pytest.mark.asyncio
async def test_generation_executor_owns_dispatch_and_upload_negotiation() -> None:
    broker = ExtensionJobBroker()
    uploads = GenerationUploadStore()
    executor = ExtensionGenerationJobs(broker, uploads)
    websocket = RespondingWebSocket(broker)
    connection = ExtensionConnection(websocket=cast(WebSocket, cast(Any, websocket)))

    result = await executor.execute(
        connection,
        message_type="submit_generation",
        request_payload={"url": "https://flow/upsampleImage", "json_data": {}},
        timeout=5,
        large_upload_enabled=True,
        upload_ttl_seconds=60,
        upload_max_bytes=1024,
        upload_threshold_bytes=512,
        force_upsample_upload=True,
    )

    assert result == {"status": "success", "response_status": 200}
    assert websocket.messages[0]["large_response_upload"]["threshold_bytes"] == 0
    assert websocket.messages[0]["large_response_upload"]["force_http_upload"] is True
    assert broker.pending_generation == {}
