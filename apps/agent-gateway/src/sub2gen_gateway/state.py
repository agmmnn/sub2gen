"""In-memory agent registry: connected WebSocket pool for solve dispatch."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class PendingSolve:
    job_id: str
    future: asyncio.Future[dict[str, Any]]
    project_id: str
    action: str
    agent_ws: WebSocket


@dataclass
class AgentBinding:
    ws: WebSocket
    auth_method: str
    subject: str
    machine_id: str = ""
    license_id: str = ""
    account_id: str = ""
    connected_at: float = 0.0


class AgentRegistry:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._ws_binding: dict[int, AgentBinding] = {}
        self._pending: dict[str, Any] = {}
        self._rr_cursor = 0

    async def register_agent(
        self,
        ws: WebSocket,
        *,
        auth_method: str,
        subject: str,
        machine_id: str = "",
        license_id: str = "",
        account_id: str = "",
    ) -> None:
        async with self._lock:
            wid = id(ws)
            binding = AgentBinding(
                ws=ws,
                auth_method=auth_method,
                subject=subject,
                machine_id=machine_id,
                license_id=license_id,
                account_id=account_id,
                connected_at=time.time(),
            )
            self._ws_binding[wid] = binding
            logger.info(
                "agent registered auth=%s subject=%s machine=%s license=%s",
                auth_method,
                subject,
                binding.machine_id,
                binding.license_id,
            )

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            wid = id(ws)
            binding = self._ws_binding.pop(wid, None)
            for jid, p in list(self._pending.items()):
                if p.agent_ws is ws and not p.future.done():
                    p.future.set_exception(RuntimeError("agent_disconnected"))
                    self._pending.pop(jid, None)
            logger.info(
                "agent unregistered subject=%s",
                (binding.subject if binding else ""),
            )

    async def dispatch_solve(
        self,
        project_id: str,
        action: str,
        timeout: float,
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        async with self._lock:
            bindings = list(self._ws_binding.values())
            if not bindings:
                raise LookupError("no_agent")
            idx = self._rr_cursor % len(bindings)
            binding = bindings[idx]
            self._rr_cursor = (idx + 1) % len(bindings)
            ws = binding.ws
            p = PendingSolve(
                job_id=job_id,
                future=fut,
                project_id=project_id,
                action=action,
                agent_ws=ws,
            )
            self._pending[job_id] = p

        msg: dict[str, Any] = {
            "type": "solve_job",
            "job_id": job_id,
            "project_id": project_id,
            "action": action,
        }
        try:
            await ws.send_json(msg)
        except Exception as e:
            async with self._lock:
                self._pending.pop(job_id, None)
            if not fut.done():
                fut.set_exception(e)
            raise

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                self._pending.pop(job_id, None)
            raise
        except Exception:
            async with self._lock:
                self._pending.pop(job_id, None)
            raise

    async def complete_job(self, job_id: str, result: dict[str, Any]) -> bool:
        async with self._lock:
            p = self._pending.pop(job_id, None)
        if p is None:
            return False
        if not p.future.done():
            p.future.set_result(result)
        return True

    async def fail_job(self, job_id: str, err: str) -> bool:
        async with self._lock:
            p = self._pending.pop(job_id, None)
        if p is None:
            return False
        if not p.future.done():
            p.future.set_exception(RuntimeError(err))
        return True

    async def list_agents(self) -> list[dict[str, Any]]:
        async with self._lock:
            out: list[dict[str, Any]] = []
            for binding in self._ws_binding.values():
                out.append(
                    {
                        "auth_method": binding.auth_method,
                        "subject": binding.subject,
                        "machine_id": binding.machine_id,
                        "license_id": binding.license_id,
                        "account_id": binding.account_id,
                        "connected_at": binding.connected_at,
                    }
                )
            out.sort(key=lambda item: float(item.get("connected_at") or 0.0), reverse=True)
            return out


registry = AgentRegistry()
