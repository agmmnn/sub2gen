"""Bounded HTTP side-channel storage for extension generation responses."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from typing import Any


def _debug_logger():
    # Lazy import avoids the current core package re-export cycle during direct
    # unit tests of this worker component.
    from ...core.logger import debug_logger

    return debug_logger


class GenerationUploadStore:
    def __init__(self) -> None:
        self._slots: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _prune_unlocked(self) -> None:
        now = time.time()
        expired = [key for key, value in self._slots.items() if float(value.get("expires_at") or 0) < now]
        for key in expired:
            self._slots.pop(key, None)

    async def register(self, *, req_id: str, max_body_bytes: int, ttl_seconds: int) -> tuple[str, str]:
        async with self._lock:
            self._prune_unlocked()
            upload_id = uuid.uuid4().hex
            upload_secret = secrets.token_urlsafe(48)
            self._slots[upload_id] = {
                "req_id": req_id,
                "secret": upload_secret,
                "body": None,
                "expires_at": time.time() + float(ttl_seconds),
                "max_body_bytes": int(max_body_bytes),
            }
            return upload_id, upload_secret

    async def ingest(self, upload_id: str, upload_secret: str, body: bytes) -> tuple[bool, str]:
        async with self._lock:
            self._prune_unlocked()
            slot = self._slots.get(upload_id)
            if not slot:
                return False, "unknown_or_expired_upload_id"
            if slot.get("secret") != upload_secret:
                return False, "invalid_upload_secret"
            if slot.get("body") is not None:
                return False, "duplicate_upload"
            if len(body) > int(slot.get("max_body_bytes") or 0):
                return False, "body_too_large"
            slot["body"] = body
            _debug_logger().log_info(
                f"[EXT-GEN] generation upload ingested: upload_id={upload_id}, bytes={len(body)}"
            )
            return True, ""

    async def resolve(self, *, req_id: str, upload_id: str, base_payload: dict[str, Any]) -> dict[str, Any]:
        deadline = time.time() + 8.0
        while time.time() < deadline:
            async with self._lock:
                self._prune_unlocked()
                slot = self._slots.get(upload_id)
                if slot is None:
                    return {
                        **base_payload,
                        "upload_status": "failed",
                        "upload_error": "unknown_or_expired_upload_id",
                    }
                if str(slot.get("req_id") or "") != str(req_id):
                    return {
                        **base_payload,
                        "upload_status": "failed",
                        "upload_error": "upload_req_mismatch",
                    }
                body = slot.get("body")
                if body is not None:
                    text = body.decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(text) if text else None
                    except Exception:
                        parsed = None
                    self._slots.pop(upload_id, None)
                    if not isinstance(parsed, dict) and text:
                        _debug_logger().log_warning(
                            f"[EXT-GEN] upload JSON parse failed for upload_id={upload_id}, text_len={len(text)}"
                        )
                        return {
                            **base_payload,
                            "upload_status": "failed",
                            "upload_error": "upload_invalid_json",
                            "response_text": text[:500],
                        }
                    return {
                        **base_payload,
                        "response_text": text,
                        "response_json": parsed if isinstance(parsed, dict) else None,
                        "upload_status": "uploaded",
                    }
            await asyncio.sleep(0.05)
        _debug_logger().log_warning(
            f"[EXT-GEN] upload body wait timeout: req_id={req_id}, upload_id={upload_id}"
        )
        return {
            **base_payload,
            "upload_status": "failed",
            "upload_error": "upload_body_missing_or_timeout",
        }
