from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastapi import WebSocket

from sub2gen.workers.extension.jobs import ExtensionJobBroker
from sub2gen.workers.extension.models import ExtensionConnection
from sub2gen.workers.extension.refresh import ExtensionRefreshJobs


class RefreshWebSocket:
    def __init__(self, broker: ExtensionJobBroker) -> None:
        self.broker = broker
        self.message: dict[str, Any] | None = None

    async def send_text(self, raw: str) -> None:
        self.message = json.loads(raw)
        status, future = self.broker.match_response(
            "captcha",
            self.message["req_id"],
            cast(WebSocket, cast(Any, self)),
        )
        assert status == "matched" and future is not None
        future.set_result({"status": "success", "session_token": " refreshed-session "})


@pytest.mark.asyncio
async def test_refresh_executor_dispatches_and_cleans_future() -> None:
    broker = ExtensionJobBroker()
    executor = ExtensionRefreshJobs(broker)
    websocket = RefreshWebSocket(broker)
    connection = ExtensionConnection(websocket=cast(WebSocket, cast(Any, websocket)))

    result = await executor.execute(connection, token_id=42, timeout=5)

    assert result == "refreshed-session"
    assert websocket.message is not None
    assert websocket.message["type"] == "refresh_st"
    assert websocket.message["token_id"] == 42
    assert broker.pending_captcha == {}
