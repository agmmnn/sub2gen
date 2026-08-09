from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import WebSocket

from sub2gen.workers.extension.models import ExtensionConnection
from sub2gen.workers.extension.registry import ExtensionConnectionRegistry


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def close(self, **_kwargs: Any) -> None:
        self.closed = True


def make_connection(websocket: FakeWebSocket, instance_id: str) -> ExtensionConnection:
    return ExtensionConnection(
        websocket=cast(WebSocket, cast(Any, websocket)),
        instance_id=instance_id,
    )


@pytest.mark.asyncio
async def test_registry_replaces_matching_instance_and_notifies_disconnect() -> None:
    registry = ExtensionConnectionRegistry()
    first_socket = FakeWebSocket()
    first = make_connection(first_socket, "shared")
    await registry.add(first)
    disconnected: list[Any] = []

    await registry.replace_instance(
        make_connection(FakeWebSocket(), "shared"),
        disconnect=disconnected.append,
    )

    assert first_socket.closed is True
    assert disconnected == [first.websocket]


@pytest.mark.asyncio
async def test_registry_owns_waiter_accounting() -> None:
    registry = ExtensionConnectionRegistry()

    async with registry.waiting("key:7"):
        assert registry.waiters == {"key:7": 1}

    assert registry.waiters == {}
