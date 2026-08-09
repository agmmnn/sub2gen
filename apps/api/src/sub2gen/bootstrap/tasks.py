"""Lifecycle-aware registry for application background tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class TaskRegistry:
    """Own named tasks and shut them down deterministically."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tasks)

    def is_running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    def start(self, name: str, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        existing = self._tasks.get(name)
        if existing is not None and not existing.done():
            coroutine.close()
            raise RuntimeError(f"Background task already running: {name}")
        task = asyncio.create_task(coroutine, name=f"sub2gen-{name}")
        self._tasks[name] = task
        return task

    async def cancel(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def cancel_all(self) -> None:
        names = tuple(reversed(self.names))
        for name in names:
            await self.cancel(name)
