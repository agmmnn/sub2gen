"""Canonical worker protocol v1 pairing and WebSocket transport."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sub2gen_worker_protocol.codec import ProtocolCodecError, decode_envelope, encode_envelope, make_envelope
from sub2gen_worker_protocol.generated import (
    JobDecisionPayload,
    JobErrorPayload,
    JobProgressPayload,
    JobResultPayload,
    MessageType,
    WorkerChallengePayload,
    WorkerHeartbeatPayload,
    WorkerHelloPayload,
    WorkerRegisterPayload,
    WorkerRegisteredPayload,
)
from sub2gen_worker_protocol.negotiation import ProtocolNegotiationError, negotiate_protocol
from sub2gen_worker_protocol.security import DeviceAuthError

from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container, get_websocket_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible

router = APIRouter()


class PairingCodeRequest(BaseModel):
    ttl_seconds: int = Field(default=300, ge=30, le=1800)


class PairWorkerRequest(BaseModel):
    pairing_code: str = Field(min_length=1)
    worker_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=160)
    public_key: str = Field(min_length=1, max_length=512)
    capabilities: tuple[str, ...] = Field(min_length=1, max_length=64)


class RevokeWorkerRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=160)


def _require_worker_admin(auth: AuthContext) -> None:
    if auth.is_legacy or "*" in auth.scopes or "workers:pair" in auth.scopes:
        return
    raise HTTPException(status_code=403, detail="Missing scope: workers:pair")


@router.post("/api/workers/pairing-code")
async def create_worker_pairing_code(
    body: PairingCodeRequest,
    auth: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    _require_worker_admin(auth)
    return {
        "pairing_code": container.worker_pairing.create_pairing_code(ttl_seconds=body.ttl_seconds),
        "expires_in": body.ttl_seconds,
    }


@router.post("/api/workers/pair")
async def pair_worker(
    body: PairWorkerRequest,
    container: AppContainer = Depends(get_container),
):
    try:
        worker = await container.worker_pairing.pair(
            code=body.pairing_code,
            worker_id=body.worker_id,
            kind=body.kind,
            label=body.label,
            public_key_base64=body.public_key,
            approved_capabilities=body.capabilities,
        )
    except DeviceAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "worker_id": worker.id,
        "kind": worker.kind,
        "label": worker.label,
        "approved_capabilities": worker.approved_capabilities,
        "credential_expires_at": worker.credential_expires_at,
    }


@router.post("/api/workers/revoke")
async def revoke_worker(
    body: RevokeWorkerRequest,
    auth: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    _require_worker_admin(auth)
    if not await container.worker_pairing.revoke(body.worker_id):
        raise HTTPException(status_code=404, detail="Worker not found or already revoked")
    container.worker_coordinator.disconnect(body.worker_id)
    return {"revoked": True, "worker_id": body.worker_id}


@router.websocket("/worker_ws")
async def worker_websocket_endpoint(websocket: WebSocket):
    container = get_websocket_container(websocket)
    await websocket.accept()
    worker_id = ""
    try:
        hello_envelope, hello_payload = decode_envelope(await websocket.receive_text())
        if hello_envelope.message_type is not MessageType.WORKER_HELLO or not isinstance(
            hello_payload, WorkerHelloPayload
        ):
            raise ProtocolCodecError("first frame must be worker.hello")
        worker_id = hello_envelope.worker_id
        negotiated = negotiate_protocol(hello_payload.supported_versions)
        if negotiated.legacy:
            raise ProtocolNegotiationError("legacy workers must use their existing endpoint")

        challenge_id, nonce, expires_at = await container.worker_pairing.issue_challenge(worker_id)
        challenge = make_envelope(
            message_type=MessageType.WORKER_CHALLENGE,
            worker_id=worker_id,
            correlation_id=hello_envelope.message_id,
            payload=WorkerChallengePayload(
                challenge_id=challenge_id,
                nonce=nonce,
                expires_at=expires_at,
            ),
        )
        await websocket.send_text(encode_envelope(challenge))

        register_envelope, register_payload = decode_envelope(await websocket.receive_text())
        if register_envelope.message_type is not MessageType.WORKER_REGISTER or not isinstance(
            register_payload, WorkerRegisterPayload
        ):
            raise ProtocolCodecError("second frame must be worker.register")
        if register_envelope.worker_id != worker_id or register_payload.challenge_id != challenge_id:
            raise ProtocolCodecError("worker registration does not match challenge")
        await container.worker_pairing.authenticate(
            worker_id=worker_id,
            challenge_id=challenge_id,
            signature=register_payload.signature,
        )
        device = await container.repositories.workers.devices.get_for_auth(worker_id)
        if device is None:
            raise DeviceAuthError("device credential is unavailable")
        container.worker_coordinator.register(
            worker_id=worker_id,
            worker_session_id=register_payload.worker_session_id,
            capabilities=register_payload.capabilities,
            approved_capabilities=set(device.approved_capabilities),
            available_slots=1,
        )
        registered = make_envelope(
            message_type=MessageType.WORKER_REGISTERED,
            worker_id=worker_id,
            correlation_id=register_envelope.message_id,
            payload=WorkerRegisteredPayload(
                selected_version="1.0",
                worker_session_id=register_payload.worker_session_id,
                lease_seconds=30,
            ),
        )
        await websocket.send_text(encode_envelope(registered))

        while True:
            envelope, payload = decode_envelope(await websocket.receive_text())
            if envelope.worker_id != worker_id:
                raise ProtocolCodecError("worker identity changed during a session")
            _handle_worker_message(container, envelope, payload)
            if envelope.message_type is MessageType.WORKER_HEARTBEAT:
                await container.repositories.workers.touch_device_heartbeat(worker_id)
    except WebSocketDisconnect:
        pass
    except (ProtocolCodecError, ProtocolNegotiationError, DeviceAuthError, RuntimeError) as exc:
        await websocket.close(code=1008, reason=str(exc)[:120])
    finally:
        if worker_id:
            container.worker_coordinator.disconnect(worker_id)


def _handle_worker_message(container: AppContainer, envelope: Any, payload: Any) -> None:
    coordinator = container.worker_coordinator
    if envelope.message_type is MessageType.WORKER_HEARTBEAT and isinstance(payload, WorkerHeartbeatPayload):
        coordinator.heartbeat(envelope.worker_id, payload)
    elif envelope.message_type is MessageType.JOB_ACCEPT and isinstance(payload, JobDecisionPayload):
        coordinator.accept(payload.lease_id, worker_id=envelope.worker_id)
    elif envelope.message_type is MessageType.JOB_REJECT and isinstance(payload, JobDecisionPayload):
        coordinator.leases.reject(payload.lease_id, worker_id=envelope.worker_id)
    elif envelope.message_type is MessageType.JOB_PROGRESS and isinstance(payload, JobProgressPayload):
        coordinator.progress(worker_id=envelope.worker_id, job_id=envelope.job_id or "", payload=payload)
    elif envelope.message_type is MessageType.JOB_RESULT and isinstance(payload, JobResultPayload):
        coordinator.result(worker_id=envelope.worker_id, job_id=envelope.job_id or "", payload=payload)
    elif envelope.message_type is MessageType.JOB_ERROR and isinstance(payload, JobErrorPayload):
        coordinator.error(worker_id=envelope.worker_id, job_id=envelope.job_id or "", payload=payload)
    else:
        raise ProtocolCodecError("message type is not valid in the registered worker state")
