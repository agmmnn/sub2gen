"""Pure model and media helpers for Google Flow."""

from __future__ import annotations

import uuid
import time


class FlowModelResource:
    @staticmethod
    def generate_session_id() -> str:
        return f";{int(time.time() * 1000)}"

    @staticmethod
    def generate_scene_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def detect_image_mime_type(image_bytes: bytes) -> str:
        if len(image_bytes) < 12:
            return "image/jpeg"
        signatures = (
            (image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP", "image/webp"),
            (image_bytes[:4] == b"\x89PNG", "image/png"),
            (image_bytes[:3] == b"\xff\xd8\xff", "image/jpeg"),
            (image_bytes[:6] in (b"GIF87a", b"GIF89a"), "image/gif"),
            (image_bytes[:2] == b"BM", "image/bmp"),
            (image_bytes[:6] == b"\x00\x00\x00\x0cjP", "image/jp2"),
        )
        return next((mime for matched, mime in signatures if matched), "image/jpeg")
