"""
WebSocket for outbound agents. First message must be register.
Supports auth modes:
- legacy: shared device_token
- keygen: agent_token
- dual: either path accepted
"""
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket
from fastapi import status as http_status
from starlette.websockets import WebSocketDisconnect

from .auth_keygen import verify_agent_token
from .config import load_settings
from .schemas import AgentIdentity, WsRegister
from .state import registry

router = APIRouter()
logger = logging.getLogger(__name__)


def _ws_reason(text: str, max_bytes: int = 120) -> str:
    """WebSocket close reason must fit control frame limits (<=123 bytes in practice)."""
    raw = (text or "").strip()
    if not raw:
        return "policy violation"
    encoded = raw.encode("utf-8")
    if len(encoded) <= max_bytes:
        return raw
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8", errors="strict").rstrip()
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "policy violation"


async def _resolve_identity(data: dict[str, Any], s) -> AgentIdentity:
    if s.agent_auth_mode in {"legacy", "dual"} and str(data.get("device_token") or ""):
        if data.get("device_token") != s.agent_device_token:
            raise PermissionError("invalid device token")
        return AgentIdentity(auth_method="legacy", subject="legacy-shared-token")
    if s.agent_auth_mode == "legacy":
        raise PermissionError("legacy mode requires device_token")
    agent_token = str(data.get("agent_token") or data.get("license_token") or "").strip()
    if not agent_token:
        raise PermissionError("agent_token required")
    agent_token_id = str(data.get("agent_token_id") or data.get("license_token_id") or "").strip()
    identity = await verify_agent_token(agent_token, agent_token_id, s)
    return AgentIdentity(
        auth_method="keygen",
        subject=identity.subject,
        machine_id=identity.machine_id,
        license_id=identity.license_id,
        account_id=identity.account_id,
    )


@router.websocket("/ws/agents")
async def ws_agents(websocket: WebSocket) -> None:
    await websocket.accept()
    s = load_settings()
    if s.agent_auth_mode in {"legacy", "dual"} and not s.agent_device_token:
        await websocket.close(code=4500, reason="GATEWAY_AGENT_DEVICE_TOKEN is not set")
        return

    first = await websocket.receive_text()
    try:
        data = WsRegister.model_validate_json(first).model_dump()
    except Exception:
        await websocket.close(
            code=http_status.WS_1008_POLICY_VIOLATION,
            reason="expected JSON",
        )
        return

    if data.get("type") != "register":
        await websocket.close(
            code=http_status.WS_1008_POLICY_VIOLATION,
            reason="first message must be register",
        )
        return
    try:
        identity = await _resolve_identity(data, s)
    except PermissionError as e:
        await websocket.close(
            code=http_status.WS_1008_POLICY_VIOLATION,
            reason=_ws_reason(str(e)),
        )
        return
    except Exception as e:
        await websocket.close(
            code=http_status.WS_1008_POLICY_VIOLATION,
            reason=_ws_reason(f"agent auth failed: {e}"),
        )
        return

    await registry.register_agent(
        websocket,
        auth_method=identity.auth_method,
        subject=identity.subject,
        machine_id=identity.machine_id,
        license_id=identity.license_id,
        account_id=identity.account_id,
    )
    try:
        await websocket.send_json(
            {
                "type": "registered",
                "subject": identity.subject,
                "auth_method": identity.auth_method,
            }
        )
    except Exception:
        await registry.unregister(websocket)
        return

    try:
        while True:
            text = await websocket.receive_text()
            try:
                msg: dict[str, Any] = json.loads(text)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "invalid JSON"})
                continue

            mtype = msg.get("type")
            if mtype == "solve_result":
                job_id = msg.get("job_id")
                if not job_id:
                    continue
                await registry.complete_job(
                    str(job_id),
                    {
                        "token": msg.get("token"),
                        "session_id": msg.get("session_id"),
                        "fingerprint": msg.get("fingerprint"),
                    },
                )
            elif mtype == "solve_error":
                job_id = msg.get("job_id")
                err = str(msg.get("error") or "agent_error")
                if job_id:
                    await registry.fail_job(str(job_id), err)
            else:
                await websocket.send_json(
                    {"type": "error", "detail": f"unknown type {mtype!r}"}
                )
    except WebSocketDisconnect as e:
        logger.info("ws agent disconnected code=%s reason=%s", e.code, e.reason or "")
    except Exception:
        logger.exception("ws agent loop")
    finally:
        await registry.unregister(websocket)
