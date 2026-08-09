"""Executable codec for the unversioned agent-gateway worker dialect."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal


LegacyDirection = Literal["server_to_worker", "worker_to_server"]

_SERVER_MESSAGE_TYPES = frozenset({"registered", "solve_job", "error"})
_WORKER_MESSAGE_TYPES = frozenset({"register", "solve_result", "solve_error", "ping"})


class LegacyAgentGatewayCodecError(ValueError):
    """Raised when a frame is not valid for the frozen legacy dialect."""


def _validate_message(message: dict[str, Any], direction: LegacyDirection) -> dict[str, Any]:
    message_type = message.get("type")
    if not isinstance(message_type, str):
        raise LegacyAgentGatewayCodecError("message type must be a string")
    allowed = _SERVER_MESSAGE_TYPES if direction == "server_to_worker" else _WORKER_MESSAGE_TYPES
    if message_type not in allowed:
        raise LegacyAgentGatewayCodecError(
            f"unsupported {direction} legacy agent-gateway message: {message_type}"
        )
    return message


def decode_legacy_agent_gateway_message(
    frame: str | bytes,
    *,
    direction: LegacyDirection,
) -> dict[str, Any]:
    """Decode and minimally validate one UTF-8 JSON text frame."""
    try:
        parsed = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise LegacyAgentGatewayCodecError("invalid legacy agent-gateway JSON frame") from exc
    if not isinstance(parsed, dict):
        raise LegacyAgentGatewayCodecError("legacy agent-gateway frame must be a JSON object")
    return _validate_message(parsed, direction)


def encode_legacy_agent_gateway_message(
    message: Mapping[str, Any],
    *,
    direction: LegacyDirection,
) -> str:
    """Validate and deterministically encode one legacy JSON text frame."""
    validated = _validate_message(dict(message), direction)
    return json.dumps(validated, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
