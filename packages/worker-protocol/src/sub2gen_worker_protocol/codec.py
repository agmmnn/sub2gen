"""Strict JSON codec for worker protocol v1."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from .generated import (
    Envelope,
    JobCancelPayload,
    JobCancelledPayload,
    JobDecisionPayload,
    JobErrorPayload,
    JobOfferPayload,
    JobProgressPayload,
    JobResultPayload,
    MessageType,
    WorkerCapabilitiesPayload,
    WorkerChallengePayload,
    WorkerHeartbeatPayload,
    WorkerHelloPayload,
    WorkerRegisterPayload,
    WorkerRegisteredPayload,
)

PayloadModel = type[BaseModel]

PAYLOAD_MODELS: dict[MessageType, PayloadModel] = {
    MessageType.WORKER_HELLO: WorkerHelloPayload,
    MessageType.WORKER_CHALLENGE: WorkerChallengePayload,
    MessageType.WORKER_REGISTER: WorkerRegisterPayload,
    MessageType.WORKER_REGISTERED: WorkerRegisteredPayload,
    MessageType.WORKER_CAPABILITIES: WorkerCapabilitiesPayload,
    MessageType.WORKER_HEARTBEAT: WorkerHeartbeatPayload,
    MessageType.JOB_OFFER: JobOfferPayload,
    MessageType.JOB_ACCEPT: JobDecisionPayload,
    MessageType.JOB_REJECT: JobDecisionPayload,
    MessageType.JOB_PROGRESS: JobProgressPayload,
    MessageType.JOB_RESULT: JobResultPayload,
    MessageType.JOB_ERROR: JobErrorPayload,
    MessageType.JOB_CANCEL: JobCancelPayload,
    MessageType.JOB_CANCELLED: JobCancelledPayload,
}


class ProtocolCodecError(ValueError):
    pass


def validate_envelope(value: Envelope | dict[str, Any]) -> tuple[Envelope, BaseModel]:
    try:
        envelope = value if isinstance(value, Envelope) else Envelope.model_validate(value)
        payload = PAYLOAD_MODELS[envelope.message_type].model_validate(envelope.payload)
    except (KeyError, ValidationError, TypeError, ValueError) as exc:
        raise ProtocolCodecError("invalid worker protocol v1 envelope") from exc
    return envelope, payload


def decode_envelope(frame: str | bytes) -> tuple[Envelope, BaseModel]:
    try:
        value = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ProtocolCodecError("invalid worker protocol JSON frame") from exc
    if not isinstance(value, dict):
        raise ProtocolCodecError("worker protocol frame must be an object")
    return validate_envelope(value)


def encode_envelope(envelope: Envelope) -> str:
    validate_envelope(envelope)
    return json.dumps(
        envelope.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def make_envelope(
    *,
    message_type: MessageType,
    worker_id: str,
    payload: BaseModel | dict[str, Any],
    correlation_id: str | None = None,
    job_id: str | None = None,
    job_kind: str | None = None,
    message_id: str | None = None,
    sent_at: datetime | None = None,
) -> Envelope:
    payload_value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    envelope = Envelope(
        message_id=message_id or f"msg_{uuid4().hex}",
        message_type=message_type,
        correlation_id=correlation_id,
        job_id=job_id,
        job_kind=job_kind,
        worker_id=worker_id,
        sent_at=sent_at or datetime.now(timezone.utc),
        payload=payload_value,
    )
    validate_envelope(envelope)
    return envelope
