"""Executable codec for the unversioned extension worker dialect.

This module freezes legacy framing for compatibility tests. Protocol v1 will replace
it with generated schemas; new runtime features must not extend this dialect.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal


LegacyDirection = Literal["server_to_worker", "worker_to_server"]

_SERVER_MESSAGE_TYPES = frozenset(
    {
        "register_ack",
        "get_token",
        "captcha_upstream_verdict",
        "refresh_st",
        "submit_generation",
        "poll_generation",
    }
)
_WORKER_MESSAGE_TYPES = frozenset(
    {
        "register",
        "ping",
        "client_shutdown",
        "submit_generation_result",
        "poll_generation_result",
    }
)


class LegacyExtensionCodecError(ValueError):
    """Raised when a frame is not valid for the frozen legacy dialect."""


def _validate_message(message: dict[str, Any], direction: LegacyDirection) -> dict[str, Any]:
    message_type = message.get("type")
    if message_type is None:
        if direction != "worker_to_server" or not {"req_id", "status"} <= message.keys():
            raise LegacyExtensionCodecError("untyped frames require worker req_id and status")
        return message
    if not isinstance(message_type, str):
        raise LegacyExtensionCodecError("message type must be a string")
    allowed = _SERVER_MESSAGE_TYPES if direction == "server_to_worker" else _WORKER_MESSAGE_TYPES
    if message_type not in allowed:
        raise LegacyExtensionCodecError(
            f"unsupported {direction} legacy extension message: {message_type}"
        )
    return message


def decode_legacy_extension_message(
    frame: str | bytes,
    *,
    direction: LegacyDirection,
) -> dict[str, Any]:
    """Decode and minimally validate one UTF-8 JSON text frame."""
    try:
        parsed = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise LegacyExtensionCodecError("invalid legacy extension JSON frame") from exc
    if not isinstance(parsed, dict):
        raise LegacyExtensionCodecError("legacy extension frame must be a JSON object")
    return _validate_message(parsed, direction)


def encode_legacy_extension_message(
    message: Mapping[str, Any],
    *,
    direction: LegacyDirection,
) -> str:
    """Validate and deterministically encode one legacy JSON text frame."""
    validated = _validate_message(dict(message), direction)
    return json.dumps(validated, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
