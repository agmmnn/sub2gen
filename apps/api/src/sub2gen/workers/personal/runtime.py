"""Browser runtime failure policy for personal workers."""

from __future__ import annotations

import os
import time
from typing import Any


RUNTIME_ERROR_KEYWORDS = (
    "has been closed",
    "browser has been closed",
    "target closed",
    'has no attribute "closed"',
    "has no attribute 'closed'",
    "connection closed",
    "connection lost",
    "connection refused",
    "connection reset",
    "broken pipe",
    "session closed",
    "not attached to an active page",
    "no session with given id",
    "cannot find context with specified id",
    "websocket is not open",
    "websocket unavailable",
    "'nonetype' object has no attribute 'send'",
    '"nonetype" object has no attribute "send"',
    "no close frame received or sent",
    "cannot call write to closing transport",
    "cannot write to closing transport",
    "cannot call send once a close message has been sent",
    "connectionclosederror",
    "connectionrefusederror",
    "disconnected",
    "errno 111",
)

NORMAL_CLOSE_KEYWORDS = (
    "connectionclosedok",
    "normal closure",
    "normal_closure",
    "sent 1000 (ok)",
    "received 1000 (ok)",
    "close(code=1000",
)


def flatten_exception_text(error: Any) -> str:
    """Flatten an exception chain for stable runtime error classification."""

    visited: set[int] = set()
    pending = [error]
    parts: list[str] = []
    while pending:
        current = pending.pop()
        if current is None or id(current) in visited:
            continue
        visited.add(id(current))
        parts.append(type(current).__name__)
        message = str(current or "").strip()
        if message:
            parts.append(message)
        args = getattr(current, "args", None)
        if isinstance(args, tuple):
            parts.extend(text for arg in args if (text := str(arg or "").strip()))
        pending.extend((getattr(current, "__cause__", None), getattr(current, "__context__", None)))
    return " | ".join(parts).lower()


def is_runtime_disconnect_error(error: Any) -> bool:
    error_text = flatten_exception_text(error)
    return bool(error_text) and (
        any(keyword in error_text for keyword in RUNTIME_ERROR_KEYWORDS)
        or any(keyword in error_text for keyword in NORMAL_CLOSE_KEYWORDS)
    )


def is_runtime_normal_close_error(error: Any) -> bool:
    error_text = flatten_exception_text(error)
    return bool(error_text) and any(keyword in error_text for keyword in NORMAL_CLOSE_KEYWORDS)


class PersonalBrowserRuntimePolicy:
    """Own launch backoff state and classify browser runtime failures."""

    def __init__(self) -> None:
        self.launch_failure_streak = 0
        self.launch_cooldown_until = 0.0
        self.launch_last_error = ""

    def cooldown_remaining(self) -> float:
        return max(0.0, self.launch_cooldown_until - time.monotonic())

    def reset_launch_failures(self) -> None:
        self.launch_failure_streak = 0
        self.launch_cooldown_until = 0.0
        self.launch_last_error = ""

    def record_launch_failure(self, error: Any) -> None:
        self.launch_failure_streak = min(8, max(0, self.launch_failure_streak) + 1)
        error_text = str(error or "").strip()
        error_lower = error_text.lower()
        base_seconds = 2.0
        if isinstance(error, PermissionError) or "winerror 5" in error_lower:
            base_seconds = 5.0
        elif any(keyword in error_lower for keyword in ("address already in use", "only one usage", "port")):
            base_seconds = 8.0
        cooldown = min(45.0, base_seconds * (2 ** min(4, self.launch_failure_streak - 1)))
        self.launch_cooldown_until = time.monotonic() + cooldown
        self.launch_last_error = f"{type(error).__name__}: {error_text or '<empty>'}"

    def raise_if_cooling_down(self) -> None:
        remaining = self.cooldown_remaining()
        if remaining <= 0.0:
            return
        suffix = f", last_error={self.launch_last_error}" if self.launch_last_error else ""
        raise RuntimeError(f"浏览器启动冷却中，请 {remaining:.1f}s 后重试{suffix}")

    @staticmethod
    def should_retry_without_sandbox(error: Any) -> bool:
        if os.name != "posix":
            return False
        error_text = str(error or "").lower()
        return any(
            keyword in error_text
            for keyword in (
                "no_sandbox",
                "no usable sandbox",
                "setuid sandbox",
                "namespace",
                "running as root",
                "you are running as root",
            )
        )

    @staticmethod
    def is_retryable_launch_error(error: Any) -> bool:
        error_text = str(error or "").lower()
        return any(
            keyword in error_text
            for keyword in (
                "failed to connect to browser",
                "connection refused",
                "connection reset",
                "connection closed",
                "websocket is not open",
                "chrome not reachable",
                "browser has been closed",
                "target closed",
            )
        )

    @staticmethod
    def is_memory_pressure_error(error: Any) -> bool:
        error_text = flatten_exception_text(error)
        return any(
            keyword in error_text
            for keyword in (
                "0xc000012d",
                "status_commitment_limit",
                "commitment limit",
                "paging file",
                "not enough memory",
                "insufficient system resources",
                "not enough storage is available",
                "out of memory",
                "cannot allocate memory",
            )
        )

    @staticmethod
    def is_invalid_context_error(error: Any) -> bool:
        error_text = str(error or "").lower()
        return (
            "failed to find browser context" in error_text
            or "cannot find context with specified id" in error_text
            or ("browser context" in error_text and "-32602" in error_text)
        )

    @staticmethod
    def is_no_browser_window_error(error: Any) -> bool:
        error_text = str(error or "").lower()
        return "no browser is open" in error_text or "failed to open new tab" in error_text

    @classmethod
    def is_runtime_error(cls, error: Any) -> bool:
        return is_runtime_disconnect_error(error) or cls.is_no_browser_window_error(error)
