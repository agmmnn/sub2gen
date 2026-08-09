"""Shared LLM provider orchestration (Gemini / OpenAI / OpenRouter / third-party / Cloudflare)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, List, Optional

from curl_cffi.requests import AsyncSession
from fastapi import HTTPException

from ..core.config import config as app_config

CLONING_PROVIDERS = [
    "cliproxy",
    "gemini_native",
    "openai",
    "openrouter",
    "third_party_gemini",
    "cloudflare",
]

METADATA_PROVIDERS = [
    "cliproxy",
    "gemini_native",
    "openai",
    "openrouter",
    "third_party_gemini",
    "cloudflare",
    "csvgen",
]

# OpenAI-compatible providers default to very high max_tokens; cap completion budget for JSON extraction calls.
_JSON_CHAT_MAX_TOKENS = 8192
_DEFAULT_JSON_HTTP_TIMEOUT_SECONDS = 120.0
_MIN_JSON_HTTP_TIMEOUT_SECONDS = 1.0


def extract_json_object(raw: str) -> Dict[str, Any]:
    cleaned = str(raw or "").replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise HTTPException(status_code=422, detail="Model response did not contain a JSON object")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Model response JSON parse failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="Model response JSON root must be an object")
    return parsed


def extract_non_empty_json_object(raw: str, service: str = "Model") -> Dict[str, Any]:
    if not str(raw or "").strip():
        raise HTTPException(status_code=422, detail=f"{service} returned empty JSON content")
    return extract_json_object(raw)


def _json_http_timeout(deadline_at: Optional[float], default_timeout: float = _DEFAULT_JSON_HTTP_TIMEOUT_SECONDS) -> float:
    if deadline_at is None:
        return default_timeout
    remaining = float(deadline_at) - time.monotonic()
    if remaining <= _MIN_JSON_HTTP_TIMEOUT_SECONDS:
        raise HTTPException(status_code=504, detail="Cloning prompt generation deadline exceeded before provider response")
    return max(_MIN_JSON_HTTP_TIMEOUT_SECONDS, min(default_timeout, remaining))


def get_csv(raw: str) -> List[str]:
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def _truncate_response_snippet(raw: Optional[str], max_len: int = 1200) -> str:
    s = (raw or "").replace("\r\n", "\n").strip()
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s or "(empty body)"


def _chain_http_status_for_upstream(upstream_status: int) -> int:
    """Map upstream HTTP codes for client responses; auth errors are not retried by the provider chain."""
    if upstream_status in (401, 402, 403):
        return upstream_status
    if upstream_status == 404:
        return 404
    if upstream_status == 429:
        return 429
    return 502


def _http_fail_exc(service: str, upstream_status: int, resp_text: Optional[str]) -> HTTPException:
    mapped = _chain_http_status_for_upstream(upstream_status)
    detail = f"{service} HTTP {upstream_status}: {_truncate_response_snippet(resp_text)}"
    return HTTPException(status_code=mapped, detail=detail)


def is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, HTTPException):
        status = int(getattr(exc, "status_code", 500) or 500)
        if status in (401, 402, 403):
            return False
        return status == 429 or status >= 500
    text = str(exc).lower()
    return any(token in text for token in ("timeout", "timed out", "connection", "temporar", "rate limit"))


def normalized_retry_count(raw_value: Any) -> int:
    try:
        return max(0, min(5, int(raw_value)))
    except Exception:
        return 1


class LlmProviderChain:
    @staticmethod
    def resolve_provider_chain(
        explicit_provider: Optional[str],
        *,
        provider_order_csv: str,
        enabled_providers_csv: str,
        legacy_backend: str,
        allowed_providers: List[str],
    ) -> List[str]:
        explicit = str(explicit_provider or "").strip().lower()
        if explicit:
            return [explicit]

        allowed = [p for p in allowed_providers]
        allowed_set = set(allowed)

        legacy = str(legacy_backend or "").strip().lower()
        if legacy not in allowed_set:
            legacy = allowed[0]

        configured_order = [p.strip().lower() for p in str(provider_order_csv or "").split(",") if p.strip()]
        configured_order = [p for p in configured_order if p in allowed_set]
        if not configured_order:
            configured_order = list(allowed)
        else:
            configured_order = configured_order + [p for p in allowed if p not in configured_order]

        enabled = [p.strip().lower() for p in str(enabled_providers_csv or "").split(",") if p.strip()]
        enabled = [p for p in enabled if p in allowed_set]
        if not enabled:
            enabled = [legacy]

        ordered_enabled = [p for p in configured_order if p in set(enabled)]
        return ordered_enabled

    async def invoke_with_provider_chain(
        self,
        *,
        providers: List[str],
        retry_count: int,
        model: str,
        fallback_models: Optional[List[str]],
        prompt_text: str,
        image_bytes: Optional[bytes],
        mime_type: str,
        use_cloning_credentials: bool = False,
        deadline_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not providers:
            raise HTTPException(status_code=400, detail="No enabled providers configured")

        retries = normalized_retry_count(retry_count)
        last_err: Optional[Exception] = None
        attempt_failures: List[str] = []

        for provider in providers:
            for attempt in range(retries + 1):
                try:
                    return await self.invoke_model_json(
                        provider=provider,
                        model=model,
                        fallback_models=fallback_models,
                        prompt_text=prompt_text,
                        image_bytes=image_bytes,
                        mime_type=mime_type,
                        use_cloning_credentials=use_cloning_credentials,
                        deadline_at=deadline_at,
                    )
                except Exception as exc:
                    last_err = exc
                    attempt_failures.append(f"{provider}#{attempt + 1}: {exc}")
                    if attempt < retries and is_retryable_error(exc):
                        continue
                    break

        detail = str(last_err or "Model invocation failed")
        if attempt_failures:
            detail = f"{detail} | attempts: {'; '.join(attempt_failures[-6:])}"
        out_status = 500
        if isinstance(last_err, HTTPException):
            out_status = int(getattr(last_err, "status_code", 500) or 500)
        raise HTTPException(status_code=out_status, detail=detail)

    async def invoke_model_json(
        self,
        *,
        provider: str,
        model: str,
        fallback_models: Optional[List[str]],
        prompt_text: str,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/jpeg",
        use_cloning_credentials: bool = False,
        deadline_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        models = [model] + [m for m in (fallback_models or []) if m and m != model]
        gateway_namespaces = {"codex", "gemini", "claude", "xai", "kimi", "vertex"}

        def is_gateway_model(value: str) -> bool:
            return "/" in str(value or "") and str(value).split("/", 1)[0] in gateway_namespaces

        if provider == "cliproxy":
            gateway_models = [candidate for candidate in models if is_gateway_model(candidate)]
            if gateway_models:
                models = gateway_models
        else:
            downstream_models: List[str] = []
            for candidate in models:
                if not is_gateway_model(candidate):
                    downstream_models.append(candidate)
                    continue
                namespace, raw_model = candidate.split("/", 1)
                if provider == "openai" and namespace == "codex":
                    downstream_models.append(raw_model)
                elif provider in {"gemini_native", "third_party_gemini"} and namespace == "gemini":
                    downstream_models.append(raw_model)
                elif provider == "openrouter":
                    openrouter_namespaces = {
                        "codex": "openai",
                        "gemini": "google",
                        "claude": "anthropic",
                        "xai": "x-ai",
                        "kimi": "moonshotai",
                    }
                    openrouter_namespace = openrouter_namespaces.get(namespace)
                    if openrouter_namespace:
                        downstream_models.append(
                            raw_model if "/" in raw_model else f"{openrouter_namespace}/{raw_model}"
                        )
            if downstream_models:
                models = list(dict.fromkeys(downstream_models))
        last_err: Optional[Exception] = None
        for candidate in models:
            try:
                if provider == "cliproxy":
                    return await self._invoke_cliproxy(
                        candidate, prompt_text, image_bytes, mime_type, deadline_at
                    )
                if provider == "openai":
                    return await self._invoke_openai(
                        candidate, prompt_text, image_bytes, mime_type, use_cloning_credentials, deadline_at
                    )
                if provider == "openrouter":
                    return await self._invoke_openrouter(
                        candidate, prompt_text, image_bytes, mime_type, use_cloning_credentials, deadline_at
                    )
                if provider == "third_party_gemini":
                    return await self._invoke_third_party(
                        candidate, prompt_text, image_bytes, mime_type, use_cloning_credentials, deadline_at
                    )
                if provider == "cloudflare":
                    return await self._invoke_cloudflare(
                        candidate, prompt_text, image_bytes, mime_type, use_cloning_credentials, deadline_at
                    )
                return await self._invoke_gemini(
                    candidate, prompt_text, image_bytes, mime_type, use_cloning_credentials, deadline_at
                )
            except Exception as exc:
                last_err = exc
                if isinstance(exc, HTTPException) and int(exc.status_code) in {400, 404}:
                    # A model rejected as unknown/incompatible should advance to
                    # the next configured model without treating the provider
                    # itself as unhealthy.
                    continue
                if not is_retryable_error(exc):
                    raise
                continue
        if last_err is None:
            raise HTTPException(status_code=500, detail="Model invocation failed")
        if isinstance(last_err, HTTPException):
            raise HTTPException(
                status_code=int(getattr(last_err, "status_code", 500) or 500),
                detail=last_err.detail,
            ) from last_err
        raise HTTPException(status_code=500, detail=str(last_err)) from last_err

    async def _invoke_cliproxy(
        self,
        model: str,
        prompt_text: str,
        image_bytes: Optional[bytes],
        mime_type: str,
        deadline_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        # Imported lazily to keep the management adapter independent from the
        # provider-chain module during application startup.
        from .cliproxy_client import CLIProxyInferenceClient

        return await CLIProxyInferenceClient().chat_json(
            model=model,
            prompt_text=prompt_text,
            image_bytes=image_bytes,
            mime_type=mime_type,
            timeout=_json_http_timeout(deadline_at),
            max_tokens=_JSON_CHAT_MAX_TOKENS,
        )

    async def _invoke_openai(
        self,
        model: str,
        prompt_text: str,
        image_bytes: Optional[bytes],
        mime_type: str,
        use_cloning_credentials: bool = False,
        deadline_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        keys = get_csv(app_config.sub2gen_openai_api_keys)
        if use_cloning_credentials:
            alt = get_csv(app_config.sub2gen_cloning_openai_api_keys)
            if alt:
                keys = alt
        if not keys:
            raise HTTPException(status_code=503, detail="OpenAI API key not configured")
        content: Any = prompt_text
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            content = [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                {"type": "text", "text": prompt_text},
            ]
        last_upstream = 502
        last_snippet = ""
        for key in keys:
            async with AsyncSession() as session:
                resp = await session.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "max_tokens": _JSON_CHAT_MAX_TOKENS,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "user", "content": content}],
                    },
                    timeout=_json_http_timeout(deadline_at),
                )
                if resp.status_code >= 400:
                    last_upstream = int(resp.status_code)
                    last_snippet = getattr(resp, "text", None) or ""
                    continue
                data = resp.json()
                text = (((data or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                return extract_non_empty_json_object(text, "OpenAI")
        raise _http_fail_exc("OpenAI", last_upstream, last_snippet)

    async def _invoke_openrouter(
        self,
        model: str,
        prompt_text: str,
        image_bytes: Optional[bytes],
        mime_type: str,
        use_cloning_credentials: bool = False,
        deadline_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        keys = get_csv(app_config.sub2gen_openrouter_api_keys)
        if use_cloning_credentials:
            alt = get_csv(app_config.sub2gen_cloning_openrouter_api_keys)
            if alt:
                keys = alt
        if not keys:
            raise HTTPException(status_code=503, detail="OpenRouter API key not configured")
        content: Any = prompt_text
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            content = [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                {"type": "text", "text": prompt_text},
            ]
        url = "https://openrouter.ai/api/v1/chat/completions"
        extra_headers = {
            "Referer": "https://github.com/sub2gen",
            "X-Title": "sub2gen",
        }
        last_upstream = 502
        last_snippet = ""
        for key in keys:
            async with AsyncSession() as session:
                resp = await session.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        **extra_headers,
                    },
                    json={
                        "model": model,
                        "max_tokens": _JSON_CHAT_MAX_TOKENS,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "user", "content": content}],
                    },
                    timeout=_json_http_timeout(deadline_at),
                )
                if resp.status_code >= 400:
                    last_upstream = int(resp.status_code)
                    last_snippet = getattr(resp, "text", None) or ""
                    continue
                data = resp.json()
                text = (((data or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                return extract_non_empty_json_object(text, "OpenRouter")
        raise _http_fail_exc("OpenRouter", last_upstream, last_snippet)

    async def _invoke_gemini(
        self,
        model: str,
        prompt_text: str,
        image_bytes: Optional[bytes],
        mime_type: str,
        use_cloning_credentials: bool = False,
        deadline_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        keys = get_csv(app_config.sub2gen_gemini_api_keys)
        if use_cloning_credentials:
            alt = get_csv(app_config.sub2gen_cloning_gemini_api_keys)
            if alt:
                keys = alt
        if not keys:
            raise HTTPException(status_code=503, detail="Gemini API key not configured")
        last_upstream = 502
        last_snippet = ""
        for key in keys:
            parts: List[Dict[str, Any]] = []
            if image_bytes:
                parts.append({"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}})
            parts.append({"text": prompt_text})
            body = {"contents": [{"parts": parts}], "generationConfig": {"responseMimeType": "application/json"}}
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            async with AsyncSession() as session:
                resp = await session.post(url, json=body, timeout=_json_http_timeout(deadline_at))
                if resp.status_code >= 400:
                    last_upstream = int(resp.status_code)
                    last_snippet = getattr(resp, "text", None) or ""
                    continue
                data = resp.json()
                text_parts = (((((data or {}).get("candidates") or [{}])[0].get("content") or {}).get("parts")) or [])
                text = "\n".join([str(p.get("text") or "") for p in text_parts if not p.get("thought")]).strip()
                return extract_non_empty_json_object(text, "Gemini")
        raise _http_fail_exc("Gemini", last_upstream, last_snippet)

    async def _invoke_third_party(
        self,
        model: str,
        prompt_text: str,
        image_bytes: Optional[bytes],
        mime_type: str,
        use_cloning_credentials: bool = False,
        deadline_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        endpoint = str(app_config.sub2gen_third_party_gemini_base_url or "").strip().rstrip("/")
        keys = get_csv(app_config.sub2gen_third_party_gemini_api_keys)
        if use_cloning_credentials:
            ce = str(app_config.sub2gen_cloning_third_party_gemini_base_url or "").strip().rstrip("/")
            if ce:
                endpoint = ce
            alt = get_csv(app_config.sub2gen_cloning_third_party_gemini_api_keys)
            if alt:
                keys = alt
        if not endpoint or not keys:
            raise HTTPException(status_code=503, detail="Third-party Gemini is not configured")
        content: Any = prompt_text
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            content = [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                {"type": "text", "text": prompt_text},
            ]
        last_upstream = 502
        last_snippet = ""
        for key in keys:
            async with AsyncSession() as session:
                resp = await session.post(
                    f"{endpoint}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "max_tokens": _JSON_CHAT_MAX_TOKENS,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "user", "content": content}],
                    },
                    timeout=_json_http_timeout(deadline_at),
                )
                if resp.status_code >= 400:
                    last_upstream = int(resp.status_code)
                    last_snippet = getattr(resp, "text", None) or ""
                    continue
                data = resp.json()
                text = (((data or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                return extract_non_empty_json_object(text, "Third-party Gemini")
        raise _http_fail_exc("Third-party Gemini", last_upstream, last_snippet)

    async def _invoke_cloudflare(
        self,
        model: str,
        prompt_text: str,
        image_bytes: Optional[bytes],
        mime_type: str,
        use_cloning_credentials: bool = False,
        deadline_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        account_id = str(app_config.cloudflare_account_id or "").strip()
        api_token = str(app_config.cloudflare_api_token or "").strip()
        if use_cloning_credentials:
            c_aid = str(app_config.sub2gen_cloning_cloudflare_account_id or "").strip()
            c_tok = str(app_config.sub2gen_cloning_cloudflare_api_token or "").strip()
            if c_aid and c_tok:
                account_id = c_aid
                api_token = c_tok
        if not account_id or not api_token:
            raise HTTPException(status_code=503, detail="Cloudflare Workers AI is not configured")
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            content: Any = [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                {"type": "text", "text": prompt_text},
            ]
        else:
            content = [{"type": "text", "text": prompt_text}]
        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"
        async with AsyncSession() as session:
            resp = await session.post(
                url,
                headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "max_tokens": _JSON_CHAT_MAX_TOKENS,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": content}],
                },
                timeout=_json_http_timeout(deadline_at),
            )
            if resp.status_code >= 400:
                snippet = (resp.text or "").replace("\r\n", "\n").strip()
                if len(snippet) > 1200:
                    snippet = snippet[:1200] + "…"
                raise HTTPException(
                    status_code=502,
                    detail=f"Cloudflare Workers AI HTTP {resp.status_code}: {snippet or '(empty body)'}",
                )
            data = resp.json()
            if isinstance(data, dict) and data.get("success") is False:
                snippet = str(data.get("errors") or data)[:1200]
                raise HTTPException(status_code=502, detail=f"Cloudflare API error: {snippet}")
            text = (((data or {}).get("result") or {}).get("response") or "")
            if not text:
                text = (((data or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            return extract_non_empty_json_object(text, "Cloudflare Workers AI")
