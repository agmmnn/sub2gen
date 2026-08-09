"""Strict CLIProxyAPI client used for inference and the admin control plane.

Only explicitly modelled management operations are exposed. In particular,
this module deliberately has no wrapper for raw config, credential downloads,
or CLIProxy's unrestricted authenticated upstream request endpoint.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx
from fastapi import HTTPException

from ..core.cliproxy_models import (
    CLIProxyAccount,
    CLIProxyAlias,
    CLIProxyApiKeyImport,
    CLIProxyCredentialImportItem,
    CLIProxyCredentialImportResponse,
    CLIProxyLogs,
    CLIProxyModel,
    CLIProxyOAuthSession,
    CLIProxyOverview,
    CLIProxyPlatform,
    CLIProxyRouting,
)
from ..core.config import config


MANAGEMENT_BASE = "/v0/management"
KNOWN_PLATFORM_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "codex": {"label": "Codex / GPT", "namespace": "codex", "oauth": True, "imports": ["credential", "api_key"]},
    "antigravity": {"label": "Gemini / Antigravity", "namespace": "gemini", "oauth": True, "imports": ["credential"]},
    "gemini": {"label": "Gemini API", "namespace": "gemini", "oauth": False, "imports": ["credential", "api_key"]},
    "claude": {"label": "Claude", "namespace": "claude", "oauth": True, "imports": ["credential", "api_key"]},
    "xai": {"label": "Grok / xAI", "namespace": "xai", "oauth": True, "imports": ["credential", "api_key"]},
    "kimi": {"label": "Kimi", "namespace": "kimi", "oauth": True, "imports": ["credential"]},
    "vertex": {"label": "Vertex AI", "namespace": "vertex", "oauth": False, "imports": ["credential", "api_key"]},
    "openai-compatible": {
        "label": "OpenAI-compatible",
        "namespace": "openai-compatible",
        "oauth": False,
        "imports": ["api_key"],
    },
}

PROVIDER_ALIASES = {
    "anthropic": "claude",
    "claude-code": "claude",
    "grok": "xai",
    "openai-codex": "codex",
    "google": "gemini",
    "openai-compatibility": "openai-compatible",
}

NAMESPACE_CHANNELS: Dict[str, Sequence[str]] = {
    "codex": ("codex",),
    "gemini": ("antigravity", "gemini"),
    "claude": ("claude",),
    "xai": ("xai",),
    "kimi": ("kimi",),
    "vertex": ("vertex",),
}

OAUTH_ENDPOINTS = {
    "codex": "codex-auth-url",
    "claude": "anthropic-auth-url",
    "antigravity": "antigravity-auth-url",
    "xai": "xai-auth-url",
    "kimi": "kimi-auth-url",
}

API_KEY_COLLECTIONS = {
    "gemini": "gemini-api-key",
    "vertex": "vertex-api-key",
    "xai": "xai-api-key",
    "codex": "codex-api-key",
    "claude": "claude-api-key",
}

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+\-]{0,254}$")
_SAFE_PLATFORM = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SECRET_KEY_RE = re.compile(
    r"(authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|cookie|password)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_VALUE_RE = re.compile(
    r"(?i)(api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|password|cookie)(\s*[:=]\s*)([^\s,;]+)"
)
_DATA_URL_RE = re.compile(r"data:[^;\s]+;base64,[A-Za-z0-9+/=]+", re.IGNORECASE)
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._@+\-]+")
MAX_CREDENTIAL_FILE_BYTES = 2 * 1024 * 1024
MAX_CREDENTIAL_BUNDLE_ITEMS = 100


@dataclass(frozen=True)
class _PreparedCredential:
    name: str
    email: str
    content: bytes


class CLIProxyUpstreamError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = int(status_code)
        self.detail = detail
        super().__init__(detail)


def _extract_json_object(raw: str) -> Dict[str, Any]:
    cleaned = str(raw or "").replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise HTTPException(status_code=422, detail="CLIProxy response did not contain a JSON object")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"CLIProxy response JSON parse failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="CLIProxy response JSON root must be an object")
    return parsed


def canonical_platform(value: Any) -> str:
    raw = str(value or "unknown").strip().lower().replace("_", "-")
    return PROVIDER_ALIASES.get(raw, raw or "unknown")


def platform_namespace(platform: str) -> str:
    canonical = canonical_platform(platform)
    definition = KNOWN_PLATFORM_DEFINITIONS.get(canonical)
    return str((definition or {}).get("namespace") or canonical)


def namespaced_model(platform: str, model: str) -> str:
    raw = str(model or "").strip().lstrip("/")
    namespace = platform_namespace(platform)
    if not raw:
        return ""
    if raw.startswith(f"{namespace}/"):
        return raw
    return f"{namespace}/{raw}"


def split_gateway_model(model: str) -> Optional[Tuple[str, str]]:
    value = str(model or "").strip()
    if "/" not in value:
        return None
    namespace, raw = value.split("/", 1)
    if namespace not in NAMESPACE_CHANNELS or not raw:
        return None
    return namespace, raw


def redact_text(value: Any, max_length: int = 1600) -> str:
    text = str(value or "")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = _DATA_URL_RE.sub("data:[REDACTED]", text)
    if len(text) > max_length:
        text = text[:max_length] + "…"
    return text


def redact_structure(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = redact_structure(item)
        return out
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _validate_identifier(value: str, label: str = "identifier") -> str:
    normalized = str(value or "").strip()
    if not _SAFE_IDENTIFIER.fullmatch(normalized):
        raise HTTPException(status_code=400, detail=f"Invalid CLIProxy {label}")
    return normalized


def _validate_platform(value: str) -> str:
    normalized = canonical_platform(value)
    if not _SAFE_PLATFORM.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="Invalid CLIProxy platform")
    return normalized


def _string_field(source: Dict[str, Any], *names: str) -> str:
    for name in names:
        value = source.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _jwt_payload(token: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
        value = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _timestamp_iso(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1_000_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OSError, OverflowError, ValueError):
            return ""
    return ""


def _codex_account_id(item: Dict[str, Any], id_token: str) -> str:
    direct = _string_field(item, "account_id", "accountId", "chatgpt_account_id")
    if direct:
        return direct
    auth_claims = _jwt_payload(id_token).get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        return ""
    return _string_field(auth_claims, "chatgpt_account_id", "account_id")


def _codex_expiry(item: Dict[str, Any], access_token: str, id_token: str) -> str:
    explicit = _timestamp_iso(item.get("expired") or item.get("expires_at"))
    if explicit:
        return explicit
    for token in (access_token, id_token):
        exp = _jwt_payload(token).get("exp")
        normalized = _timestamp_iso(exp)
        if normalized:
            return normalized
    return ""


def _normalize_cockpit_codex_credential(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    declared = _string_field(item, "type", "provider")
    if declared and canonical_platform(declared) != "codex":
        raise HTTPException(
            status_code=400,
            detail=f"Credential bundle item {index + 1} is {declared}, not codex",
        )
    auth_mode = _string_field(item, "auth_mode", "authMode").lower()
    if auth_mode in {"apikey", "api_key"} or _string_field(item, "OPENAI_API_KEY"):
        raise HTTPException(
            status_code=400,
            detail=f"Credential bundle item {index + 1} is an API-key account; use the API key tab",
        )

    nested = item.get("tokens")
    tokens = nested if isinstance(nested, dict) else item
    access_token = _string_field(tokens, "access_token", "accessToken")
    if not access_token:
        raise HTTPException(
            status_code=400,
            detail=f"Credential bundle item {index + 1} is missing access_token",
        )
    id_token = _string_field(tokens, "id_token", "idToken")
    refresh_token = _string_field(tokens, "refresh_token", "refreshToken")
    email = _string_field(item, "email") or _string_field(tokens, "email")
    account_id = _codex_account_id(item, id_token)
    last_refresh = _timestamp_iso(
        item.get("last_refresh")
        or item.get("lastRefresh")
        or item.get("token_updated_at")
        or item.get("tokenUpdatedAt")
    )

    # Only forward fields needed by CLIProxy. Cockpit notes can contain passwords,
    # MFA seeds, phone numbers, and mailbox URLs that do not belong in the gateway.
    return {
        "type": "codex",
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "account_id": account_id,
        "last_refresh": last_refresh,
        "expired": _codex_expiry(item, access_token, id_token),
    }


def _normalize_cockpit_antigravity_credential(
    item: Dict[str, Any], index: int
) -> Dict[str, Any]:
    declared = _string_field(item, "type", "provider")
    if declared and canonical_platform(declared) != "antigravity":
        raise HTTPException(
            status_code=400,
            detail=f"Credential bundle item {index + 1} is {declared}, not antigravity",
        )

    nested = item.get("token") or item.get("tokens")
    tokens = nested if isinstance(nested, dict) else item
    refresh_token = _string_field(tokens, "refresh_token", "refreshToken")
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail=f"Credential bundle item {index + 1} is missing refresh_token",
        )
    access_token = _string_field(tokens, "access_token", "accessToken")
    email = _string_field(item, "email") or _string_field(tokens, "email")
    project_id = _string_field(tokens, "project_id", "projectId") or _string_field(
        item, "project_id", "projectId"
    )
    expires_in_raw = tokens.get("expires_in", tokens.get("expiresIn", 0))
    try:
        expires_in = max(0, int(expires_in_raw or 0))
    except (TypeError, ValueError):
        expires_in = 0
    timestamp_raw = tokens.get("timestamp", item.get("timestamp", 0))
    try:
        timestamp = max(0, int(timestamp_raw or 0))
    except (TypeError, ValueError):
        timestamp = 0
    expired = _timestamp_iso(
        tokens.get("expired")
        or item.get("expired")
        or tokens.get("expiry_timestamp")
        or tokens.get("expiryTimestamp")
    )

    # Cockpit's portable Antigravity export intentionally contains only email
    # and refresh_token. CLIProxy uses the same OAuth client and can obtain a
    # fresh access token and project ID on the first inference request.
    return {
        "type": "antigravity",
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "timestamp": timestamp,
        "expired": expired,
        "project_id": project_id,
    }


def _credential_filename(platform: str, item: Dict[str, Any], index: int, used: set[str]) -> str:
    identity = _string_field(item, "email", "account_id") or f"account-{index + 1}"
    safe_identity = _UNSAFE_FILENAME_RE.sub("-", identity).strip(".-_") or f"account-{index + 1}"
    stem = f"{platform}-{safe_identity}"[:235].rstrip(".-_")
    candidate = f"{stem}.json"
    suffix = 2
    while candidate.lower() in used:
        suffix_text = f"-{suffix}"
        candidate = f"{stem[:235 - len(suffix_text)]}{suffix_text}.json"
        suffix += 1
    used.add(candidate.lower())
    return _validate_identifier(candidate, "credential filename")


def prepare_credential_imports(
    *, platform: str, filename: str, content: bytes
) -> List[_PreparedCredential]:
    canonical = _validate_platform(platform)
    if len(content) > MAX_CREDENTIAL_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Credential file exceeds 2 MiB")
    try:
        decoded = json.loads(content.decode("utf-8-sig"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Credential file must contain valid UTF-8 JSON") from exc

    is_bundle = isinstance(decoded, list)
    if is_bundle:
        if canonical not in {"codex", "antigravity"}:
            raise HTTPException(
                status_code=400,
                detail="Multi-account credential bundles are supported for Codex and Antigravity exports",
            )
        if not decoded:
            raise HTTPException(status_code=400, detail="Credential bundle is empty")
        if len(decoded) > MAX_CREDENTIAL_BUNDLE_ITEMS:
            raise HTTPException(
                status_code=400,
                detail=f"Credential bundle exceeds {MAX_CREDENTIAL_BUNDLE_ITEMS} accounts",
            )
        raw_items = decoded
    elif isinstance(decoded, dict):
        raw_items = [decoded]
    else:
        raise HTTPException(status_code=400, detail="Credential JSON root must be an object or array")

    used_names: set[str] = set()
    prepared: List[_PreparedCredential] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Credential bundle item {index + 1} must be a JSON object",
            )
        if canonical == "codex":
            normalized = _normalize_cockpit_codex_credential(raw, index)
        elif canonical == "antigravity":
            normalized = _normalize_cockpit_antigravity_credential(raw, index)
        else:
            normalized = raw
        if is_bundle:
            name = _credential_filename(canonical, normalized, index, used_names)
        else:
            name = _validate_identifier(filename, "credential filename")
            if not name.lower().endswith(".json"):
                raise HTTPException(status_code=400, detail="Credential filename must end in .json")
        prepared.append(
            _PreparedCredential(
                name=name,
                email=_string_field(normalized, "email"),
                content=json.dumps(normalized, separators=(",", ":")).encode("utf-8"),
            )
        )
    return prepared


def _model_capabilities(raw: Dict[str, Any], model_id: str) -> List[str]:
    capabilities = ["text"]
    source = raw.get("capabilities") or raw.get("modalities") or raw.get("input_modalities")
    source_text = json.dumps(source, default=str).lower() if source is not None else ""
    if "image" in source_text or any(token in model_id.lower() for token in ("vision", "vl", "image")):
        capabilities.append("image-input")
    if raw.get("thinking") or "reason" in source_text:
        capabilities.append("reasoning")
    return list(dict.fromkeys(capabilities))


def normalize_model(platform: str, raw: Any, *, excluded: bool = False) -> Optional[CLIProxyModel]:
    item = raw if isinstance(raw, dict) else {"id": raw}
    raw_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
    if not raw_id:
        return None
    return CLIProxyModel(
        id=namespaced_model(platform, raw_id),
        raw_id=raw_id,
        platform=canonical_platform(platform),
        display_name=str(item.get("display_name") or item.get("display-name") or item.get("name") or raw_id),
        owned_by=str(item.get("owned_by") or item.get("owned-by") or ""),
        capabilities=_model_capabilities(item, raw_id),
        excluded=excluded,
    )


class CLIProxyManagementClient:
    def __init__(self, *, base_url: Optional[str] = None, management_key: Optional[str] = None):
        self.base_url = str(base_url if base_url is not None else config.cliproxy_base_url).rstrip("/")
        self.management_key = str(
            management_key if management_key is not None else config.cliproxy_management_key
        ).strip()
        self._prepared_aliases: set[str] = set()
        self._alias_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.management_key)

    def require_configured(self) -> None:
        if not self.base_url:
            raise HTTPException(status_code=503, detail="CLIProxy base URL is not configured")
        if not self.management_key:
            raise HTTPException(status_code=503, detail="CLIProxy management key is not configured")

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        content: Optional[bytes] = None,
        files: Any = None,
        data: Any = None,
        timeout: float = 20.0,
    ) -> Any:
        self.require_configured()
        if not endpoint or endpoint.startswith("/") or ".." in endpoint:
            raise RuntimeError("Unsafe internal CLIProxy management endpoint")
        url = f"{self.base_url}{MANAGEMENT_BASE}/{endpoint}"
        headers = {"Authorization": f"Bearer {self.management_key}"}
        if content is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    content=content,
                    files=files,
                    data=data,
                )
        except httpx.TimeoutException as exc:
            raise CLIProxyUpstreamError(504, "CLIProxy management request timed out") from exc
        except httpx.HTTPError as exc:
            raise CLIProxyUpstreamError(502, f"CLIProxy management connection failed: {redact_text(exc)}") from exc
        if response.status_code >= 400:
            try:
                detail_source = response.json()
            except Exception:
                detail_source = response.text
            detail = redact_text(redact_structure(detail_source))
            mapped = response.status_code if response.status_code in {400, 404, 409, 422, 429} else 502
            raise CLIProxyUpstreamError(mapped, f"CLIProxy management HTTP {response.status_code}: {detail}")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except Exception as exc:
            raise CLIProxyUpstreamError(502, "CLIProxy management returned invalid JSON") from exc

    async def list_accounts(self, *, include_models: bool = False) -> List[CLIProxyAccount]:
        payload = await self._request("GET", "auth-files")
        raw_files = payload.get("files", []) if isinstance(payload, dict) else []
        accounts: List[CLIProxyAccount] = []
        for raw in raw_files if isinstance(raw_files, list) else []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or raw.get("id") or "").strip()
            if not name:
                continue
            platform = canonical_platform(raw.get("provider") or raw.get("type") or raw.get("account_type"))
            accounts.append(
                CLIProxyAccount(
                    id=str(raw.get("id") or name),
                    auth_index=str(raw.get("auth_index") or raw.get("auth-index") or ""),
                    name=name,
                    platform=platform,
                    label=str(raw.get("label") or ""),
                    email=str(raw.get("email") or ""),
                    account_type=str(raw.get("account_type") or raw.get("type") or ""),
                    status=str(raw.get("status") or "unknown"),
                    status_message=redact_text(raw.get("status_message") or "", 400),
                    disabled=bool(raw.get("disabled", False)),
                    unavailable=bool(raw.get("unavailable", False)),
                    runtime_only=bool(raw.get("runtime_only", False)),
                    source=str(raw.get("source") or ""),
                    last_refresh=str(raw.get("modtime") or raw.get("last_refresh") or "") or None,
                    success_count=int(raw.get("success") or 0),
                    failure_count=int(raw.get("failed") or 0),
                )
            )
        if include_models and accounts:
            semaphore = asyncio.Semaphore(6)

            async def load(account: CLIProxyAccount) -> None:
                async with semaphore:
                    try:
                        account.models = await self.account_models(account.name, account.platform)
                    except CLIProxyUpstreamError:
                        account.models = []

            await asyncio.gather(*(load(account) for account in accounts))
        return accounts

    async def account_models(self, name: str, platform: str = "unknown") -> List[CLIProxyModel]:
        safe_name = _validate_identifier(name, "account name")
        payload = await self._request("GET", "auth-files/models", params={"name": safe_name})
        raw_models = payload.get("models", []) if isinstance(payload, dict) else []
        return [model for item in raw_models if (model := normalize_model(platform, item)) is not None]

    async def set_account_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        safe_name = _validate_identifier(name, "account name")
        result = await self._request(
            "PATCH", "auth-files/status", json_body={"name": safe_name, "disabled": not bool(enabled)}
        )
        return redact_structure(result)

    async def delete_account(self, name: str) -> Dict[str, Any]:
        safe_name = _validate_identifier(name, "account name")
        return redact_structure(await self._request("DELETE", "auth-files", params={"name": safe_name}))

    async def import_credential(
        self, *, platform: str, filename: str, content: bytes, location: str = "us-central1"
    ) -> Dict[str, Any]:
        canonical = _validate_platform(platform)
        prepared = prepare_credential_imports(
            platform=canonical,
            filename=filename,
            content=content,
        )
        if len(prepared) != 1:
            raise HTTPException(
                status_code=400,
                detail="Use import_credential_file for a multi-account credential bundle",
            )
        return await self._upload_prepared_credential(canonical, prepared[0], location)

    async def _upload_prepared_credential(
        self, platform: str, credential: _PreparedCredential, location: str
    ) -> Dict[str, Any]:
        safe_name = credential.name
        content = credential.content
        canonical = _validate_platform(platform)
        if canonical == "vertex":
            safe_location = str(location or "us-central1").strip()
            if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", safe_location):
                raise HTTPException(status_code=400, detail="Invalid Vertex location")
            result = await self._request(
                "POST",
                "vertex/import",
                files={"file": (safe_name, content, "application/json")},
                data={"location": safe_location},
                timeout=45.0,
            )
        else:
            result = await self._request(
                "POST", "auth-files", params={"name": safe_name}, content=content, timeout=45.0
            )
        return redact_structure(result)

    async def import_credential_file(
        self, *, platform: str, filename: str, content: bytes, location: str = "us-central1"
    ) -> CLIProxyCredentialImportResponse:
        canonical = _validate_platform(platform)
        prepared = prepare_credential_imports(
            platform=canonical,
            filename=filename,
            content=content,
        )
        semaphore = asyncio.Semaphore(4)

        async def upload(credential: _PreparedCredential) -> CLIProxyCredentialImportItem:
            try:
                async with semaphore:
                    await self._upload_prepared_credential(canonical, credential, location)
                return CLIProxyCredentialImportItem(
                    name=credential.name,
                    email=credential.email,
                    status="imported",
                )
            except CLIProxyUpstreamError as exc:
                return CLIProxyCredentialImportItem(
                    name=credential.name,
                    email=credential.email,
                    status="failed",
                    error=redact_text(exc.detail, 400),
                )

        items = await asyncio.gather(*(upload(credential) for credential in prepared))
        imported = sum(1 for item in items if item.status == "imported")
        failed = len(items) - imported
        return CLIProxyCredentialImportResponse(
            success=failed == 0,
            platform=canonical,
            source_name=str(filename or "credential.json")[:255],
            total=len(items),
            imported=imported,
            failed=failed,
            items=items,
        )

    async def import_api_key(self, request: CLIProxyApiKeyImport) -> Dict[str, Any]:
        provider = _validate_platform(request.provider)
        prefix = str(request.prefix or platform_namespace(provider)).strip().lower()
        if not _SAFE_PLATFORM.fullmatch(prefix):
            raise HTTPException(status_code=400, detail="Invalid model prefix")
        models = [str(model).strip() for model in request.models if str(model).strip()]

        if provider in {"openai-compatible", "custom"}:
            if not request.name.strip() or not request.base_url.strip():
                raise HTTPException(status_code=400, detail="Custom provider name and base URL are required")
            endpoint = "openai-compatibility"
            payload = await self._request("GET", endpoint)
            entries = list(payload.get(endpoint, [])) if isinstance(payload, dict) else []
            provider_name = _validate_platform(request.name)
            existing = next((item for item in entries if str(item.get("name") or "") == provider_name), None)
            key_entry = {"api-key": request.api_key, "prefix": prefix}
            mapped_models = [
                {"name": model, "alias": namespaced_model(prefix, model), "force-mapping": True}
                for model in models
            ]
            if existing is None:
                entries.append(
                    {
                        "name": provider_name,
                        "base-url": request.base_url.rstrip("/"),
                        "api-key-entries": [key_entry],
                        "models": mapped_models,
                    }
                )
            else:
                existing.setdefault("api-key-entries", []).append(key_entry)
                if mapped_models:
                    existing["models"] = mapped_models
            await self._request("PUT", endpoint, json_body=entries)
            return {"status": "ok", "provider": provider_name}

        endpoint = API_KEY_COLLECTIONS.get(provider)
        if not endpoint:
            raise HTTPException(status_code=400, detail=f"API-key import is not supported for {provider}")
        payload = await self._request("GET", endpoint)
        entries = list(payload.get(endpoint, [])) if isinstance(payload, dict) else []
        entry: Dict[str, Any] = {"api-key": request.api_key, "prefix": prefix}
        if request.base_url.strip():
            entry["base-url"] = request.base_url.rstrip("/")
        if models:
            entry["models"] = [
                {"name": model, "alias": namespaced_model(provider, model), "force-mapping": True}
                for model in models
            ]
        entries.append(entry)
        await self._request("PUT", endpoint, json_body=entries)
        return {"status": "ok", "provider": provider}

    async def start_oauth(self, provider: str) -> CLIProxyOAuthSession:
        canonical = _validate_platform(provider)
        endpoint = OAUTH_ENDPOINTS.get(canonical)
        if not endpoint:
            raise HTTPException(status_code=400, detail=f"OAuth is not supported for {canonical}")
        params = {"is_webui": "true"} if canonical in {"codex", "claude", "antigravity"} else None
        payload = await self._request("GET", endpoint, params=params, timeout=45.0)
        return CLIProxyOAuthSession(
            provider=canonical,
            status=str(payload.get("status") or "error"),
            state=str(payload.get("state") or ""),
            url=str(payload.get("url") or ""),
            flow=str(payload.get("flow") or "browser"),
            user_code=str(payload.get("user_code") or ""),
            expires_in=payload.get("expires_in"),
            error=redact_text(payload.get("error") or "", 400),
        )

    async def oauth_status(self, state: str, provider: str = "") -> CLIProxyOAuthSession:
        safe_state = _validate_identifier(state, "OAuth state")
        payload = await self._request("GET", "get-auth-status", params={"state": safe_state})
        return CLIProxyOAuthSession(
            provider=canonical_platform(provider),
            status=str(payload.get("status") or "error"),
            state=safe_state,
            error=redact_text(payload.get("error") or "", 400),
        )

    async def cancel_oauth(self, state: str) -> Dict[str, Any]:
        safe_state = _validate_identifier(state, "OAuth state")
        return redact_structure(
            await self._request("DELETE", "oauth-session", params={"state": safe_state})
        )

    async def model_definitions(self, platform: str) -> List[CLIProxyModel]:
        canonical = _validate_platform(platform)
        payload = await self._request("GET", f"model-definitions/{canonical}")
        raw_models = payload.get("models", []) if isinstance(payload, dict) else []
        return [model for item in raw_models if (model := normalize_model(canonical, item)) is not None]

    async def list_aliases(self) -> Dict[str, List[CLIProxyAlias]]:
        payload = await self._request("GET", "oauth-model-alias")
        raw_map = payload.get("oauth-model-alias", {}) if isinstance(payload, dict) else {}
        result: Dict[str, List[CLIProxyAlias]] = {}
        for channel, raw_aliases in raw_map.items() if isinstance(raw_map, dict) else []:
            aliases: List[CLIProxyAlias] = []
            for item in raw_aliases if isinstance(raw_aliases, list) else []:
                if not isinstance(item, dict) or not item.get("name") or not item.get("alias"):
                    continue
                aliases.append(
                    CLIProxyAlias(
                        name=str(item["name"]),
                        alias=str(item["alias"]),
                        fork=bool(item.get("fork", False)),
                        display_name=str(item.get("display-name") or item.get("display_name") or ""),
                        force_mapping=bool(item.get("force-mapping", item.get("force_mapping", True))),
                    )
                )
            result[str(channel)] = aliases
        return result

    async def replace_aliases(self, platform: str, aliases: Iterable[CLIProxyAlias]) -> Dict[str, Any]:
        channel = _validate_platform(platform)
        body = {
            "channel": channel,
            "aliases": [
                {
                    "name": alias.name,
                    "alias": alias.alias,
                    "fork": alias.fork,
                    "display-name": alias.display_name,
                    "force-mapping": alias.force_mapping,
                }
                for alias in aliases
            ],
        }
        self._prepared_aliases.clear()
        return redact_structure(await self._request("PATCH", "oauth-model-alias", json_body=body))

    async def delete_aliases(self, platform: str) -> Dict[str, Any]:
        channel = _validate_platform(platform)
        self._prepared_aliases.clear()
        return redact_structure(
            await self._request("DELETE", "oauth-model-alias", params={"channel": channel})
        )

    async def ensure_namespaced_alias(self, model: str) -> None:
        split = split_gateway_model(model)
        if split is None or model in self._prepared_aliases or not self.configured:
            return
        namespace, raw_model = split
        async with self._alias_lock:
            if model in self._prepared_aliases:
                return
            aliases_by_channel = await self.list_aliases()
            for channel in NAMESPACE_CHANNELS[namespace]:
                current = aliases_by_channel.get(channel, [])
                if any(alias.alias == model and alias.name == raw_model for alias in current):
                    continue
                current.append(
                    CLIProxyAlias(
                        name=raw_model,
                        alias=model,
                        display_name=model,
                        force_mapping=True,
                    )
                )
                try:
                    await self.replace_aliases(channel, current)
                except CLIProxyUpstreamError as exc:
                    if exc.status_code not in {400, 404}:
                        raise
            self._prepared_aliases.add(model)

    async def exclusions(self) -> Dict[str, List[str]]:
        payload = await self._request("GET", "oauth-excluded-models")
        raw = payload.get("oauth-excluded-models", payload) if isinstance(payload, dict) else {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(provider): [str(model) for model in models if str(model).strip()]
            for provider, models in raw.items()
            if isinstance(models, list)
        }

    async def replace_exclusions(self, platform: str, models: List[str]) -> Dict[str, Any]:
        provider = _validate_platform(platform)
        return redact_structure(
            await self._request(
                "PATCH",
                "oauth-excluded-models",
                json_body={"provider": provider, "models": [str(model).strip() for model in models if str(model).strip()]},
            )
        )

    async def delete_exclusions(self, platform: str) -> Dict[str, Any]:
        provider = _validate_platform(platform)
        return redact_structure(
            await self._request("DELETE", "oauth-excluded-models", params={"provider": provider})
        )

    async def routing(self) -> CLIProxyRouting:
        strategy_payload, retry_payload, interval_payload = await asyncio.gather(
            self._request("GET", "routing/strategy"),
            self._request("GET", "request-retry"),
            self._request("GET", "max-retry-interval"),
        )
        return CLIProxyRouting(
            strategy=str(strategy_payload.get("strategy") or "round-robin"),
            retry_count=max(0, min(10, int(retry_payload.get("request-retry") or 0))),
            max_retry_interval_seconds=max(
                0, min(300, int(interval_payload.get("max-retry-interval") or 0))
            ),
        )

    async def set_routing(
        self,
        strategy: Optional[str] = None,
        retry_count: Optional[int] = None,
        max_retry_interval_seconds: Optional[int] = None,
    ) -> CLIProxyRouting:
        if strategy is None and retry_count is None and max_retry_interval_seconds is None:
            raise HTTPException(status_code=400, detail="At least one routing setting is required")
        if strategy is not None:
            normalized = str(strategy or "").strip().lower()
            if normalized not in {"round-robin", "fill-first"}:
                raise HTTPException(status_code=400, detail="Routing strategy must be round-robin or fill-first")
            await self._request("PATCH", "routing/strategy", json_body={"value": normalized})
        if retry_count is not None:
            normalized_retry = max(0, min(10, int(retry_count)))
            await self._request("PATCH", "request-retry", json_body={"value": normalized_retry})
        if max_retry_interval_seconds is not None:
            normalized_interval = max(0, min(300, int(max_retry_interval_seconds)))
            await self._request(
                "PATCH", "max-retry-interval", json_body={"value": normalized_interval}
            )
        return await self.routing()

    async def reset_quota(self, auth_index: str) -> Dict[str, Any]:
        safe_index = _validate_identifier(auth_index, "auth index")
        return redact_structure(
            await self._request("POST", "reset-quota", json_body={"auth_index": safe_index})
        )

    async def logs(self, *, limit: int = 200, cursor: str = "") -> CLIProxyLogs:
        safe_limit = max(1, min(1000, int(limit)))
        params: Dict[str, Any] = {"limit": safe_limit}
        if cursor:
            params["cursor"] = str(cursor)[:500]
        payload = await self._request("GET", "logs", params=params)
        raw_lines = payload.get("lines", []) if isinstance(payload, dict) else []
        lines = [redact_text(line, 4000) for line in raw_lines if isinstance(line, str)]
        return CLIProxyLogs(
            lines=lines,
            cursor=str(payload.get("next-cursor") or payload.get("cursor") or cursor or ""),
            cursor_reset=bool(payload.get("cursor-reset", False)),
            line_count=int(payload.get("line-count") or len(lines)),
        )

    async def platforms(self) -> List[CLIProxyPlatform]:
        accounts = await self.list_accounts(include_models=False)
        account_platforms = {account.platform for account in accounts}
        platform_ids = list(KNOWN_PLATFORM_DEFINITIONS)
        platform_ids.extend(sorted(account_platforms - set(platform_ids)))
        results: List[CLIProxyPlatform] = []
        for platform in platform_ids:
            definition = KNOWN_PLATFORM_DEFINITIONS.get(
                platform,
                {"label": platform.replace("-", " ").title(), "namespace": platform, "oauth": False, "imports": ["credential"]},
            )
            matching = [account for account in accounts if account.platform == platform]
            try:
                models = await self.model_definitions(platform)
                error = ""
            except CLIProxyUpstreamError as exc:
                models = []
                error = exc.detail if matching else ""
                if matching:
                    for account in matching:
                        try:
                            models.extend(await self.account_models(account.name, platform))
                        except CLIProxyUpstreamError:
                            continue
                    models = list({model.id: model for model in models}.values())
            results.append(
                CLIProxyPlatform(
                    id=platform,
                    label=str(definition["label"]),
                    namespace=str(definition["namespace"]),
                    oauth=bool(definition["oauth"]),
                    import_types=list(definition["imports"]),
                    account_count=len(matching),
                    healthy_count=sum(
                        1 for account in matching if not account.disabled and not account.unavailable
                    ),
                    models=models,
                    error=redact_text(error, 300),
                )
            )
        return results

    async def overview(self) -> CLIProxyOverview:
        if not self.configured:
            return CLIProxyOverview(
                configured=False,
                reachable=False,
                version=config.cliproxy_version,
                public_url=config.cliproxy_public_url,
                message="CLIProxy environment variables are not configured",
            )
        try:
            accounts, routing = await asyncio.gather(self.list_accounts(), self.routing())
        except CLIProxyUpstreamError as exc:
            return CLIProxyOverview(
                configured=True,
                reachable=False,
                version=config.cliproxy_version,
                public_url=config.cliproxy_public_url,
                message=exc.detail,
            )
        healthy = [a for a in accounts if not a.disabled and not a.unavailable]
        return CLIProxyOverview(
            configured=True,
            reachable=True,
            version=config.cliproxy_version,
            public_url=config.cliproxy_public_url,
            routing_strategy=routing.strategy,
            platform_count=len({a.platform for a in accounts}),
            account_count=len(accounts),
            healthy_count=len(healthy),
            unavailable_count=sum(1 for a in accounts if a.unavailable),
            disabled_count=sum(1 for a in accounts if a.disabled),
            message="CLIProxy is reachable",
        )


class CLIProxyInferenceClient:
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        management: Optional[CLIProxyManagementClient] = None,
    ):
        self.base_url = str(base_url if base_url is not None else config.cliproxy_base_url).rstrip("/")
        self.api_key = str(api_key if api_key is not None else config.cliproxy_api_key).strip()
        self.management = management or CLIProxyManagementClient(base_url=self.base_url)

    async def chat_json(
        self,
        *,
        model: str,
        prompt_text: str,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/jpeg",
        timeout: float = 120.0,
        max_tokens: int = 8192,
    ) -> Dict[str, Any]:
        if not self.base_url or not self.api_key:
            raise HTTPException(status_code=503, detail="CLIProxy inference is not configured")
        selected_model = str(model or "").strip()
        if not selected_model:
            raise HTTPException(status_code=400, detail="CLIProxy model is required")
        try:
            await self.management.ensure_namespaced_alias(selected_model)
        except CLIProxyUpstreamError as exc:
            raise HTTPException(status_code=502, detail=exc.detail) from exc

        content: Any = prompt_text
        if image_bytes:
            encoded = base64.b64encode(image_bytes).decode("ascii")
            content = [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
                {"type": "text", "text": prompt_text},
            ]
        payload = {
            "model": selected_model,
            "stream": False,
            "max_tokens": max(1, min(8192, int(max_tokens))),
            "messages": [{"role": "user", "content": content}],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="CLIProxy inference timed out") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"CLIProxy inference connection failed: {redact_text(exc)}") from exc
        if response.status_code >= 400:
            mapped = response.status_code if response.status_code in {401, 403, 429} else 502
            raise HTTPException(
                status_code=mapped,
                detail=f"CLIProxy inference HTTP {response.status_code}: {redact_text(response.text)}",
            )
        try:
            data = response.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail="CLIProxy inference returned invalid JSON") from exc
        message = (((data or {}).get("choices") or [{}])[0].get("message") or {})
        content_out = message.get("content") or ""
        if isinstance(content_out, list):
            content_out = "\n".join(
                str(item.get("text") or "") for item in content_out if isinstance(item, dict)
            )
        if not str(content_out).strip():
            raise HTTPException(status_code=422, detail="CLIProxy returned empty JSON content")
        return _extract_json_object(str(content_out))

    async def connectivity_test(self, model: str) -> Dict[str, Any]:
        result = await self.chat_json(
            model=model,
            prompt_text='Return exactly one JSON object: {"ok": true}',
            timeout=45.0,
            max_tokens=32,
        )
        return {"ok": bool(result.get("ok", True)), "model": model}


def management_error_to_http(exc: CLIProxyUpstreamError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)
