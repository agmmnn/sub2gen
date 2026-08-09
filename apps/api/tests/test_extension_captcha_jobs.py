from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastapi import WebSocket

from sub2gen.workers.extension.captcha import ExtensionCaptchaJobs
from sub2gen.workers.extension.jobs import ExtensionJobBroker
from sub2gen.workers.extension.models import ExtensionConnection
from sub2gen.workers.extension.routing import ExtensionWorkerRouting


class CaptchaWebSocket:
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
        future.set_result(
            {
                "status": "success",
                "token": " solved-token ",
                "user_agent": " Solver-UA/1 ",
            }
        )


@pytest.mark.asyncio
async def test_captcha_executor_owns_dispatch_health_and_solver_metadata() -> None:
    broker = ExtensionJobBroker()
    routing = ExtensionWorkerRouting()
    executor = ExtensionCaptchaJobs(broker, routing)
    websocket = CaptchaWebSocket(broker)
    connection = ExtensionConnection(
        websocket=cast(WebSocket, cast(Any, websocket)),
        refresh_token_id=42,
    )

    token, request_id = await executor.execute(
        connection,
        project_id="project-1",
        action="IMAGE_GENERATION",
        managed_api_key_id=9,
        timeout=5,
        selection_meta={"dedicated_hybrid": True},
    )

    assert token == "solved-token"
    assert request_id is not None
    assert websocket.message is not None
    assert websocket.message["type"] == "get_token"
    assert websocket.message["project_id"] == "project-1"
    assert broker.consume_user_agent(request_id) == "Solver-UA/1"
    assert await broker.take_upstream_verdict(request_id) is connection.websocket
    assert broker.pending_captcha == {}
    stats = routing.stats(connection.worker_session_id)
    assert stats.inflight_count == 0
    assert stats.success_count == 1


@pytest.mark.asyncio
async def test_captcha_executor_records_worker_timeout_and_cleans_future() -> None:
    broker = ExtensionJobBroker()
    routing = ExtensionWorkerRouting()
    executor = ExtensionCaptchaJobs(broker, routing)

    class SilentWebSocket:
        async def send_text(self, _raw: str) -> None:
            return None

    connection = ExtensionConnection(
        websocket=cast(WebSocket, cast(Any, SilentWebSocket())),
        captcha_worker_id=3,
    )

    token, request_id = await executor.execute(
        connection,
        project_id="project-1",
        action="IMAGE_GENERATION",
        managed_api_key_id=None,
        timeout=0,
    )

    assert token is None
    assert request_id is None
    assert broker.pending_captcha == {}
    stats = routing.stats(connection.worker_session_id)
    assert stats.inflight_count == 0
    assert len(stats.timeout_timestamps) == 1
    assert stats.cooldown_until > 0
