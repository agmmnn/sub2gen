"""Extension worker connection and result models."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket


CAPTCHA_USER_AGENT_MAX_LENGTH = 512


class NoExtensionGenerationWorkerError(RuntimeError):
    """Raised when extension-first generation has no eligible worker."""


def normalize_extension_captcha_user_agent(value: Any) -> str | None:
    """Return a safe solver-produced UA without weakening token compatibility."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > CAPTCHA_USER_AGENT_MAX_LENGTH or "\r" in normalized or "\n" in normalized:
        return None
    return normalized


@dataclass
class DedicatedWorkerStats:
    """In-memory health and latency signals for a dedicated worker."""

    inflight_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    ema_latency_ms: float = 0.0
    has_latency_sample: bool = False
    fail_timestamps: list[float] = field(default_factory=list)
    timeout_timestamps: list[float] = field(default_factory=list)
    cooldown_until: float = 0.0


@dataclass
class ExtensionStRefreshResult:
    """Outcome of extension-based session-token refresh."""

    session_token: str | None = None
    failure_code: str | None = None


@dataclass
class ExtensionConnection:
    websocket: WebSocket
    worker_session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    instance_id: str = ""
    route_key: str = ""
    client_label: str = ""
    managed_api_key_id: int | None = None
    binding_source: str = "none"
    captcha_worker_id: int | None = None
    captcha_worker_key_label: str = ""
    captcha_worker_key_prefix: str = ""
    refresh_token_id: int | None = None
    allow_captcha: bool = True
    allow_session_refresh: bool = True
    allow_generation: bool = False
    connected_at: float = field(default_factory=time.time)
    dispatch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
