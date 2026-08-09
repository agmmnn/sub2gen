"""HTTP backend for the direct Gemini generateContent API."""

from __future__ import annotations

import base64
from urllib.parse import quote

import httpx
from sub2gen_provider_sdk import (
    Artifact,
    GenerationRequest,
    ProviderError,
    ProviderErrorCode,
    ProviderExecutionContext,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderResult,
)


class GoogleGeminiHttpBackend:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://generativelanguage.googleapis.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Google Gemini API key is required")
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport)

    async def health(self) -> ProviderHealth:
        return ProviderHealth("google-gemini", ProviderHealthStatus.READY, "API key configured")

    async def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> ProviderResult:
        model = request.model.removeprefix("google-gemini/")
        parts: list[dict[str, object]] = [{"text": request.prompt}]
        parts.extend(
            {
                "inlineData": {
                    "mimeType": reference.media_type,
                    "data": base64.b64encode(reference.read_bytes()).decode("ascii"),
                }
            }
            for reference in request.references
        )
        response = await self._client.post(
            f"/v1beta/models/{quote(model, safe='')}:generateContent",
            params={"key": self._api_key},
            json={
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseModalities": ["IMAGE"]},
            },
        )
        if response.status_code >= 400:
            if response.status_code in {401, 403}:
                code, retryable = ProviderErrorCode.AUTHENTICATION, False
            elif response.status_code == 429:
                code, retryable = ProviderErrorCode.QUOTA, True
            elif response.status_code >= 500:
                code, retryable = ProviderErrorCode.TRANSIENT, True
            else:
                code, retryable = ProviderErrorCode.INVALID_INPUT, False
            raise ProviderError(code, f"Gemini request failed with HTTP {response.status_code}", retryable=retryable)
        payload = response.json()
        artifacts: list[Artifact] = []
        for candidate in payload.get("candidates") or ():
            for part in (candidate.get("content") or {}).get("parts") or ():
                inline = part.get("inlineData") or part.get("inline_data")
                if not inline or not inline.get("data"):
                    continue
                try:
                    data = base64.b64decode(inline["data"], validate=True)
                except (ValueError, TypeError) as exc:
                    raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Gemini returned invalid image data") from exc
                artifacts.append(Artifact(str(inline.get("mimeType") or inline.get("mime_type") or "image/png"), data=data))
        if not artifacts:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Gemini returned no image artifacts")
        return ProviderResult(tuple(artifacts), context.resolved, metadata={"response_id": payload.get("responseId")})

    async def cancel(self, provider_job_id: str) -> None:
        del provider_job_id

    async def aclose(self) -> None:
        await self._client.aclose()
