"""Extension generation request executor."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from .jobs import ExtensionJobBroker
from .models import ExtensionConnection
from .uploads import GenerationUploadStore


class ExtensionGenerationJobs:
    def __init__(
        self,
        broker: ExtensionJobBroker,
        uploads: GenerationUploadStore,
    ) -> None:
        self.broker = broker
        self.uploads = uploads

    async def execute(
        self,
        connection: ExtensionConnection,
        *,
        message_type: str,
        request_payload: dict[str, Any],
        timeout: int,
        large_upload_enabled: bool,
        upload_ttl_seconds: int,
        upload_max_bytes: int,
        upload_threshold_bytes: int,
        force_upsample_upload: bool,
    ) -> dict[str, Any]:
        request_id = f"gen_req_{uuid.uuid4().hex}"
        future = self.broker.register("generation", request_id, connection.websocket)
        message: dict[str, Any] = {
            "type": message_type,
            "req_id": request_id,
            **request_payload,
        }
        if large_upload_enabled:
            upload_id, upload_secret = await self.uploads.register(
                req_id=request_id,
                max_body_bytes=upload_max_bytes,
                ttl_seconds=upload_ttl_seconds,
            )
            url = str(request_payload.get("url") or "").lower()
            force_upload = force_upsample_upload and "upsampleimage" in url
            message["large_response_upload"] = {
                "upload_id": upload_id,
                "upload_secret": upload_secret,
                "upload_path": "/api/extension/generation-upload",
                "threshold_bytes": 0 if force_upload else upload_threshold_bytes,
                "force_http_upload": force_upload,
            }
        try:
            await connection.websocket.send_text(json.dumps(message))
            result = await asyncio.wait_for(future, timeout=max(5, int(timeout or 30)))
            if not isinstance(result, dict):
                raise RuntimeError("Invalid extension generation response format")
            if result.get("status") == "success":
                return result
            raise RuntimeError(str(result.get("error") or "Extension generation request failed"))
        finally:
            self.broker.remove("generation", request_id)
