"""Extension-first generation execution helper."""

import json
from typing import Any, Dict, Optional

from ..core.logger import debug_logger
from .browser_captcha_extension import ExtensionCaptchaService


class ExtensionGenerationService:
    """Adapter for extension-driven generation submit/poll requests.

    Reuses the existing extension websocket channel and returns parsed JSON
    payloads compatible with FlowClient callers.
    """

    def __init__(self, db=None):
        self.db = db

    async def submit_generation(
        self,
        *,
        url: str,
        method: str,
        headers: Dict[str, Any],
        json_data: Optional[Dict[str, Any]],
        timeout_seconds: int,
        token_id: Optional[int],
        managed_api_key_id: Optional[int],
    ) -> Dict[str, Any]:
        svc = await ExtensionCaptchaService.get_instance(self.db)
        debug_logger.log_info(f"[EXT-GEN] submit via extension: {method} {url}")
        result = await svc.submit_generation_via_extension(
            url=url,
            method=method,
            headers=headers,
            json_data=json_data or {},
            timeout=timeout_seconds,
            token_id=token_id,
            managed_api_key_id=managed_api_key_id,
        )
        return self._unwrap_extension_response(result)

    async def poll_generation(
        self,
        *,
        url: str,
        method: str,
        headers: Dict[str, Any],
        json_data: Optional[Dict[str, Any]],
        timeout_seconds: int,
        token_id: Optional[int],
        managed_api_key_id: Optional[int],
    ) -> Dict[str, Any]:
        svc = await ExtensionCaptchaService.get_instance(self.db)
        debug_logger.log_info(f"[EXT-GEN] poll fallback via extension: {method} {url}")
        result = await svc.poll_generation_via_extension(
            url=url,
            method=method,
            headers=headers,
            json_data=json_data or {},
            timeout=timeout_seconds,
            token_id=token_id,
            managed_api_key_id=managed_api_key_id,
        )
        return self._unwrap_extension_response(result)

    @staticmethod
    def _unwrap_extension_response(result: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(result, dict):
            raise RuntimeError("Invalid extension response payload")
        relay_status = str(result.get("status") or "")
        if relay_status and relay_status != "success":
            relay_error = str(result.get("error") or "").strip()
            raise RuntimeError(relay_error or "Extension relay returned an error")
        status_code = int(result.get("response_status") or 0)
        if status_code >= 400:
            response_text = str(result.get("response_text") or "").strip()
            raise RuntimeError(response_text or f"HTTP Error {status_code}")
        upload_status = str(result.get("upload_status") or "").strip()
        upload_error = str(result.get("upload_error") or "").strip()
        response_json = result.get("response_json")
        if isinstance(response_json, dict):
            if upload_status == "failed":
                response_json.setdefault("_relay_upload_status", "failed")
                if upload_error:
                    response_json.setdefault("_relay_upload_error", upload_error)
                debug_logger.log_warning(
                    f"[EXT-GEN] upstream success with upload warning: {upload_error or 'unknown_upload_error'}"
                )
            return response_json
        response_text = str(result.get("response_text") or "").strip()
        if response_text:
            try:
                parsed = json.loads(response_text)
                if isinstance(parsed, dict):
                    if upload_status == "failed":
                        parsed.setdefault("_relay_upload_status", "failed")
                        if upload_error:
                            parsed.setdefault("_relay_upload_error", upload_error)
                    return parsed
            except Exception:
                pass
        if upload_status == "failed":
            raise RuntimeError(upload_error or "Extension upload pipeline failed")
        raise RuntimeError(response_text or "Extension response missing JSON body")
