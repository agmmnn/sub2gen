from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import WebSocket

from sub2gen.workers.extension.jobs import ExtensionJobBroker


def websocket() -> WebSocket:
    return cast(WebSocket, cast(Any, object()))


@pytest.mark.asyncio
async def test_job_broker_rejects_response_from_non_owner() -> None:
    broker = ExtensionJobBroker()
    owner = websocket()
    other = websocket()
    future = broker.register("captcha", "request-1", owner)

    status, matched = broker.match_response("captcha", "request-1", other)

    assert status == "wrong_owner"
    assert matched is future
    assert future.done() is False


@pytest.mark.asyncio
async def test_job_broker_fails_generation_when_owner_disconnects() -> None:
    broker = ExtensionJobBroker()
    owner = websocket()
    future = broker.register("generation", "request-2", owner)

    broker.disconnect(owner)

    with pytest.raises(RuntimeError, match="Extension worker disconnected"):
        await future
    assert "request-2" not in broker.pending_generation


@pytest.mark.asyncio
async def test_job_broker_owns_verdict_and_one_time_user_agent_state() -> None:
    broker = ExtensionJobBroker()
    owner = websocket()
    broker.capture_user_agent("request-3", "Browser/1")
    await broker.bind_upstream_verdict("request-3", owner)

    assert broker.consume_user_agent("request-3") == "Browser/1"
    assert broker.consume_user_agent("request-3") is None
    assert await broker.take_upstream_verdict("request-3") is owner
    assert await broker.take_upstream_verdict("request-3") is None
