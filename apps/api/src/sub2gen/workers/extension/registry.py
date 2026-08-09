"""Connection ownership and waiter coordination for extension workers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import WebSocket

from .models import ExtensionConnection


class ExtensionConnectionRegistry:
    def __init__(self) -> None:
        self.connections: list[ExtensionConnection] = []
        self.changed = asyncio.Condition()
        self.waiters: dict[str, int] = {}
        self.managed_round_robin: dict[str, int] = {}
        self.state_lock = asyncio.Lock()

    def find(self, websocket: WebSocket) -> ExtensionConnection | None:
        return next(
            (connection for connection in self.connections if connection.websocket is websocket),
            None,
        )

    async def replace_instance(
        self,
        connection: ExtensionConnection,
        *,
        disconnect: Any,
    ) -> None:
        if not connection.instance_id:
            return
        replaced = [existing for existing in list(self.connections) if existing.instance_id == connection.instance_id]
        for existing in replaced:
            try:
                await existing.websocket.close(code=1000, reason="Replaced by reconnect")
            except Exception:
                pass
            disconnect(existing.websocket)

    async def add(self, connection: ExtensionConnection) -> None:
        self.connections.append(connection)
        await self.notify_changed()

    def remove(self, websocket: WebSocket) -> ExtensionConnection | None:
        connection = self.find(websocket)
        if connection is not None:
            self.connections.remove(connection)
        return connection

    async def notify_changed(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    async def wait_for_change(self, timeout: float) -> None:
        async with self.changed:
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    @asynccontextmanager
    async def waiting(self, queue_key: str) -> AsyncIterator[None]:
        await self.begin_wait(queue_key)
        try:
            yield
        finally:
            await self.end_wait(queue_key)

    async def begin_wait(self, queue_key: str) -> None:
        async with self.state_lock:
            self.waiters[queue_key] = self.waiters.get(queue_key, 0) + 1

    async def end_wait(self, queue_key: str) -> None:
        async with self.state_lock:
            remaining = self.waiters.get(queue_key, 0) - 1
            if remaining <= 0:
                self.waiters.pop(queue_key, None)
            else:
                self.waiters[queue_key] = remaining

    def clear_managed_cursor_if_unused(self, managed_api_key_id: int) -> None:
        if any(
            connection.managed_api_key_id is not None and int(connection.managed_api_key_id) == int(managed_api_key_id)
            for connection in self.connections
        ):
            return
        self.managed_round_robin.pop(f"key:{managed_api_key_id}", None)
