"""Pending extension job futures and response ownership."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import WebSocket


JobKind = Literal["captcha", "generation"]
ResponseMatch = Literal["missing", "wrong_owner", "matched"]


class ExtensionJobBroker:
    def __init__(self) -> None:
        self.pending_captcha: dict[str, tuple[asyncio.Future[Any], WebSocket]] = {}
        self.pending_generation: dict[str, tuple[asyncio.Future[Any], WebSocket]] = {}
        self.upstream_verdict_targets: dict[str, WebSocket] = {}
        self.token_user_agents: dict[str, str] = {}
        self.lock = asyncio.Lock()

    def _pending(self, kind: JobKind) -> dict[str, tuple[asyncio.Future[Any], WebSocket]]:
        return self.pending_generation if kind == "generation" else self.pending_captcha

    def register(self, kind: JobKind, request_id: str, websocket: WebSocket) -> asyncio.Future[Any]:
        future = asyncio.get_running_loop().create_future()
        self._pending(kind)[request_id] = (future, websocket)
        return future

    def remove(self, kind: JobKind, request_id: str) -> None:
        self._pending(kind).pop(request_id, None)

    def match_response(
        self,
        kind: JobKind,
        request_id: str,
        websocket: WebSocket,
    ) -> tuple[ResponseMatch, asyncio.Future[Any] | None]:
        pending = self._pending(kind).get(request_id)
        if pending is None:
            return "missing", None
        future, owner = pending
        if owner is not websocket:
            return "wrong_owner", future
        return "matched", future

    async def bind_upstream_verdict(self, request_id: str, websocket: WebSocket) -> None:
        async with self.lock:
            self.upstream_verdict_targets[request_id] = websocket

    async def take_upstream_verdict(self, request_id: str) -> WebSocket | None:
        async with self.lock:
            websocket = self.upstream_verdict_targets.pop(request_id, None)
            self.token_user_agents.pop(request_id, None)
            return websocket

    async def abandon_upstream_verdict(self, request_id: str) -> None:
        async with self.lock:
            self.upstream_verdict_targets.pop(request_id, None)
            self.token_user_agents.pop(request_id, None)

    def capture_user_agent(self, request_id: str, user_agent: str) -> None:
        self.token_user_agents[request_id] = user_agent

    def consume_user_agent(self, request_id: str) -> str | None:
        return self.token_user_agents.pop(request_id, None)

    def disconnect(self, websocket: WebSocket) -> None:
        stale_verdicts = [
            request_id for request_id, owner in self.upstream_verdict_targets.items() if owner is websocket
        ]
        for request_id in stale_verdicts:
            self.upstream_verdict_targets.pop(request_id, None)
            self.token_user_agents.pop(request_id, None)

        stale_generation = [
            request_id for request_id, (_future, owner) in self.pending_generation.items() if owner is websocket
        ]
        for request_id in stale_generation:
            future, _owner = self.pending_generation.pop(request_id)
            if not future.done():
                try:
                    future.set_exception(RuntimeError("Extension worker disconnected"))
                except Exception:
                    pass
