"""Compatibility translators for the two frozen unversioned worker dialects."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .codec import make_envelope
from .generated import (
    JobErrorPayload,
    JobOfferPayload,
    JobResultPayload,
    MessageType,
    StructuredError,
)


class LegacyCompatibilityError(ValueError):
    pass


def legacy_extension_server_frame_to_v1(message: dict[str, Any], *, worker_id: str):
    message_type = message.get("type")
    request_id = str(message.get("req_id") or "")
    if message_type == "get_token":
        capability, job_kind = "captcha.solve", "captcha.solve"
    elif message_type == "refresh_st":
        capability, job_kind = "session.refresh", "session.refresh"
    elif message_type in {"submit_generation", "poll_generation"}:
        capability, job_kind = "image.generate:google-flow", "image.generate"
    else:
        raise LegacyCompatibilityError("legacy extension frame is not a job offer")
    payload = JobOfferPayload(
        attempt=1,
        lease_id=f"legacy:{request_id}",
        capability=capability,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        input={"legacy_message_type": message_type, "legacy_payload": dict(message)},
    )
    return make_envelope(
        message_type=MessageType.JOB_OFFER,
        worker_id=worker_id,
        payload=payload,
        correlation_id=request_id,
        job_id=request_id,
        job_kind=job_kind,
    )


def legacy_gateway_server_frame_to_v1(message: dict[str, Any], *, worker_id: str):
    if message.get("type") != "solve_job":
        raise LegacyCompatibilityError("legacy gateway frame is not a solve job")
    job_id = str(message.get("job_id") or "")
    payload = JobOfferPayload(
        attempt=1,
        lease_id=f"legacy:{job_id}",
        capability="captcha.solve",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        input={"legacy_payload": dict(message)},
    )
    return make_envelope(
        message_type=MessageType.JOB_OFFER,
        worker_id=worker_id,
        payload=payload,
        correlation_id=job_id,
        job_id=job_id,
        job_kind="captcha.solve",
    )


def v1_terminal_to_legacy_extension(envelope, payload: JobResultPayload | JobErrorPayload) -> dict[str, Any]:
    if isinstance(payload, JobResultPayload):
        return {"req_id": envelope.job_id, "status": "success", **payload.output}
    if isinstance(payload, JobErrorPayload):
        return {"req_id": envelope.job_id, "status": "error", "error": payload.error.message}
    raise LegacyCompatibilityError("v1 frame is not terminal")


def v1_terminal_to_legacy_gateway(envelope, payload: JobResultPayload | JobErrorPayload) -> dict[str, Any]:
    if isinstance(payload, JobResultPayload):
        return {"type": "solve_result", "job_id": envelope.job_id, **payload.output}
    if isinstance(payload, JobErrorPayload):
        return {"type": "solve_error", "job_id": envelope.job_id, "error": payload.error.message}
    raise LegacyCompatibilityError("v1 frame is not terminal")


def legacy_error(code: str, message: str, *, retryable: bool = False) -> JobErrorPayload:
    return JobErrorPayload(
        attempt=1,
        lease_id="legacy",
        error=StructuredError(code=code, message=message, retryable=retryable),
    )
