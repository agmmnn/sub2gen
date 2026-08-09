"""Redact / truncate payloads before persisting to request_logs."""

from __future__ import annotations

import json
import re
from typing import Any

_MAX_STR = 2048
_KEY_SUBSTR_REDACT = ("base64", "embedding", "dataurl", "session_token", "access_token")
_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)


def _clean_base64_payload(value: str) -> tuple[str, str | None] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    data_url_match = _DATA_URL_RE.match(raw)
    if data_url_match:
        mime = data_url_match.group(1).strip().lower()
        raw = data_url_match.group(2)
    else:
        mime = None
        if "base64," in raw.lower():
            raw = raw.split(",", 1)[1]
    compact = "".join(raw.split())
    if not compact:
        return None
    return compact, mime


def _clean_image_mime(value: Any, fallback: str | None = None) -> str | None:
    text = str(value or "").strip().split(";", 1)[0].lower()
    if text.startswith("image/") and len(text) <= 80:
        return text
    if fallback and fallback.startswith("image/") and len(fallback) <= 80:
        return fallback
    return None


def _image_preview_for_log(payload: dict[str, Any]) -> dict[str, Any] | None:
    image_base64 = payload.get("image_base64")
    if isinstance(image_base64, str) and image_base64.strip():
        cleaned = _clean_base64_payload(image_base64)
        if cleaned:
            base64_payload, data_url_mime = cleaned
            mime_type = _clean_image_mime(
                payload.get("mimeType") or payload.get("mime_type"),
                data_url_mime,
            )
            if mime_type:
                return {
                    "source": "image_base64",
                    "mimeType": mime_type,
                    "dataUrl": f"data:{mime_type};base64,{base64_payload}",
                    "base64Length": len(base64_payload),
                }

    image_url = payload.get("image_url")
    if isinstance(image_url, str) and image_url.strip():
        return {
            "source": "image_url",
            "url": _truncate_str(image_url.strip()),
        }
    return None


def _truncate_str(s: str) -> str:
    if len(s) <= _MAX_STR:
        return s
    return s[: _MAX_STR - 48] + f"...[truncated len={len(s)}]"


def sanitize_for_request_log(value: Any, depth: int = 0) -> Any:
    if depth > 28:
        return "<max_depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_str(value)
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            ks = str(k)
            lk = ks.lower()
            if any(tok in lk for tok in _KEY_SUBSTR_REDACT):
                out[ks] = "<redacted>"
                continue
            out[ks] = sanitize_for_request_log(v, depth + 1)
        if "imagePreview" not in out:
            preview = _image_preview_for_log(value)
            if preview:
                out["imagePreview"] = preview
        return out
    if isinstance(value, (list, tuple)):
        cap = 120
        seq = [sanitize_for_request_log(x, depth + 1) for x in value[:cap]]
        if len(value) > cap:
            seq.append(f"<... {len(value) - cap} more items>")
        return seq
    return _truncate_str(str(value))


def dumps_for_request_log(obj: Any) -> str:
    try:
        return json.dumps(sanitize_for_request_log(obj), ensure_ascii=False)
    except Exception:
        return json.dumps({"error": "serialization_failed"}, ensure_ascii=False)
