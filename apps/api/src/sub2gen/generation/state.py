"""Request-local generation outcome state."""

from __future__ import annotations

from typing import Any


def create_generation_result() -> dict[str, Any]:
    return {
        "success": False,
        "error_message": None,
        "error_emitted": False,
        "error_status_code": 500,
        "error_extra": {},
    }


def create_response_state() -> dict[str, Any]:
    return {"url": None, "generated_assets": None, "base_url": None}


def mark_generation_failed(
    result: dict[str, Any] | None,
    error_message: str,
    *,
    status_code: int = 500,
    error_extra: dict[str, Any] | None = None,
) -> None:
    if result is None:
        return
    result.update(
        success=False,
        error_message=error_message,
        error_emitted=True,
        error_status_code=int(status_code),
        error_extra=dict(error_extra or {}),
    )


def mark_generation_succeeded(result: dict[str, Any] | None) -> None:
    if result is None:
        return
    result.update(
        success=True,
        error_message=None,
        error_emitted=False,
        error_status_code=200,
        error_extra={},
    )
