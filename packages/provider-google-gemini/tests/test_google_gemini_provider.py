from __future__ import annotations

import base64

import httpx
import pytest

from sub2gen_provider_google_gemini import GoogleGeminiHttpBackend, GoogleGeminiProvider
from sub2gen_provider_sdk import (
    CancellationToken,
    GenerationRequest,
    ProviderExecutionContext,
    ReferenceInput,
    ResolvedExecution,
)
from sub2gen_provider_sdk.testing import exercise_provider


def _context() -> ProviderExecutionContext:
    return ProviderExecutionContext(
        ResolvedExecution(
            requested_model="google-gemini/gemini-2.5-flash-image",
            resolved_model="gemini-2.5-flash-image",
            provider_id="google-gemini",
            billing_pool="google-gemini:api",
            provider_account_id="gemini-account",
        ),
        CancellationToken(),
        timeout_seconds=1,
    )


@pytest.mark.asyncio
async def test_direct_gemini_provider_passes_sdk_conformance() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "responseId": "response-1",
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(b"gemini-image").decode(),
                                    }
                                }
                            ]
                        }
                    }
                ],
            },
        )

    backend = GoogleGeminiHttpBackend("test-key", transport=httpx.MockTransport(handler))
    provider = GoogleGeminiProvider(backend)
    request = GenerationRequest(
        "request-1",
        "draw",
        "google-gemini/gemini-2.5-flash-image",
        references=(ReferenceInput("image/png", data=b"reference"),),
    )

    report = await exercise_provider(provider, request, _context())

    assert report.provider_id == "google-gemini"
    assert len(requests) == 2
    assert requests[0].url.path.endswith("/gemini-2.5-flash-image:generateContent")
    assert requests[0].url.params["key"] == "test-key"
    await backend.aclose()
