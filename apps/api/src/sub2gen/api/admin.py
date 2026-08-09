"""Admin API routes"""

import asyncio
import csv
import inspect
import json
import mimetypes
import hashlib
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from typing import Optional, List, Dict, Any
import secrets
import time
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse
from curl_cffi.requests import AsyncSession
from ..core.auth import AuthManager
from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.database import Database
from ..core.config import config, get_runtime_data_dir, get_yescaptcha_min_score, normalize_yescaptcha_task_type
from ..core.models import GenerationConfig, Token
from ..core.browser_runtime_status import (
    fail_runtime_prepare,
    finish_runtime_prepare,
    get_runtime_status,
    progress_runtime_prepare,
    start_runtime_prepare,
)
from ..core.monitoring import build_public_health_snapshot
from ..services.proxy_manager import ProxyManager
from ..services.runway_service import RunwayService
from ..services.geminigen_service import (
    GEMINIGEN_GROK_IMAGE_MAX_DAILY,
    GEMINIGEN_IMAGE_GEN_MAX_DAILY,
    GeminiGenService,
)
from ..services.generation_handler import MODEL_CONFIG, GenerationHandler
from ..services.browser_profile_service import BrowserProfileService
from ..services.browser_metrics_cleanup import get_last_browser_metrics_cleanup_stats
from ..services.google_drive_backup import GoogleDriveBackupError, GoogleDriveBackupService
from ..services.redis_runtime import RedisUnavailableError, redis_runtime

try:
    import httpx
except ImportError:
    httpx = None

router = APIRouter()

# Admin session TTLs (seconds)
_ADMIN_SESSION_TTL_REMEMBER = 30 * 24 * 3600  # 30 days when "remember me" is on
_ADMIN_SESSION_TTL_BROWSER = 24 * 3600  # 24 hours when off
ADMIN_SESSION_COOKIE_NAME = "admin_session"

SUPPORTED_API_CAPTCHA_METHODS = {"yescaptcha", "capmonster", "ezcaptcha", "capsolver"}
MAX_SQLITE_RESTORE_BYTES = 512 * 1024 * 1024
REQUIRED_SQLITE_RESTORE_TABLES = {"admin_config", "tokens"}


def _generate_captcha_worker_key() -> str:
    return f"cwk_{secrets.token_urlsafe(32)}"


def _hash_worker_registration_key(raw_key: str) -> str:
    return hashlib.sha256((raw_key or "").encode("utf-8")).hexdigest()


def _worker_key_prefix(raw_key: str) -> str:
    key = (raw_key or "").strip()
    if len(key) <= 12:
        return key
    return f"{key[:8]}...{key[-4:]}"


def _mask_token(token: Optional[str]) -> str:
    if not token:
        return ""
    if len(token) <= 24:
        return token
    return f"{token[:18]}...{token[-8:]}"


def _truncate_text(text: Any, limit: int = 240) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def _to_iso(value: Any) -> Any:
    """Serialize date-like values while preserving already-serialized values."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def _validate_uploaded_sqlite_database(path: Path) -> Dict[str, Any]:
    """Validate a candidate sub2gen SQLite DB before swapping it into place."""
    if not path.exists() or path.stat().st_size <= 0:
        raise HTTPException(status_code=400, detail="Uploaded database is empty")

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity = row[0] if row else ""
            if integrity != "ok":
                raise HTTPException(
                    status_code=400,
                    detail=f"SQLite integrity check failed: {integrity or 'unknown'}",
                )

            table_rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            tables = {str(r[0]) for r in table_rows}
    except HTTPException:
        raise
    except sqlite3.DatabaseError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid SQLite database: {exc}") from exc

    missing = REQUIRED_SQLITE_RESTORE_TABLES - tables
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Uploaded database is missing required table(s): {', '.join(sorted(missing))}",
        )

    return {"size": path.stat().st_size, "table_count": len(tables)}


def _create_sqlite_database_snapshot(source_path: Path, snapshot_path: Path) -> None:
    """Create a consistent SQLite backup snapshot without copying a live DB file."""
    source_uri = f"file:{source_path}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source:
            with sqlite3.connect(snapshot_path) as target:
                source.backup(target)
    except sqlite3.DatabaseError as exc:
        raise HTTPException(status_code=500, detail=f"Database snapshot failed: {exc}") from exc


def _unlink_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _extract_error_summary(payload: Any) -> str:
    """从响应体里提取用户可读的错误摘要。"""
    if payload is None:
        return ""

    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            return ""
        try:
            return _extract_error_summary(json.loads(raw))
        except Exception:
            return _truncate_text(raw)

    if isinstance(payload, dict):
        for key in ("error_summary", "error_message", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_text(value)

        error_value = payload.get("error")
        if isinstance(error_value, dict):
            for key in ("message", "detail", "reason", "code"):
                value = error_value.get(key)
                if isinstance(value, str) and value.strip():
                    return _truncate_text(value)
        elif isinstance(error_value, str) and error_value.strip():
            return _truncate_text(error_value)

        for nested_key in ("response", "data"):
            nested = payload.get(nested_key)
            if isinstance(nested, (dict, list, str)):
                summary = _extract_error_summary(nested)
                if summary:
                    return summary

        return ""

    if isinstance(payload, list):
        for item in payload:
            summary = _extract_error_summary(item)
            if summary:
                return summary
        return ""

    return _truncate_text(payload)


def _extract_log_job_id(*payloads: Any) -> str:
    def visit(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ""
            try:
                return visit(json.loads(text))
            except Exception:
                return ""
        if isinstance(value, dict):
            for key in ("job_id", "request_id", "task_id", "upstream_task_id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for key in ("content", "response", "data", "task", "operation", "performance"):
                nested = visit(value.get(key))
                if nested:
                    return nested
        if isinstance(value, list):
            for item in value:
                nested = visit(item)
                if nested:
                    return nested
        return ""

    for payload in payloads:
        found = visit(payload)
        if found:
            return found
    return ""


def _extract_captcha_user_agent_metadata(*payloads: Any) -> Dict[str, Any]:
    """Extract the persisted provider-UA marker without exposing the raw User-Agent."""

    def visit(value: Any) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return visit(json.loads(text))
            except Exception:
                if not re.search(r'"captcha_user_agent_set"\s*:\s*true', text, re.IGNORECASE):
                    return None
                provider_match = re.search(
                    r'"captcha_provider"\s*:\s*"([^"\\]+)"',
                    text,
                    re.IGNORECASE,
                )
                return {
                    "captcha_user_agent_set": True,
                    "captcha_provider": provider_match.group(1).strip() if provider_match else None,
                }
        if isinstance(value, dict):
            if value.get("captcha_user_agent_set") is True:
                provider = str(value.get("captcha_provider") or "").strip() or None
                return {
                    "captcha_user_agent_set": True,
                    "captcha_provider": provider,
                }
            for nested in value.values():
                found = visit(nested)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = visit(item)
                if found:
                    return found
        return None

    for payload in payloads:
        found = visit(payload)
        if found:
            return found
    return {"captcha_user_agent_set": False, "captcha_provider": None}


def _guess_client_hints_from_user_agent(user_agent: str) -> Dict[str, str]:
    """根据 UA 补全常见的 sec-ch-* 头。"""
    ua = (user_agent or "").strip()
    if not ua:
        return {}

    headers: Dict[str, str] = {}
    major_match = re.search(r"(?:Chrome|Chromium|Edg|EdgA|EdgiOS)/(\d+)", ua)
    is_mobile = any(token in ua for token in ("Android", "iPhone", "iPad", "Mobile"))
    headers["sec-ch-ua-mobile"] = "?1" if is_mobile else "?0"

    if "Windows" in ua:
        headers["sec-ch-ua-platform"] = '"Windows"'
    elif "Macintosh" in ua or "Mac OS X" in ua:
        headers["sec-ch-ua-platform"] = '"macOS"'
    elif "Android" in ua:
        headers["sec-ch-ua-platform"] = '"Android"'
    elif "iPhone" in ua or "iPad" in ua:
        headers["sec-ch-ua-platform"] = '"iOS"'
    elif "Linux" in ua:
        headers["sec-ch-ua-platform"] = '"Linux"'

    if major_match:
        major = major_match.group(1)
        if "Edg/" in ua:
            headers["sec-ch-ua"] = f'"Not:A-Brand";v="99", "Microsoft Edge";v="{major}", "Chromium";v="{major}"'
        else:
            headers["sec-ch-ua"] = f'"Not:A-Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'

    return headers


def _guess_impersonate_from_user_agent(user_agent: str) -> str:
    """从 UA 选择可用的 curl_cffi 浏览器指纹版本。"""
    ua = (user_agent or "").strip()
    major_match = re.search(r"(?:Chrome|Chromium|Edg|EdgA|EdgiOS)/(\d+)", ua)
    if not major_match:
        return "chrome120"

    try:
        major = int(major_match.group(1))
    except Exception:
        return "chrome120"

    if major >= 124:
        return "chrome124"
    if major >= 120:
        return "chrome120"
    return "chrome120"


def _build_proxy_map(proxy_url: str) -> Optional[Dict[str, str]]:
    normalized = (proxy_url or "").strip()
    if not normalized:
        return None
    return {"http": normalized, "https": normalized}


def _normalize_http_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        raise RuntimeError("远程打码服务地址未配置")

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("远程打码服务地址格式错误，必须是 http(s)://host[:port]")

    return normalized


def _get_remote_browser_client_config() -> tuple[str, str, int]:
    base_url = _normalize_http_base_url(config.remote_browser_base_url)
    api_key = (config.remote_browser_api_key or "").strip()
    if not api_key:
        raise RuntimeError("远程打码服务 API Key 未配置")
    timeout = max(5, int(config.remote_browser_timeout or 60))
    return base_url, api_key, timeout


def _build_remote_browser_http_timeout(read_timeout: float) -> Any:
    read_value = max(3.0, float(read_timeout))
    write_value = min(10.0, max(3.0, read_value))
    if httpx is None:
        return read_value
    return httpx.Timeout(
        connect=2.5,
        read=read_value,
        write=write_value,
        pool=2.5,
    )


def _parse_json_response_text(text: str) -> Optional[Any]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


async def _stdlib_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
) -> tuple[int, Optional[Any], str]:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    request_method = (method or "GET").upper()
    request_data: Optional[bytes] = None

    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_data = json.dumps(payload).encode("utf-8")

    def do_request() -> tuple[int, str]:
        request = urllib.request.Request(
            url=url,
            data=request_data,
            headers=req_headers,
            method=request_method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=max(1.0, float(timeout))) as response:
                status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return status_code, body.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            charset = exc.headers.get_content_charset() if exc.headers else None
            return int(getattr(exc, "code", 0) or 0), body.decode(charset or "utf-8", errors="replace")

    try:
        status_code, text = await asyncio.to_thread(do_request)
    except Exception as e:
        raise RuntimeError(f"远程打码服务请求失败: {e}") from e

    return status_code, _parse_json_response_text(text), text


async def _sync_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
) -> tuple[int, Optional[Any], str]:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    request_method = (method or "GET").upper()
    request_kwargs: Dict[str, Any] = {
        "headers": req_headers,
        "timeout": _build_remote_browser_http_timeout(timeout),
    }

    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_kwargs["json"] = payload

    if httpx is None:
        return await _stdlib_json_http_request(
            method=method,
            url=url,
            headers=req_headers,
            payload=payload,
            timeout=timeout,
        )

    try:
        # remote_browser 控制面是服务间 JSON API，使用 httpx 避免 curl_cffi 在当前
        # Windows + impersonate 场景下 POST body 丢失导致 FastAPI 直接判定 body 缺失。
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as session:
            response = await session.request(
                method=request_method,
                url=url,
                **request_kwargs,
            )
    except Exception as e:
        raise RuntimeError(f"远程打码服务请求失败: {e}") from e

    status_code = int(getattr(response, "status_code", 0) or 0)
    text = response.text or ""
    parsed = _parse_json_response_text(text)

    return status_code, parsed, text


async def _resolve_score_test_verify_proxy(
    captcha_method: str,
    browser_proxy_enabled: bool,
    browser_proxy_url: str,
    proxy_manager: ProxyManager,
) -> tuple[Optional[Dict[str, str]], bool, str, str]:
    """
    选择 score-test 的 verify 请求代理，优先与浏览器打码代理保持一致。
    返回: (proxies, used, source, proxy_url)
    """
    # 浏览器打码模式优先使用 browser_proxy，确保与取 token 出口一致
    if captcha_method in {"browser", "personal"} and browser_proxy_enabled and browser_proxy_url:
        proxy_map = _build_proxy_map(browser_proxy_url)
        if proxy_map:
            return proxy_map, True, "captcha_browser_proxy", browser_proxy_url

    # 退回请求代理配置
    try:
        if proxy_manager:
            proxy_cfg = await proxy_manager.get_proxy_config()
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                proxy_map = _build_proxy_map(proxy_cfg.proxy_url)
                if proxy_map:
                    return proxy_map, True, "request_proxy", proxy_cfg.proxy_url
    except Exception:
        pass

    return None, False, "none", ""


async def _solve_recaptcha_with_api_service(
    method: str,
    website_url: str,
    website_key: str,
    action: str,
    enterprise: bool = False,
    proxy_manager: Optional[ProxyManager] = None,
) -> Optional[str]:
    """使用当前配置的第三方打码服务获取 token。"""
    if method == "yescaptcha":
        client_key = config.yescaptcha_api_key
        base_url = config.yescaptcha_base_url
        task_type = config.yescaptcha_task_type
        min_score = get_yescaptcha_min_score(task_type)
    elif method == "capmonster":
        client_key = config.capmonster_api_key
        base_url = config.capmonster_base_url
        task_type = "RecaptchaV3EnterpriseTask"
        min_score = config.capmonster_min_score
    elif method == "ezcaptcha":
        client_key = config.ezcaptcha_api_key
        base_url = config.ezcaptcha_base_url
        task_type = "ReCaptchaV3TaskProxylessS9"
        min_score = None
    elif method == "capsolver":
        client_key = config.capsolver_api_key
        base_url = config.capsolver_base_url
        task_type = "ReCaptchaV3EnterpriseTaskProxyLess" if enterprise else "ReCaptchaV3TaskProxyLess"
        min_score = None
    else:
        raise RuntimeError(f"不支持的打码方式: {method}")

    if not client_key:
        raise RuntimeError(f"{method} API Key 未配置")

    task: Dict[str, Any] = {
        "websiteURL": website_url,
        "websiteKey": website_key,
        "type": task_type,
        "pageAction": action,
    }
    if min_score is not None:
        task["minScore"] = min_score

    if enterprise and method == "capsolver":
        task["isEnterprise"] = True

    create_url = f"{base_url.rstrip('/')}/createTask"
    get_url = f"{base_url.rstrip('/')}/getTaskResult"

    proxies = None
    try:
        if proxy_manager:
            proxy_cfg = await proxy_manager.get_proxy_config()
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                proxies = _build_proxy_map(proxy_cfg.proxy_url)
    except Exception:
        pass

    # Do not use curl_cffi impersonation for captcha API JSON endpoints: some ASGI servers
    # (for example FastAPI/Uvicorn) may receive an empty body and return 422.
    async with AsyncSession() as session:
        create_resp = await session.post(
            create_url,
            json={"clientKey": client_key, "task": task},
            timeout=30,
            proxies=proxies,
        )
        create_json = create_resp.json()
        task_id = create_json.get("taskId")

        if not task_id:
            error_desc = create_json.get("errorDescription") or create_json.get("errorMessage") or str(create_json)
            raise RuntimeError(f"{method} createTask 失败: {error_desc}")

        for _ in range(40):
            poll_resp = await session.post(
                get_url,
                json={"clientKey": client_key, "taskId": task_id},
                timeout=30,
                proxies=proxies,
            )
            poll_json = poll_resp.json()
            if poll_json.get("status") == "ready":
                solution = poll_json.get("solution", {}) or {}
                token = solution.get("gRecaptchaResponse") or solution.get("token")
                if token:
                    return token
                raise RuntimeError(f"{method} 返回结果缺少 token: {poll_json}")

            if poll_json.get("errorId") not in (None, 0):
                error_desc = poll_json.get("errorDescription") or poll_json.get("errorMessage") or str(poll_json)
                raise RuntimeError(f"{method} getTaskResult 失败: {error_desc}")

            await asyncio.sleep(3)

    raise RuntimeError(f"{method} 获取 token 超时")


async def _score_test_with_remote_browser_service(
    website_url: str,
    website_key: str,
    verify_url: str,
    action: str,
    enterprise: bool = False,
) -> Dict[str, Any]:
    """调用远程有头打码服务执行页面内打码+分数校验。"""
    base_url, api_key, timeout = _get_remote_browser_client_config()
    endpoint = f"{base_url}/api/v1/custom-score"
    request_payload = {
        "website_url": website_url,
        "website_key": website_key,
        "verify_url": verify_url,
        "action": action,
        "enterprise": enterprise,
    }

    status_code, response_payload, response_text = await _sync_json_http_request(
        method="POST",
        url=endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        payload=request_payload,
        timeout=timeout,
    )

    if status_code >= 400:
        detail = ""
        if isinstance(response_payload, dict):
            detail = response_payload.get("detail") or response_payload.get("message") or str(response_payload)
        if not detail:
            detail = (response_text or "").strip()
        raise RuntimeError(f"远程打码服务请求失败 (HTTP {status_code}): {detail or '未知错误'}")

    if not isinstance(response_payload, dict):
        raise RuntimeError("远程打码服务返回格式错误")
    return response_payload


async def _probe_agent_gateway_mode(base_url: str) -> Dict[str, Any]:
    normalized = _normalize_http_base_url(base_url)
    health_url = f"{normalized}/health"
    status_code, response_payload, response_text = await _sync_json_http_request(
        method="GET",
        url=health_url,
        headers={},
        payload=None,
        timeout=8,
    )
    if status_code >= 400:
        detail = ""
        if isinstance(response_payload, dict):
            detail = str(response_payload.get("detail") or response_payload.get("message") or "").strip()
        if not detail:
            detail = (response_text or "").strip()
        raise RuntimeError(f"HTTP {status_code}{f': {detail}' if detail else ''}")

    if not isinstance(response_payload, dict):
        raise RuntimeError("health 返回格式错误")

    return {
        "ok": bool(response_payload.get("ok")),
        "service": str(response_payload.get("service") or ""),
        "agent_auth_mode": str(response_payload.get("auth_mode") or "").strip().lower(),
        "keygen_verify_mode": str(response_payload.get("verify_mode") or "").strip().lower(),
        "health_body": response_payload,
    }


async def _fetch_agent_gateway_connections(base_url: str, api_key: str) -> Dict[str, Any]:
    normalized = _normalize_http_base_url(base_url)
    endpoint = f"{normalized}/api/v1/agents"
    status_code, response_payload, response_text = await _sync_json_http_request(
        method="GET",
        url=endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        payload=None,
        timeout=8,
    )
    if status_code >= 400:
        detail = ""
        if isinstance(response_payload, dict):
            detail = str(response_payload.get("detail") or response_payload.get("message") or "").strip()
        if not detail:
            detail = (response_text or "").strip()
        raise RuntimeError(f"HTTP {status_code}{f': {detail}' if detail else ''}")
    if not isinstance(response_payload, dict):
        raise RuntimeError("agents 返回格式错误")
    return response_payload


def _supports_kwarg(fn: Any, kwarg: str) -> bool:
    """Best-effort compatibility helper for rolling upgrades."""
    try:
        return kwarg in inspect.signature(fn).parameters
    except Exception:
        return False


def _normalize_runtime_method(method: Optional[str]) -> str:
    normalized = str(method or "").strip().lower()
    if normalized not in {"browser", "personal"}:
        raise HTTPException(status_code=400, detail="Invalid runtime method")
    return normalized


async def _prepare_captcha_runtime(method: str, container: AppContainer) -> None:
    runtime_method = _normalize_runtime_method(method)
    start_runtime_prepare(runtime_method, f"Preparing {runtime_method} captcha runtime")
    try:
        if runtime_method == "browser":
            from ..services.browser_captcha import BrowserCaptchaService

            service = await BrowserCaptchaService.get_instance(container.db)
            progress_runtime_prepare(runtime_method, "Warming headed browser slots")
            await service.reload_browser_count()
            await service.warmup_browser_slots()
        else:
            from ..services.browser_captcha_personal import BrowserCaptchaService

            service = await BrowserCaptchaService.get_instance(container.db)
            progress_runtime_prepare(runtime_method, "Reloading Personal browser pool")
            await service.reload_config()
        finish_runtime_prepare(runtime_method, f"{runtime_method.title()} captcha runtime is ready")
    except Exception as exc:
        fail_runtime_prepare(
            runtime_method,
            f"Runtime preparation failed: {type(exc).__name__}: {str(exc)[:200]}",
        )
def _schedule_captcha_runtime_prepare(method: str, container: AppContainer) -> bool:
    runtime_method = _normalize_runtime_method(method)
    task_name = f"captcha-runtime-{runtime_method}"
    if container.tasks.is_running(task_name):
        progress_runtime_prepare(runtime_method, "Runtime preparation is still in progress")
        return False
    container.tasks.start(task_name, _prepare_captcha_runtime(runtime_method, container))
    return True


# ========== Request Models ==========


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = True


class GoogleDriveBackupConfigRequest(BaseModel):
    enabled: bool = False
    schedule_time: str = "03:00"
    timezone: str = "Asia/Karachi"
    retention: int = Field(default=14, ge=1, le=365)


class GoogleDriveRestoreRequest(BaseModel):
    confirmation: str


class MaintenanceRequest(BaseModel):
    active: bool
    reason: str = Field(default="operator_requested", max_length=300)


class AddTokenRequest(BaseModel):
    st: str
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1
    protocol_mode: str = "session"
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    auto_refresh_enabled: bool = True
    refresh_interval_minutes: int = Field(default=120, ge=1, le=10080)


class UpdateTokenRequest(BaseModel):
    st: Optional[str] = None  # Session Token; browser-profile accounts may leave this empty
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    use_extension_for_generation: Optional[bool] = None
    image_enabled: Optional[bool] = None
    video_enabled: Optional[bool] = None
    image_concurrency: Optional[int] = None
    video_concurrency: Optional[int] = None
    protocol_mode: Optional[str] = None
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    auto_refresh_enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = Field(default=None, ge=1, le=10080)


class BrowserProfileTokenRequest(BaseModel):
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1


class ProxyConfigRequest(BaseModel):
    proxy_enabled: bool
    proxy_url: Optional[str] = None
    media_proxy_enabled: Optional[bool] = None
    media_proxy_url: Optional[str] = None


class ProxyTestRequest(BaseModel):
    proxy_url: str
    test_url: Optional[str] = "https://labs.google/"
    timeout_seconds: Optional[int] = 15


class CaptchaScoreTestRequest(BaseModel):
    website_url: Optional[str] = "https://antcpt.com/score_detector/"
    website_key: Optional[str] = "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf"
    action: Optional[str] = "homepage"
    verify_url: Optional[str] = "https://antcpt.com/score_detector/verify.php"
    enterprise: Optional[bool] = False


class GenerationConfigRequest(BaseModel):
    image_timeout: Optional[int] = None
    video_timeout: Optional[int] = None
    max_retries: Optional[int] = None
    extension_generation_enabled: Optional[bool] = None
    extension_generation_fallback_mode: Optional[str] = None
    sub2gen_gemini_api_keys: Optional[str] = None
    sub2gen_openai_api_keys: Optional[str] = None
    sub2gen_openrouter_api_keys: Optional[str] = None
    sub2gen_third_party_gemini_api_keys: Optional[str] = None
    sub2gen_third_party_gemini_base_url: Optional[str] = None
    cloudflare_account_id: Optional[str] = None
    cloudflare_api_token: Optional[str] = None
    sub2gen_csvgen_cookie: Optional[str] = None
    sub2gen_csvgen_api_keys: Optional[str] = None
    sub2gen_cloning_model: Optional[str] = None
    sub2gen_metadata_backend: Optional[str] = None
    sub2gen_metadata_provider_order: Optional[str] = None
    sub2gen_metadata_enabled_providers: Optional[str] = None
    sub2gen_metadata_provider_retry_count: Optional[int] = None
    sub2gen_metadata_model: Optional[str] = None
    sub2gen_metadata_enabled_models: Optional[str] = None
    sub2gen_metadata_primary_model: Optional[str] = None
    sub2gen_metadata_fallback_models: Optional[str] = None
    metadata_system_prompt: Optional[str] = None
    sub2gen_cloning_backend: Optional[str] = None
    sub2gen_cloning_provider_order: Optional[str] = None
    sub2gen_cloning_enabled_providers: Optional[str] = None
    sub2gen_cloning_provider_retry_count: Optional[int] = None
    sub2gen_cloning_gemini_api_keys: Optional[str] = None
    sub2gen_cloning_openai_api_keys: Optional[str] = None
    sub2gen_cloning_openrouter_api_keys: Optional[str] = None
    sub2gen_cloning_third_party_gemini_api_keys: Optional[str] = None
    sub2gen_cloning_third_party_gemini_base_url: Optional[str] = None
    sub2gen_cloning_cloudflare_account_id: Optional[str] = None
    sub2gen_cloning_cloudflare_api_token: Optional[str] = None
    cloning_image_system_prompt: Optional[str] = None
    cloning_video_system_prompt: Optional[str] = None
    task_tracker_device_id: Optional[str] = None
    task_tracker_device_name: Optional[str] = None
    task_tracker_cookies: Optional[str] = None
    task_tracker_device_token: Optional[str] = None
    task_tracker_turnstile_token: Optional[str] = None
    task_tracker_tls_profile: Optional[str] = None


class CallLogicConfigRequest(BaseModel):
    call_mode: str


class ChangePasswordRequest(BaseModel):
    username: Optional[str] = None
    old_password: str
    new_password: str


class UpdateAPIKeyRequest(BaseModel):
    new_api_key: str


class CreateManagedApiKeyRequest(BaseModel):
    client_name: str
    label: str = "default"
    scopes: str
    account_ids: List[int]
    endpoint_limits: Dict[str, Dict[str, int]] = {}
    expires_at: Optional[str] = None


class UpdateManagedApiKeyRequest(BaseModel):
    client_name: Optional[str] = None
    label: Optional[str] = None
    is_active: Optional[bool] = None
    scopes: Optional[str] = None
    expires_at: Optional[str] = None
    account_ids: Optional[List[int]] = None
    endpoint_limits: Optional[Dict[str, Dict[str, int]]] = None


class ExtensionWorkerBindRequest(BaseModel):
    route_key: str
    api_key_id: int


class ExtensionWorkerUnbindRequest(BaseModel):
    route_key: str


class ExtensionWorkerKillRequest(BaseModel):
    worker_session_id: str


class CaptchaWorkerKeyCreateRequest(BaseModel):
    label: str = ""


class CaptchaWorkerKeyUpdateRequest(BaseModel):
    label: Optional[str] = None
    is_active: Optional[bool] = None


class UpdateDebugConfigRequest(BaseModel):
    enabled: bool


class UpdateAdminConfigRequest(BaseModel):
    error_ban_threshold: Optional[int] = None
    error_ban_enabled: Optional[bool] = None


class RunwayConfigUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    base_url: Optional[str] = None
    poll_interval_sec: Optional[float] = None
    timeout_sec: Optional[float] = None
    cache_outputs: Optional[bool] = None


class RunwayAccountRequest(BaseModel):
    label: str = ""
    raw_credential: str = ""
    is_active: bool = True
    workspace_id: Optional[str] = None
    team_id: Optional[str] = None
    concurrency_limit: int = 1


class RunwayAccountUpdateRequest(BaseModel):
    label: Optional[str] = None
    raw_credential: Optional[str] = None
    is_active: Optional[bool] = None
    workspace_id: Optional[str] = None
    team_id: Optional[str] = None
    concurrency_limit: Optional[int] = None


class RunwayTeamsRequest(BaseModel):
    raw_credential: str = ""


class RunwayModelRequest(BaseModel):
    public_model_id: str
    display_name: str = ""
    kind: str = "image"
    task_type: str
    builder_key: str = ""
    default_options: str = "{}"
    request_mapping: str = "{}"
    capability_schema: str = "{}"
    media_roles: str = "[]"
    supported_modes: str = "[]"
    limits: str = "{}"
    feature_flags: str = "[]"
    cost_feature: str = ""
    source_version: str = ""
    live_available: bool = True
    disabled_reason: str = ""
    is_enabled: bool = True


class RunwayModelUpdateRequest(BaseModel):
    public_model_id: Optional[str] = None
    display_name: Optional[str] = None
    kind: Optional[str] = None
    task_type: Optional[str] = None
    builder_key: Optional[str] = None
    default_options: Optional[str] = None
    request_mapping: Optional[str] = None
    capability_schema: Optional[str] = None
    media_roles: Optional[str] = None
    supported_modes: Optional[str] = None
    limits: Optional[str] = None
    feature_flags: Optional[str] = None
    cost_feature: Optional[str] = None
    source_version: Optional[str] = None
    live_available: Optional[bool] = None
    disabled_reason: Optional[str] = None
    is_enabled: Optional[bool] = None


class GeminiGenConfigUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    video_enabled: Optional[bool] = None
    base_url: Optional[str] = None
    poll_interval_image_sec: Optional[float] = None
    poll_interval_video_sec: Optional[float] = None
    timeout_image_sec: Optional[float] = None
    timeout_video_sec: Optional[float] = None
    global_image_concurrency: Optional[int] = None
    global_video_concurrency: Optional[int] = None
    cache_outputs: Optional[bool] = None


class GeminiGenAccountRequest(BaseModel):
    label: str = ""
    raw_cookie: str = ""
    bearer_token: str = ""
    refresh_token: str = ""
    guard_id: str = ""
    turnstile_token: str = ""
    is_active: bool = True
    image_concurrency: int = 5
    video_concurrency: int = 5


class GeminiGenAccountUpdateRequest(BaseModel):
    label: Optional[str] = None
    raw_cookie: Optional[str] = None
    bearer_token: Optional[str] = None
    refresh_token: Optional[str] = None
    guard_id: Optional[str] = None
    turnstile_token: Optional[str] = None
    is_active: Optional[bool] = None
    image_concurrency: Optional[int] = None
    video_concurrency: Optional[int] = None


class ST2ATRequest(BaseModel):
    """ST转AT请求"""

    st: str


class TokenRefreshConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = Field(default=None, ge=1, le=10080)


class ImportTokenItem(BaseModel):
    """导入Token项"""

    email: Optional[str] = None
    access_token: Optional[str] = None
    session_token: Optional[str] = None
    is_active: bool = True
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1
    protocol_mode: str = "session"
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    auto_refresh_enabled: bool = True
    refresh_interval_minutes: int = Field(default=120, ge=1, le=10080)


class ImportTokensRequest(BaseModel):
    """导入Token请求"""

    tokens: List[ImportTokenItem]


class EventCalendarItemRequest(BaseModel):
    date: str
    month: str
    event_name: str


class EventCalendarUpdateRequest(BaseModel):
    events: List[EventCalendarItemRequest]


# ========== Auth Middleware ==========


def get_admin_token_from_cookie(request: Request) -> Optional[str]:
    token = str(request.cookies.get(ADMIN_SESSION_COOKIE_NAME) or "").strip()
    return token or None


def _request_uses_https(request: Request) -> bool:
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0]
    return request.url.scheme.lower() == "https" or forwarded_proto.strip().lower() == "https"


def _set_admin_session_cookie(
    response: Response,
    request: Request,
    token: str,
    *,
    remember_me: bool,
) -> None:
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        value=token,
        max_age=_ADMIN_SESSION_TTL_REMEMBER if remember_me else None,
        httponly=True,
        samesite="lax",
        secure=_request_uses_https(request),
        path="/",
    )


def _clear_admin_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=_request_uses_https(request),
        path="/",
    )


async def is_admin_session_token_valid(token: Optional[str], database: Database) -> bool:
    normalized = str(token or "").strip()
    return bool(normalized and await database.is_admin_session_valid(normalized))


async def verify_admin_token(
    request: Request,
    authorization: str = Header(None),
    container: AppContainer = Depends(get_container),
):
    """Verify a persistent admin session from Bearer auth or the browser cookie."""
    header_token = ""
    if authorization and authorization.startswith("Bearer "):
        header_token = authorization[7:].strip()
    cookie_token = get_admin_token_from_cookie(request) or ""

    if header_token and await is_admin_session_token_valid(header_token, container.db):
        return header_token
    if cookie_token and await is_admin_session_token_valid(cookie_token, container.db):
        return cookie_token
    if header_token or cookie_token:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")
    raise HTTPException(status_code=401, detail="Missing authorization")


# ========== Auth Endpoints ==========


@router.post("/api/admin/login")
async def admin_login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    """Admin login - returns session token (NOT API key)"""
    if not AuthManager.verify_admin(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Generate independent session token
    session_token = f"admin-{secrets.token_urlsafe(32)}"

    ttl = _ADMIN_SESSION_TTL_REMEMBER if payload.remember_me else _ADMIN_SESSION_TTL_BROWSER
    expires_at = int(time.time()) + ttl
    await container.db.insert_admin_session(session_token, expires_at)
    _set_admin_session_cookie(
        response,
        request,
        session_token,
        remember_me=payload.remember_me,
    )

    return {
        "success": True,
        "token": session_token,  # Session token (NOT API key)
        "username": config.admin_username,
    }


@router.post("/api/admin/logout")
async def admin_logout(
    request: Request,
    response: Response,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Admin logout - invalidate session token"""
    cookie_token = get_admin_token_from_cookie(request)
    for session_token in {token, cookie_token}:
        if session_token:
            await container.db.delete_admin_session(session_token)
    _clear_admin_session_cookie(response, request)
    return {"success": True, "message": "退出登录成功"}


@router.post("/api/admin/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Change admin password"""
    # Verify old password
    if not AuthManager.verify_admin(config.admin_username, payload.old_password):
        raise HTTPException(status_code=400, detail="旧密码错误")

    # Update password and username in database
    update_params = {"password": payload.new_password}
    if payload.username:
        update_params["username"] = payload.username

    await container.db.update_admin_config(**update_params)

    # 🔥 Hot reload: sync database config to memory
    await container.db.reload_config_to_memory()

    # 🔑 Invalidate all admin session tokens (force re-login for security)
    await container.db.delete_all_admin_sessions()
    _clear_admin_session_cookie(response, request)

    return {"success": True, "message": "密码修改成功,请重新登录"}


# ========== Token Management ==========


@router.get("/api/tokens")
async def get_tokens(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get all tokens with statistics"""
    token_rows = await container.db.get_all_tokens_with_stats()
    profile_service = await BrowserProfileService.get_instance(
        db=container.db, flow_client=container.token_manager.flow_client
    )
    runtime_states = {
        int(row["id"]): await profile_service.is_runtime_open(int(row["id"]))
        for row in token_rows
        if row.get("id") is not None and (row.get("auth_mode") or "session_token") == "browser_profile"
    }
    to_iso = lambda value: value.isoformat() if hasattr(value, "isoformat") else value
    now = datetime.now(timezone.utc)

    def normalize_dt(value):
        if not value:
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return None
        if getattr(value, "tzinfo", None) is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    return [
        {
            "id": row.get("id"),
            "st": row.get("st"),  # Session Token for editing
            "at": row.get("at"),  # Access Token for editing (从ST转换而来)
            "at_expires": to_iso(row.get("at_expires")) if row.get("at_expires") else None,  # 🆕 AT过期时间
            "at_expired": bool(normalize_dt(row.get("at_expires")) and normalize_dt(row.get("at_expires")) <= now),
            "at_expiring_within_1h": bool(
                normalize_dt(row.get("at_expires"))
                and normalize_dt(row.get("at_expires")) > now
                and (normalize_dt(row.get("at_expires")) - now).total_seconds() < 3600
            ),
            "token": row.get("at"),  # 兼容前端 token.token 的访问方式
            "email": row.get("email"),
            "name": row.get("name"),
            "remark": row.get("remark"),
            "is_active": bool(row.get("is_active")),
            "created_at": to_iso(row.get("created_at")) if row.get("created_at") else None,
            "last_used_at": to_iso(row.get("last_used_at")) if row.get("last_used_at") else None,
            "use_count": row.get("use_count"),
            "credits": row.get("credits"),  # 🆕 余额
            "user_paygate_tier": row.get("user_paygate_tier"),
            "current_project_id": row.get("current_project_id"),  # 🆕 项目ID
            "current_project_name": row.get("current_project_name"),  # 🆕 项目名称
            "auth_mode": row.get("auth_mode") or "session_token",
            "browser_profile_path": row.get("browser_profile_path"),
            "browser_profile_status": row.get("browser_profile_status") or "not_created",
            "browser_profile_email": row.get("browser_profile_email"),
            "browser_profile_name": row.get("browser_profile_name"),
            "browser_profile_login_state": row.get("browser_profile_login_state") or "unknown",
            "browser_profile_cookie_status": row.get("browser_profile_cookie_status") or "unknown",
            "browser_profile_st_status": row.get("browser_profile_st_status") or "unknown",
            "browser_profile_at_status": row.get("browser_profile_at_status") or "unknown",
            "browser_profile_last_opened_at": to_iso(row.get("browser_profile_last_opened_at"))
            if row.get("browser_profile_last_opened_at")
            else None,
            "browser_profile_last_sync_at": to_iso(row.get("browser_profile_last_sync_at"))
            if row.get("browser_profile_last_sync_at")
            else None,
            "browser_profile_last_refresh_at": to_iso(row.get("browser_profile_last_refresh_at"))
            if row.get("browser_profile_last_refresh_at")
            else None,
            "browser_profile_last_error": row.get("browser_profile_last_error"),
            "runtime_open": runtime_states.get(int(row.get("id") or 0), False),
            "captcha_proxy_url": row.get("captcha_proxy_url") or "",
            "extension_route_key": row.get("extension_route_key") or "",
            "protocol_mode": row.get("protocol_mode") or "session",
            "google_cookies": row.get("google_cookies") or "",
            "has_google_cookies": bool(str(row.get("google_cookies") or "").strip()),
            "login_account": row.get("login_account") or "",
            "login_password": row.get("login_password") or "",
            "proxy_url": row.get("proxy_url") or "",
            "protocol_proxy_configured": bool(str(row.get("proxy_url") or "").strip()),
            "auto_refresh_enabled": bool(row.get("auto_refresh_enabled", True)),
            "refresh_interval_minutes": int(row.get("refresh_interval_minutes") or 120),
            "last_st_refresh_at": to_iso(row.get("last_st_refresh_at")) if row.get("last_st_refresh_at") else None,
            "last_st_refresh_result": row.get("last_st_refresh_result") or "",
            "image_enabled": bool(row.get("image_enabled")),
            "video_enabled": bool(row.get("video_enabled")),
            "image_concurrency": row.get("image_concurrency"),
            "video_concurrency": row.get("video_concurrency"),
            "image_count": row.get("image_count", 0),
            "video_count": row.get("video_count", 0),
            "error_count": row.get("error_count", 0),
            "today_error_count": row.get("today_error_count", 0),
            "consecutive_error_count": row.get("consecutive_error_count", 0),
            "last_error_at": to_iso(row.get("last_error_at")) if row.get("last_error_at") else None,
            "ban_reason": row.get("ban_reason"),
            "banned_at": to_iso(row.get("banned_at")) if row.get("banned_at") else None,
        }
        for row in token_rows
    ]  # 直接返回数组,兼容前端


@router.post("/api/tokens")
async def add_token(
    request: AddTokenRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Add a new token"""
    try:
        add_kwargs: Dict[str, Any] = {
            "st": request.st,
            "project_id": None,
            "project_name": None,
            "remark": request.remark,
            "captcha_proxy_url": request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            "image_enabled": request.image_enabled,
            "video_enabled": request.video_enabled,
            "image_concurrency": request.image_concurrency,
            "video_concurrency": request.video_concurrency,
            "protocol_mode": request.protocol_mode,
            "google_cookies": request.google_cookies,
            "login_account": request.login_account,
            "login_password": request.login_password,
            "proxy_url": request.proxy_url,
            "auto_refresh_enabled": request.auto_refresh_enabled,
            "refresh_interval_minutes": request.refresh_interval_minutes,
        }
        if _supports_kwarg(container.token_manager.add_token, "extension_route_key"):
            add_kwargs["extension_route_key"] = (
                request.extension_route_key.strip() if request.extension_route_key is not None else None
            )
        new_token = await container.token_manager.add_token(**add_kwargs)

        # 热更新并发限制，避免必须重启服务
        if container.concurrency_manager:
            await container.concurrency_manager.reset_token(
                new_token.id,
                image_concurrency=new_token.image_concurrency,
                video_concurrency=new_token.video_concurrency,
            )

        return {
            "success": True,
            "message": "Token添加成功",
            "token": {
                "id": new_token.id,
                "email": new_token.email,
                "credits": new_token.credits,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"添加Token失败: {str(e)}")


@router.post("/api/tokens/browser-profile")
async def add_browser_profile_token(
    request: BrowserProfileTokenRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Create a headed browser-profile account placeholder without a real ST yet."""
    try:
        service = await BrowserProfileService.get_instance(
            db=container.db, flow_client=container.token_manager.flow_client
        )
        account = Token(
            st=service.build_placeholder_st(),
            at=None,
            at_expires=None,
            email=service.build_placeholder_email(),
            name="Browser profile",
            remark=request.remark,
            is_active=False,
            credits=0,
            current_project_id=None,
            current_project_name=None,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency,
            captcha_proxy_url=request.captcha_proxy_url.strip() if request.captcha_proxy_url else None,
            auth_mode="browser_profile",
            browser_profile_status="not_created",
            browser_profile_login_state="unknown",
            browser_profile_cookie_status="unknown",
            browser_profile_st_status="missing",
            browser_profile_at_status="missing",
        )
        token_id = await container.db.add_token(account)
        await container.db.update_token(
            token_id,
            email=service.build_placeholder_email(token_id),
            browser_profile_path=str(service.profile_path_for_token(token_id)),
        )
        if container.concurrency_manager:
            await container.concurrency_manager.reset_token(
                token_id,
                image_concurrency=request.image_concurrency,
                video_concurrency=request.video_concurrency,
            )
        return {
            "success": True,
            "message": "Browser profile account created",
            "token": await service.status(token_id),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Create browser profile account failed: {str(e)}")


@router.get("/api/tokens/{token_id}/browser-profile/status")
async def browser_profile_status(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        service = await BrowserProfileService.get_instance(
            db=container.db, flow_client=container.token_manager.flow_client
        )
        return {"success": True, "profile": await service.status(token_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tokens/{token_id}/browser-profile/open")
async def open_browser_profile(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        service = await BrowserProfileService.get_instance(
            db=container.db, flow_client=container.token_manager.flow_client
        )
        return {"success": True, "profile": await service.open_profile(token_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tokens/{token_id}/browser-profile/close")
async def close_browser_profile(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        service = await BrowserProfileService.get_instance(
            db=container.db, flow_client=container.token_manager.flow_client
        )
        return {"success": True, "profile": await service.close_profile(token_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tokens/{token_id}/browser-profile/sync")
async def sync_browser_profile(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        service = await BrowserProfileService.get_instance(
            db=container.db, flow_client=container.token_manager.flow_client
        )
        profile = await service.sync_profile(token_id, retain_runtime=False)
        if profile.get("profile_status") == "connected" and profile.get("st_status") == "ok":
            await container.token_manager.enable_token(token_id)
        return {"success": True, "profile": profile}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tokens/{token_id}/browser-profile/refresh")
async def refresh_browser_profile(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        service = await BrowserProfileService.get_instance(
            db=container.db, flow_client=container.token_manager.flow_client
        )
        profile = await service.refresh_profile(token_id, retain_runtime=False)
        if profile.get("profile_status") == "connected" and profile.get("st_status") == "ok":
            await container.token_manager.enable_token(token_id)
        return {"success": True, "profile": profile}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tokens/{token_id}/browser-profile/reset")
async def reset_browser_profile(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        service = await BrowserProfileService.get_instance(
            db=container.db, flow_client=container.token_manager.flow_client
        )
        return {"success": True, "profile": await service.reset_profile(token_id)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/tokens/{token_id}")
async def update_token_profile_aware(
    token_id: int,
    request: UpdateTokenRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update token/account metadata; ST is optional for browser-profile accounts."""
    try:
        existing = await container.token_manager.get_token(token_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Token not found")

        from datetime import datetime

        update_kwargs: Dict[str, Any] = {
            "token_id": token_id,
            "remark": request.remark,
            "captcha_proxy_url": request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            "image_enabled": request.image_enabled,
            "video_enabled": request.video_enabled,
            "image_concurrency": request.image_concurrency,
            "video_concurrency": request.video_concurrency,
            "protocol_mode": request.protocol_mode,
            "google_cookies": request.google_cookies,
            "login_account": request.login_account,
            "login_password": request.login_password,
            "proxy_url": request.proxy_url,
            "auto_refresh_enabled": request.auto_refresh_enabled,
            "refresh_interval_minutes": request.refresh_interval_minutes,
        }

        st_value = (request.st or "").strip()
        if st_value:
            result = await container.token_manager.flow_client.st_to_at(st_value)
            expires = result.get("expires")
            at_expires = None
            if expires:
                try:
                    at_expires = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                except Exception:
                    at_expires = None
            update_kwargs.update(
                {
                    "st": st_value,
                    "at": result["access_token"],
                    "at_expires": at_expires,
                }
            )

        if _supports_kwarg(container.token_manager.update_token, "extension_route_key"):
            update_kwargs["extension_route_key"] = (
                request.extension_route_key.strip() if request.extension_route_key is not None else None
            )
        if _supports_kwarg(container.token_manager.update_token, "use_extension_for_generation"):
            if request.use_extension_for_generation is not None:
                update_kwargs["use_extension_for_generation"] = bool(request.use_extension_for_generation)

        await container.token_manager.update_token(**update_kwargs)

        if container.concurrency_manager:
            updated_token = await container.token_manager.get_token(token_id)
            if updated_token:
                await container.concurrency_manager.reset_token(
                    token_id,
                    image_concurrency=updated_token.image_concurrency,
                    video_concurrency=updated_token.video_concurrency,
                )

        return {"success": True, "message": "Token updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/tokens/{token_id}/legacy-update-disabled")
async def update_token(
    token_id: int,
    request: UpdateTokenRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update token - 使用ST自动刷新AT"""
    try:
        # 先ST转AT
        result = await container.token_manager.flow_client.st_to_at(request.st)
        at = result["access_token"]
        expires = result.get("expires")

        # 解析过期时间
        from datetime import datetime

        at_expires = None
        if expires:
            try:
                at_expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except:
                pass

        # 更新token (包含AT、ST、AT过期时间、project_id和project_name)
        update_kwargs: Dict[str, Any] = {
            "token_id": token_id,
            "st": request.st,
            "at": at,
            "at_expires": at_expires,  # 🆕 更新AT过期时间
            "project_id": request.project_id,
            "project_name": request.project_name,
            "remark": request.remark,
            "captcha_proxy_url": request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            "image_enabled": request.image_enabled,
            "video_enabled": request.video_enabled,
            "image_concurrency": request.image_concurrency,
            "video_concurrency": request.video_concurrency,
            "protocol_mode": request.protocol_mode,
            "google_cookies": request.google_cookies,
            "login_account": request.login_account,
            "login_password": request.login_password,
            "proxy_url": request.proxy_url,
            "auto_refresh_enabled": request.auto_refresh_enabled,
            "refresh_interval_minutes": request.refresh_interval_minutes,
        }
        if _supports_kwarg(container.token_manager.update_token, "extension_route_key"):
            update_kwargs["extension_route_key"] = (
                request.extension_route_key.strip() if request.extension_route_key is not None else None
            )
        if _supports_kwarg(container.token_manager.update_token, "use_extension_for_generation"):
            if request.use_extension_for_generation is not None:
                update_kwargs["use_extension_for_generation"] = bool(request.use_extension_for_generation)
        await container.token_manager.update_token(**update_kwargs)

        # 热更新并发限制，确保管理台修改立即生效
        if container.concurrency_manager:
            updated_token = await container.token_manager.get_token(token_id)
            if updated_token:
                await container.concurrency_manager.reset_token(
                    token_id,
                    image_concurrency=updated_token.image_concurrency,
                    video_concurrency=updated_token.video_concurrency,
                )

        return {"success": True, "message": "Token更新成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/tokens/{token_id}")
async def delete_token(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Delete token"""
    try:
        await container.token_manager.delete_token(token_id)
        if container.concurrency_manager:
            await container.concurrency_manager.remove_token(token_id)
        return {"success": True, "message": "Token删除成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tokens/{token_id}/enable")
async def enable_token(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Enable token"""
    await container.token_manager.enable_token(token_id)
    return {"success": True, "message": "Token已启用"}


@router.post("/api/tokens/{token_id}/disable")
async def disable_token(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Disable token"""
    await container.token_manager.disable_token(token_id)
    return {"success": True, "message": "Token已禁用"}


@router.post("/api/tokens/{token_id}/refresh-credits")
async def refresh_credits(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """刷新Token余额 🆕"""
    try:
        credits = await container.token_manager.refresh_credits(token_id)
        return {"success": True, "message": "余额刷新成功", "credits": credits}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新余额失败: {str(e)}")


@router.post("/api/tokens/{token_id}/refresh-at")
async def refresh_at(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """手动刷新Token的AT (使用ST转换) 🆕

    如果 AT 刷新失败且处于 personal 模式，会自动尝试通过浏览器刷新 ST
    """
    from ..core.logger import debug_logger
    from ..core.config import config
    from ..services.st_refresh_reasons import describe_st_refresh_reason

    debug_logger.log_info(f"[API] 手动刷新 AT 请求: token_id={token_id}, captcha_method={config.captcha_method}")

    try:
        # 调用token_manager的内部刷新方法（包含 ST 自动刷新逻辑）
        success = await container.token_manager._refresh_at(token_id)
        st_refresh_reason = container.token_manager.consume_st_refresh_reason(token_id)
        captcha_mode = str(config.captcha_method or "").strip()
        supports_st_refresh = captcha_mode in {"personal", "browser", "extension"} or bool(
            getattr(config, "dedicated_extension_enabled", False)
        )

        if success:
            # 获取更新后的token信息
            updated_token = await container.token_manager.get_token(token_id)

            message = "AT刷新成功"
            if supports_st_refresh:
                message += "（支持ST自动刷新）"

            debug_logger.log_info(f"[API] AT 刷新成功: token_id={token_id}")

            return {
                "success": True,
                "message": message,
                "token": {
                    "id": updated_token.id,
                    "email": updated_token.email,
                    "at_expires": updated_token.at_expires.isoformat() if updated_token.at_expires else None,
                },
            }
        else:
            debug_logger.log_error(f"[API] AT 刷新失败: token_id={token_id}")

            error_detail = "AT刷新失败"
            if supports_st_refresh:
                error_detail += f"（当前打码模式: {captcha_mode or '-'}，已尝试 ST 自动刷新后重试 AT）"
                reason_hint = describe_st_refresh_reason(st_refresh_reason)
                if reason_hint:
                    error_detail += f"；原因: {reason_hint}"
            else:
                error_detail += f"（当前打码模式: {captcha_mode or '-'}，当前模式未启用 ST 自动刷新能力）"
            if captcha_mode == "browser":
                error_detail += (
                    f"，gateway fallback={'on' if bool(config.browser_fallback_to_remote_browser) else 'off'}"
                )

            raise HTTPException(status_code=500, detail=error_detail)
    except HTTPException:
        raise
    except Exception as e:
        debug_logger.log_error(f"[API] 刷新AT异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"刷新AT失败: {str(e)}")


@router.post("/api/tokens/{token_id}/refresh-profile")
async def refresh_token_profile(
    token_id: int,
    request: ST2ATRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Refresh token email/name using ST -> AT user profile payload."""
    try:
        token_obj = await container.token_manager.get_token(token_id)
        if not token_obj:
            raise HTTPException(status_code=404, detail="Token not found")

        st_value = (request.st or "").strip()
        if not st_value:
            raise HTTPException(status_code=400, detail="st is required")

        result = await container.token_manager.flow_client.st_to_at(st_value)
        user_info = result.get("user", {}) if isinstance(result, dict) else {}
        email = str(user_info.get("email", "") or "").strip()
        name = str(user_info.get("name", "") or "").strip()
        if not email:
            raise HTTPException(status_code=400, detail="Failed to resolve email from session token")
        if not name:
            name = email.split("@")[0] if "@" in email else email

        await container.db.update_token(token_id, email=email, name=name)

        return {
            "success": True,
            "message": "Token profile refreshed",
            "token": {
                "id": token_id,
                "email": email,
                "name": name,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新邮箱失败: {str(e)}")


def _project_to_dict(project):
    to_iso = lambda value: value.isoformat() if hasattr(value, "isoformat") and value else None
    return {
        "id": project.id,
        "project_id": project.project_id,
        "project_name": project.project_name,
        "token_id": project.token_id,
        "is_active": bool(project.is_active),
        "created_at": to_iso(project.created_at),
    }


@router.get("/api/tokens/{token_id}/projects")
async def list_token_projects(
    token_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """List VideoFX projects stored for a token."""
    raise HTTPException(status_code=410, detail="Project management has been removed")
    t = await container.token_manager.get_token(token_id)
    if not t:
        raise HTTPException(status_code=404, detail="Token not found")
    projects = await container.token_manager.db.get_projects_by_token(token_id)
    return {"success": True, "projects": [_project_to_dict(p) for p in projects]}


@router.post("/api/tokens/{token_id}/projects")
async def create_token_project(
    token_id: int,
    request: dict,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Create a new VideoFX project for an existing token."""
    raise HTTPException(status_code=410, detail="Project management has been removed")
    t = await container.token_manager.get_token(token_id)
    if not t:
        raise HTTPException(status_code=404, detail="Token not found")

    title = request.get("title")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=400, detail="title must be a string")
    if isinstance(title, str):
        title = title.strip() or None
    set_as_current = request.get("set_as_current", True)
    if not isinstance(set_as_current, bool):
        raise HTTPException(status_code=400, detail="set_as_current must be a boolean")

    try:
        project = await container.token_manager.create_project_for_token(
            token_id,
            title=title,
            set_as_current=set_as_current,
        )
        updated = await container.token_manager.get_token(token_id) if set_as_current else None
        return {
            "success": True,
            "message": "Project created",
            "project": _project_to_dict(project),
            "token": (
                {
                    "id": updated.id,
                    "current_project_id": updated.current_project_id,
                    "current_project_name": updated.current_project_name,
                }
                if updated
                else None
            ),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Create project failed: {str(e)}")


@router.post("/api/tokens/st2at")
async def st_to_at(
    request: ST2ATRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Convert Session Token to Access Token (仅转换,不添加到数据库)"""
    try:
        result = await container.token_manager.flow_client.st_to_at(request.st)
        return {
            "success": True,
            "message": "ST converted to AT successfully",
            "access_token": result["access_token"],
            "email": result.get("user", {}).get("email"),
            "expires": result.get("expires"),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/tokens/import")
async def import_tokens(
    request: ImportTokensRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """批量导入Token"""
    from datetime import datetime, timezone

    added = 0
    updated = 0
    errors = []
    # 保持与历史逻辑一致：按 created_at DESC 的结果中，优先命中同邮箱“最新一条”
    existing_by_email = {}
    for existing_token in await container.token_manager.get_all_tokens():
        if existing_token.email and existing_token.email not in existing_by_email:
            existing_by_email[existing_token.email] = existing_token

    for idx, item in enumerate(request.tokens):
        try:
            st = item.session_token

            if not st:
                errors.append(f"第{idx + 1}项: 缺少 session_token")
                continue

            # 使用 ST 转 AT 获取用户信息
            try:
                result = await container.token_manager.flow_client.st_to_at(st)
                at = result["access_token"]
                email = result.get("user", {}).get("email")
                expires = result.get("expires")

                if not email:
                    errors.append(f"第{idx + 1}项: 无法获取邮箱信息")
                    continue

                # 解析过期时间
                at_expires = None
                is_expired = False
                if expires:
                    try:
                        at_expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                        # 判断是否过期
                        now = datetime.now(timezone.utc)
                        is_expired = at_expires <= now
                    except:
                        pass

                # 使用邮箱检查是否已存在
                existing = existing_by_email.get(email)

                if existing:
                    # 更新现有Token
                    import_update_kwargs: Dict[str, Any] = {
                        "token_id": existing.id,
                        "st": st,
                        "at": at,
                        "at_expires": at_expires,
                        "captcha_proxy_url": item.captcha_proxy_url.strip()
                        if item.captcha_proxy_url is not None
                        else None,
                        "image_enabled": item.image_enabled,
                        "video_enabled": item.video_enabled,
                        "image_concurrency": item.image_concurrency,
                        "video_concurrency": item.video_concurrency,
                        "protocol_mode": item.protocol_mode,
                        "google_cookies": item.google_cookies,
                        "login_account": item.login_account,
                        "login_password": item.login_password,
                        "proxy_url": item.proxy_url,
                        "auto_refresh_enabled": item.auto_refresh_enabled,
                        "refresh_interval_minutes": item.refresh_interval_minutes,
                    }
                    if _supports_kwarg(container.token_manager.update_token, "extension_route_key"):
                        import_update_kwargs["extension_route_key"] = (
                            item.extension_route_key.strip() if item.extension_route_key is not None else None
                        )
                    await container.token_manager.update_token(**import_update_kwargs)
                    # 如果过期则禁用
                    if is_expired:
                        await container.token_manager.disable_token(existing.id)
                        existing.is_active = False
                    existing.st = st
                    existing.at = at
                    existing.at_expires = at_expires
                    existing.captcha_proxy_url = item.captcha_proxy_url
                    if hasattr(existing, "extension_route_key"):
                        existing.extension_route_key = item.extension_route_key
                    existing.image_enabled = item.image_enabled
                    existing.video_enabled = item.video_enabled
                    existing.image_concurrency = item.image_concurrency
                    existing.video_concurrency = item.video_concurrency
                    existing.protocol_mode = item.protocol_mode
                    existing.google_cookies = item.google_cookies or ""
                    existing.login_account = item.login_account or ""
                    existing.login_password = item.login_password or ""
                    existing.proxy_url = item.proxy_url or ""
                    existing.auto_refresh_enabled = item.auto_refresh_enabled
                    existing.refresh_interval_minutes = item.refresh_interval_minutes
                    updated += 1
                else:
                    # 添加新Token
                    import_add_kwargs: Dict[str, Any] = {
                        "st": st,
                        "captcha_proxy_url": item.captcha_proxy_url.strip()
                        if item.captcha_proxy_url is not None
                        else None,
                        "image_enabled": item.image_enabled,
                        "video_enabled": item.video_enabled,
                        "image_concurrency": item.image_concurrency,
                        "video_concurrency": item.video_concurrency,
                        "protocol_mode": item.protocol_mode,
                        "google_cookies": item.google_cookies,
                        "login_account": item.login_account,
                        "login_password": item.login_password,
                        "proxy_url": item.proxy_url,
                        "auto_refresh_enabled": item.auto_refresh_enabled,
                        "refresh_interval_minutes": item.refresh_interval_minutes,
                    }
                    if _supports_kwarg(container.token_manager.add_token, "extension_route_key"):
                        import_add_kwargs["extension_route_key"] = (
                            item.extension_route_key.strip() if item.extension_route_key is not None else None
                        )
                    new_token = await container.token_manager.add_token(**import_add_kwargs)
                    # 如果过期则禁用
                    if is_expired:
                        await container.token_manager.disable_token(new_token.id)
                        new_token.is_active = False
                    existing_by_email[email] = new_token
                    added += 1

            except Exception as e:
                errors.append(f"第{idx + 1}项: {str(e)}")

        except Exception as e:
            errors.append(f"第{idx + 1}项: {str(e)}")

    return {
        "success": True,
        "added": added,
        "updated": updated,
        "errors": errors if errors else None,
        "message": f"导入完成: 新增 {added} 个, 更新 {updated} 个" + (f", {len(errors)} 个失败" if errors else ""),
    }


# ========== Config Management ==========


@router.get("/api/config/proxy")
async def get_proxy_config(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get proxy configuration"""
    config = await container.proxy_manager.get_proxy_config()
    return {
        "success": True,
        "config": {
            "enabled": config.enabled,
            "proxy_url": config.proxy_url,
            "media_proxy_enabled": config.media_proxy_enabled,
            "media_proxy_url": config.media_proxy_url,
        },
    }


@router.get("/api/proxy/config")
async def get_proxy_config_alias(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get proxy configuration (alias for frontend compatibility)"""
    config = await container.proxy_manager.get_proxy_config()
    return {
        "proxy_enabled": config.enabled,  # Frontend expects proxy_enabled
        "proxy_url": config.proxy_url,
        "media_proxy_enabled": config.media_proxy_enabled,
        "media_proxy_url": config.media_proxy_url,
    }


@router.post("/api/proxy/config")
async def update_proxy_config_alias(
    request: ProxyConfigRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update proxy configuration (alias for frontend compatibility)"""
    try:
        await container.proxy_manager.update_proxy_config(
            enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
            media_proxy_enabled=request.media_proxy_enabled,
            media_proxy_url=request.media_proxy_url,
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": "代理配置更新成功"}


@router.post("/api/config/proxy")
async def update_proxy_config(
    request: ProxyConfigRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update proxy configuration"""
    try:
        await container.proxy_manager.update_proxy_config(
            enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
            media_proxy_enabled=request.media_proxy_enabled,
            media_proxy_url=request.media_proxy_url,
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": "代理配置更新成功"}


@router.post("/api/proxy/test")
async def test_proxy_connectivity(
    request: ProxyTestRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """测试代理是否可访问目标站点（默认 https://labs.google/）"""
    proxy_input = (request.proxy_url or "").strip()
    test_url = (request.test_url or "https://labs.google/").strip()
    timeout_seconds = int(request.timeout_seconds or 15)
    timeout_seconds = max(5, min(timeout_seconds, 60))

    if not proxy_input:
        return {"success": False, "message": "代理地址为空", "test_url": test_url}

    try:
        proxy_url = container.proxy_manager.normalize_proxy_url(proxy_input)
    except ValueError as e:
        return {"success": False, "message": str(e), "test_url": test_url}

    start_time = time.time()
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        async with AsyncSession() as session:
            resp = await session.get(
                test_url,
                proxies=proxies,
                timeout=timeout_seconds,
                impersonate="chrome120",
                allow_redirects=True,
                verify=False,
            )

        elapsed_ms = int((time.time() - start_time) * 1000)
        status_code = resp.status_code
        final_url = str(resp.url)
        ok = 200 <= status_code < 400

        return {
            "success": ok,
            "message": "代理可用" if ok else f"代理可连通，但目标返回状态码 {status_code}",
            "test_url": test_url,
            "final_url": final_url,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return {"success": False, "message": f"代理测试失败: {str(e)}", "test_url": test_url, "elapsed_ms": elapsed_ms}


@router.get("/api/config/generation")
async def get_generation_config(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get generation timeout configuration"""
    config = await container.db.get_generation_config()
    if config is None:
        config = GenerationConfig()
    return {
        "success": True,
        "config": {
            "image_timeout": config.image_timeout,
            "video_timeout": config.video_timeout,
            "max_retries": config.max_retries,
            "extension_generation_enabled": bool(getattr(config, "extension_generation_enabled", False)),
            "extension_generation_fallback_mode": str(
                getattr(config, "extension_generation_fallback_mode", "local_http_on_recaptcha")
                or "local_http_on_recaptcha"
            ),
            "sub2gen_gemini_api_keys": str(getattr(config, "sub2gen_gemini_api_keys", "") or ""),
            "sub2gen_openai_api_keys": str(getattr(config, "sub2gen_openai_api_keys", "") or ""),
            "sub2gen_openrouter_api_keys": str(getattr(config, "sub2gen_openrouter_api_keys", "") or ""),
            "sub2gen_third_party_gemini_api_keys": str(
                getattr(config, "sub2gen_third_party_gemini_api_keys", "") or ""
            ),
            "sub2gen_third_party_gemini_base_url": str(
                getattr(config, "sub2gen_third_party_gemini_base_url", "") or ""
            ),
            "cloudflare_account_id": str(getattr(config, "cloudflare_account_id", "") or ""),
            "cloudflare_api_token": str(getattr(config, "cloudflare_api_token", "") or ""),
            "sub2gen_csvgen_cookie": str(getattr(config, "sub2gen_csvgen_cookie", "") or ""),
            "sub2gen_csvgen_api_keys": str(getattr(config, "sub2gen_csvgen_api_keys", "") or ""),
            "sub2gen_cloning_model": str(
                getattr(config, "sub2gen_cloning_model", "gemini-2.5-flash") or "gemini-2.5-flash"
            ),
            "sub2gen_cloning_backend": str(
                getattr(config, "sub2gen_cloning_backend", "gemini_native") or "gemini_native"
            ),
            "sub2gen_cloning_provider_order": str(getattr(config, "sub2gen_cloning_provider_order", "") or ""),
            "sub2gen_cloning_enabled_providers": str(getattr(config, "sub2gen_cloning_enabled_providers", "") or ""),
            "sub2gen_cloning_provider_retry_count": int(
                getattr(config, "sub2gen_cloning_provider_retry_count", 1) or 1
            ),
            "sub2gen_cloning_gemini_api_keys": str(getattr(config, "sub2gen_cloning_gemini_api_keys", "") or ""),
            "sub2gen_cloning_openai_api_keys": str(getattr(config, "sub2gen_cloning_openai_api_keys", "") or ""),
            "sub2gen_cloning_openrouter_api_keys": str(
                getattr(config, "sub2gen_cloning_openrouter_api_keys", "") or ""
            ),
            "sub2gen_cloning_third_party_gemini_api_keys": str(
                getattr(config, "sub2gen_cloning_third_party_gemini_api_keys", "") or ""
            ),
            "sub2gen_cloning_third_party_gemini_base_url": str(
                getattr(config, "sub2gen_cloning_third_party_gemini_base_url", "") or ""
            ),
            "sub2gen_cloning_cloudflare_account_id": str(
                getattr(config, "sub2gen_cloning_cloudflare_account_id", "") or ""
            ),
            "sub2gen_cloning_cloudflare_api_token": str(
                getattr(config, "sub2gen_cloning_cloudflare_api_token", "") or ""
            ),
            "sub2gen_metadata_backend": str(
                getattr(config, "sub2gen_metadata_backend", "gemini_native") or "gemini_native"
            ),
            "sub2gen_metadata_provider_order": str(getattr(config, "sub2gen_metadata_provider_order", "") or ""),
            "sub2gen_metadata_enabled_providers": str(
                getattr(config, "sub2gen_metadata_enabled_providers", "") or ""
            ),
            "sub2gen_metadata_provider_retry_count": int(
                getattr(config, "sub2gen_metadata_provider_retry_count", 1) or 1
            ),
            "sub2gen_metadata_model": str(
                getattr(config, "sub2gen_metadata_model", "gemini-2.5-flash") or "gemini-2.5-flash"
            ),
            "sub2gen_metadata_enabled_models": str(getattr(config, "sub2gen_metadata_enabled_models", "") or ""),
            "sub2gen_metadata_primary_model": str(getattr(config, "sub2gen_metadata_primary_model", "") or ""),
            "sub2gen_metadata_fallback_models": str(getattr(config, "sub2gen_metadata_fallback_models", "") or ""),
            "metadata_system_prompt": str(getattr(config, "metadata_system_prompt", "") or ""),
            "cloning_image_system_prompt": str(getattr(config, "cloning_image_system_prompt", "") or ""),
            "cloning_video_system_prompt": str(getattr(config, "cloning_video_system_prompt", "") or ""),
            "task_tracker_device_id": str(getattr(config, "task_tracker_device_id", "") or ""),
            "task_tracker_device_name": str(getattr(config, "task_tracker_device_name", "") or ""),
            "task_tracker_cookies": str(getattr(config, "task_tracker_cookies", "") or ""),
            "task_tracker_device_token": str(getattr(config, "task_tracker_device_token", "") or ""),
            "task_tracker_turnstile_token": str(getattr(config, "task_tracker_turnstile_token", "") or ""),
            "task_tracker_tls_profile": str(getattr(config, "task_tracker_tls_profile", "") or ""),
        },
    }


@router.post("/api/config/generation")
async def update_generation_config(
    request: GenerationConfigRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update generation timeout configuration"""
    captcha_cfg = await container.db.get_captcha_config()
    extension_mode_active = str(getattr(captcha_cfg, "captcha_method", "") or "").strip().lower() == "extension"
    extension_generation_enabled = bool(request.extension_generation_enabled) if extension_mode_active else False
    extension_generation_fallback_mode = request.extension_generation_fallback_mode if extension_mode_active else "none"
    await container.db.update_generation_config(
        image_timeout=request.image_timeout,
        video_timeout=request.video_timeout,
        max_retries=request.max_retries,
        extension_generation_enabled=extension_generation_enabled,
        extension_generation_fallback_mode=extension_generation_fallback_mode,
        sub2gen_gemini_api_keys=request.sub2gen_gemini_api_keys,
        sub2gen_openai_api_keys=request.sub2gen_openai_api_keys,
        sub2gen_openrouter_api_keys=request.sub2gen_openrouter_api_keys,
        sub2gen_third_party_gemini_api_keys=request.sub2gen_third_party_gemini_api_keys,
        sub2gen_third_party_gemini_base_url=request.sub2gen_third_party_gemini_base_url,
        cloudflare_account_id=request.cloudflare_account_id,
        cloudflare_api_token=request.cloudflare_api_token,
        sub2gen_csvgen_cookie=request.sub2gen_csvgen_cookie,
        sub2gen_csvgen_api_keys=request.sub2gen_csvgen_api_keys,
        sub2gen_cloning_model=request.sub2gen_cloning_model,
        sub2gen_cloning_backend=request.sub2gen_cloning_backend,
        sub2gen_cloning_provider_order=request.sub2gen_cloning_provider_order,
        sub2gen_cloning_enabled_providers=request.sub2gen_cloning_enabled_providers,
        sub2gen_cloning_provider_retry_count=request.sub2gen_cloning_provider_retry_count,
        sub2gen_cloning_gemini_api_keys=request.sub2gen_cloning_gemini_api_keys,
        sub2gen_cloning_openai_api_keys=request.sub2gen_cloning_openai_api_keys,
        sub2gen_cloning_openrouter_api_keys=request.sub2gen_cloning_openrouter_api_keys,
        sub2gen_cloning_third_party_gemini_api_keys=request.sub2gen_cloning_third_party_gemini_api_keys,
        sub2gen_cloning_third_party_gemini_base_url=request.sub2gen_cloning_third_party_gemini_base_url,
        sub2gen_cloning_cloudflare_account_id=request.sub2gen_cloning_cloudflare_account_id,
        sub2gen_cloning_cloudflare_api_token=request.sub2gen_cloning_cloudflare_api_token,
        sub2gen_metadata_backend=request.sub2gen_metadata_backend,
        sub2gen_metadata_provider_order=request.sub2gen_metadata_provider_order,
        sub2gen_metadata_enabled_providers=request.sub2gen_metadata_enabled_providers,
        sub2gen_metadata_provider_retry_count=request.sub2gen_metadata_provider_retry_count,
        sub2gen_metadata_model=request.sub2gen_metadata_model,
        sub2gen_metadata_enabled_models=request.sub2gen_metadata_enabled_models,
        sub2gen_metadata_primary_model=request.sub2gen_metadata_primary_model,
        sub2gen_metadata_fallback_models=request.sub2gen_metadata_fallback_models,
        metadata_system_prompt=request.metadata_system_prompt,
        cloning_image_system_prompt=request.cloning_image_system_prompt,
        cloning_video_system_prompt=request.cloning_video_system_prompt,
        task_tracker_device_id=request.task_tracker_device_id,
        task_tracker_device_name=request.task_tracker_device_name,
        task_tracker_cookies=request.task_tracker_cookies,
        task_tracker_device_token=request.task_tracker_device_token,
        task_tracker_turnstile_token=request.task_tracker_turnstile_token,
        task_tracker_tls_profile=request.task_tracker_tls_profile,
    )

    # 🔥 Hot reload: sync database config to memory
    await container.db.reload_config_to_memory()

    return {"success": True, "message": "生成配置更新成功"}


def _require_runway_service(service: Optional[RunwayService]) -> RunwayService:
    if service is None:
        raise HTTPException(status_code=500, detail="Runway service not initialized")
    return service


def _require_geminigen_service(service: Optional[GeminiGenService]) -> GeminiGenService:
    if service is None:
        raise HTTPException(status_code=500, detail="GeminiGen service not initialized")
    return service


def _validate_json_object_text(raw: str, field_name: str) -> str:
    text = (raw or "").strip() or "{}"
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a JSON object")
    return json.dumps(parsed, ensure_ascii=False)


def _validate_json_text(raw: str, field_name: str, fallback: str, expected_type) -> str:
    text = (raw or "").strip() or fallback
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, expected_type):
        label = "array" if expected_type is list else "object"
        raise HTTPException(status_code=400, detail=f"{field_name} must be a JSON {label}")
    return json.dumps(parsed, ensure_ascii=False)


def _runway_account_payload(account) -> Dict[str, Any]:
    return {
        "id": account.id,
        "label": account.label,
        "raw_credential": account.raw_credential,
        "is_active": bool(account.is_active),
        "workspace_id": account.workspace_id or "",
        "team_id": account.team_id or "",
        "concurrency_limit": account.concurrency_limit,
        "in_flight": account.in_flight,
        "last_status": account.last_status or "",
        "last_error": account.last_error or "",
        "last_used_at": account.last_used_at.isoformat() if account.last_used_at else None,
        "created_at": account.created_at.isoformat() if account.created_at else None,
        "updated_at": account.updated_at.isoformat() if account.updated_at else None,
    }


def _clean_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _runway_model_payload(model) -> Dict[str, Any]:
    return {
        "id": model.id,
        "public_model_id": model.public_model_id,
        "display_name": model.display_name,
        "kind": model.kind,
        "task_type": model.task_type,
        "builder_key": model.builder_key,
        "default_options": model.default_options,
        "request_mapping": model.request_mapping,
        "capability_schema": model.capability_schema,
        "media_roles": model.media_roles,
        "supported_modes": model.supported_modes,
        "limits": model.limits,
        "feature_flags": model.feature_flags,
        "cost_feature": model.cost_feature,
        "source_version": model.source_version,
        "live_available": bool(model.live_available),
        "disabled_reason": model.disabled_reason or "",
        "last_synced_at": model.last_synced_at.isoformat() if model.last_synced_at else None,
        "is_enabled": bool(model.is_enabled),
        "created_at": model.created_at.isoformat() if model.created_at else None,
        "updated_at": model.updated_at.isoformat() if model.updated_at else None,
    }


def _mask_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 12:
        return "***"
    return f"{text[:6]}...{text[-4:]}"


def _geminigen_account_payload(account, generation_stats: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    active_benefits = []
    raw_benefits = getattr(account, "active_benefits_json", None)
    if raw_benefits:
        try:
            parsed_benefits = json.loads(raw_benefits)
            if isinstance(parsed_benefits, list):
                active_benefits = parsed_benefits
        except Exception:
            active_benefits = []
    generation_stats = generation_stats or {}
    now_utc = datetime.now(timezone.utc)

    def daily_limit_state(value: Optional[datetime]) -> tuple[bool, Optional[str]]:
        if not value:
            return False, None
        normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return normalized > now_utc, normalized.isoformat().replace("+00:00", "Z")

    image_gen_daily_limited, image_gen_daily_limit_reset_at = daily_limit_state(
        getattr(account, "image_gen_daily_limit_reset_at", None)
    )
    grok_image_daily_limited, grok_image_daily_limit_reset_at = daily_limit_state(
        getattr(account, "grok_image_daily_limit_reset_at", None)
    )
    video_daily_limited, video_daily_limit_reset_at = daily_limit_state(
        getattr(account, "video_daily_limit_reset_at", None)
    )
    last_status = account.last_status or ""
    last_error = account.last_error or ""
    if (
        last_error == "DAILY_LIMIT_EXCEEDED"
        and last_status.startswith("daily_limited_")
        and not image_gen_daily_limited
        and not grok_image_daily_limited
        and not video_daily_limited
    ):
        last_status = ""
        last_error = ""

    def quota_summary(
        *,
        is_max: Optional[bool],
        max_remaining: Optional[int],
        free_remaining: Optional[int],
        max_total: int,
    ) -> Dict[str, Any]:
        if is_max is True and max_remaining is not None:
            remaining = max(0, int(max_remaining))
            return {
                "tier": "max",
                "remaining": remaining,
                "used": max(0, min(max_total, max_total - remaining)),
                "max": max_total,
            }
        if free_remaining is not None:
            return {
                "tier": "free",
                "remaining": max(0, int(free_remaining)),
                "used": None,
                "max": None,
            }
        return {"tier": "unknown", "remaining": None, "used": None, "max": None}

    image_gen_quota = quota_summary(
        is_max=getattr(account, "is_image_gen_max", None),
        max_remaining=getattr(account, "remaining_image_gen_max_daily_images", None),
        free_remaining=getattr(account, "remaining_image_gen_free_daily_images", None),
        max_total=GEMINIGEN_IMAGE_GEN_MAX_DAILY,
    )
    grok_image_quota = quota_summary(
        is_max=getattr(account, "is_grok_image_max", None),
        max_remaining=getattr(account, "remaining_grok_image_max_daily_images", None),
        free_remaining=getattr(account, "remaining_grok_image_free_daily_images", None),
        max_total=GEMINIGEN_GROK_IMAGE_MAX_DAILY,
    )
    return {
        "id": account.id,
        "label": account.label,
        "raw_cookie": "",
        "raw_cookie_preview": _mask_secret(account.raw_cookie),
        "bearer_token": "",
        "bearer_token_preview": _mask_secret(account.bearer_token),
        "refresh_token": "",
        "refresh_token_preview": _mask_secret(getattr(account, "refresh_token", "")),
        "guard_id": "",
        "guard_id_preview": _mask_secret(account.guard_id),
        "turnstile_token": "",
        "turnstile_token_preview": _mask_secret(account.turnstile_token),
        "is_active": bool(account.is_active),
        "image_concurrency": account.image_concurrency,
        "video_concurrency": account.video_concurrency,
        "image_in_flight": account.image_in_flight,
        "video_in_flight": account.video_in_flight,
        "image_gen_daily_limited": image_gen_daily_limited,
        "grok_image_daily_limited": grok_image_daily_limited,
        "video_daily_limited": video_daily_limited,
        "image_gen_daily_limit_reset_at": image_gen_daily_limit_reset_at,
        "grok_image_daily_limit_reset_at": grok_image_daily_limit_reset_at,
        "video_daily_limit_reset_at": video_daily_limit_reset_at,
        "image_gen_quota": image_gen_quota,
        "grok_image_quota": grok_image_quota,
        "is_image_gen_max": getattr(account, "is_image_gen_max", None),
        "is_image_premium": getattr(account, "is_image_premium", None),
        "image_gen_concurrent_streams": getattr(account, "image_gen_concurrent_streams", None),
        "remaining_image_gen_max_daily_images": getattr(
            account,
            "remaining_image_gen_max_daily_images",
            None,
        ),
        "remaining_image_gen_free_daily_images": getattr(
            account,
            "remaining_image_gen_free_daily_images",
            None,
        ),
        "is_grok_image_max": getattr(account, "is_grok_image_max", None),
        "grok_image_concurrent_streams": getattr(account, "grok_image_concurrent_streams", None),
        "remaining_grok_image_max_daily_images": getattr(
            account,
            "remaining_grok_image_max_daily_images",
            None,
        ),
        "remaining_grok_image_free_daily_images": getattr(
            account,
            "remaining_grok_image_free_daily_images",
            None,
        ),
        "is_grok_max": getattr(account, "is_grok_max", None),
        "grok_max_concurrent_streams": getattr(account, "grok_max_concurrent_streams", None),
        "image_generated_today": int(generation_stats.get("image_generated_today") or 0),
        "image_generated_total": int(generation_stats.get("image_generated_total") or 0),
        "video_generated_today": int(generation_stats.get("video_generated_today") or 0),
        "video_generated_total": int(generation_stats.get("video_generated_total") or 0),
        "last_status": last_status,
        "last_error": last_error,
        "last_used_at": account.last_used_at.isoformat() if account.last_used_at else None,
        "profile_user_id": account.profile_user_id,
        "profile_uuid": account.profile_uuid,
        "profile_email": account.profile_email,
        "profile_full_name": account.profile_full_name,
        "profile_is_active": account.profile_is_active if account.profile_is_active is not None else None,
        "available_credit": account.available_credit,
        "plan_credit": account.plan_credit,
        "purchased_credit": account.purchased_credit,
        "locked_credit": account.locked_credit,
        "subscription_credit": account.subscription_credit,
        "plan_name": account.plan_name,
        "plan_expire_at": account.plan_expire_at.isoformat() if account.plan_expire_at else None,
        "active_benefits": active_benefits,
        "remaining_bulk_videos": account.remaining_bulk_videos,
        "remaining_daily_videos": account.remaining_daily_videos,
        "remaining_grok_max_daily_videos": account.remaining_grok_max_daily_videos,
        "remaining_grok_max_daily_720p_videos": account.remaining_grok_max_daily_720p_videos,
        "remaining_grok_max_daily_10s_videos": account.remaining_grok_max_daily_10s_videos,
        "remaining_grok_max_daily_15s_videos": account.remaining_grok_max_daily_15s_videos,
        "quota_synced_at": account.quota_synced_at.isoformat() if account.quota_synced_at else None,
        "quota_sync_status": account.quota_sync_status or "",
        "quota_sync_error": account.quota_sync_error or "",
        "profile_synced_at": account.profile_synced_at.isoformat() if account.profile_synced_at else None,
        "profile_sync_status": account.profile_sync_status or "",
        "profile_sync_error": account.profile_sync_error or "",
        "created_at": account.created_at.isoformat() if account.created_at else None,
        "updated_at": account.updated_at.isoformat() if account.updated_at else None,
    }


def _geminigen_config_payload(cfg) -> Dict[str, Any]:
    return {
        "enabled": bool(cfg.enabled),
        "video_enabled": bool(getattr(cfg, "video_enabled", True)),
        "base_url": cfg.base_url,
        "poll_interval_image_sec": cfg.poll_interval_image_sec,
        "poll_interval_video_sec": cfg.poll_interval_video_sec,
        "timeout_image_sec": cfg.timeout_image_sec,
        "timeout_video_sec": cfg.timeout_video_sec,
        "global_image_concurrency": cfg.global_image_concurrency,
        "global_video_concurrency": cfg.global_video_concurrency,
        "cache_outputs": bool(cfg.cache_outputs),
    }


@router.get("/api/admin/runway/config")
async def get_runway_admin_config(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    cfg = await container.db.get_runway_config()
    accounts = await container.db.list_runway_accounts()
    models = await container.db.list_runway_models()
    return {
        "success": True,
        "config": {
            "enabled": bool(cfg.enabled),
            "base_url": cfg.base_url,
            "poll_interval_sec": cfg.poll_interval_sec,
            "timeout_sec": cfg.timeout_sec,
            "cache_outputs": bool(cfg.cache_outputs),
        },
        "accounts": [_runway_account_payload(account) for account in accounts],
        "models": [_runway_model_payload(model) for model in models],
    }


@router.post("/api/admin/runway/accounts/teams")
async def get_runway_teams_for_credential(
    request: RunwayTeamsRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    if not request.raw_credential.strip():
        raise HTTPException(status_code=400, detail="Runway credential is required")
    service = _require_runway_service(container.runway_service)
    try:
        result = await service.get_teams_for_credential(request.raw_credential.strip())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **result}


@router.post("/api/admin/runway/config")
async def update_runway_admin_config(
    request: RunwayConfigUpdateRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    cfg = await container.db.update_runway_config(
        enabled=request.enabled,
        base_url=request.base_url,
        poll_interval_sec=request.poll_interval_sec,
        timeout_sec=request.timeout_sec,
        cache_outputs=request.cache_outputs,
    )
    return {
        "success": True,
        "config": {
            "enabled": bool(cfg.enabled),
            "base_url": cfg.base_url,
            "poll_interval_sec": cfg.poll_interval_sec,
            "timeout_sec": cfg.timeout_sec,
            "cache_outputs": bool(cfg.cache_outputs),
        },
    }


@router.post("/api/admin/runway/accounts")
async def create_runway_account(
    request: RunwayAccountRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    if not request.raw_credential.strip():
        raise HTTPException(status_code=400, detail="Runway credential is required")
    meta = RunwayService.describe_credential(request.raw_credential)
    selected_workspace_id = _clean_optional_text(request.workspace_id)
    selected_team_id = _clean_optional_text(request.team_id)
    workspace_id = selected_workspace_id or _clean_optional_text(str(meta.get("workspace_id") or ""))
    team_id = selected_team_id or selected_workspace_id or _clean_optional_text(str(meta.get("team_id") or ""))
    account_id = await container.db.create_runway_account(
        label=request.label.strip() or "Runway account",
        raw_credential=request.raw_credential.strip(),
        is_active=request.is_active,
        workspace_id=workspace_id,
        team_id=team_id,
        concurrency_limit=max(-1, int(request.concurrency_limit or 1)),
        last_status=str(meta.get("status") or ""),
        last_error=str(meta.get("error") or ""),
    )
    account = await container.db.get_runway_account(account_id)
    return {"success": True, "account": _runway_account_payload(account)}


@router.patch("/api/admin/runway/accounts/{account_id}")
async def update_runway_account(
    account_id: int,
    request: RunwayAccountUpdateRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    account = await container.db.get_runway_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Runway account not found")
    updates: Dict[str, Any] = {}
    if request.label is not None:
        updates["label"] = request.label.strip()
    if request.raw_credential is not None:
        updates["raw_credential"] = request.raw_credential.strip()
        meta = RunwayService.describe_credential(request.raw_credential)
        if request.workspace_id is None:
            updates["workspace_id"] = _clean_optional_text(str(meta.get("workspace_id") or ""))
        if request.team_id is None:
            updates["team_id"] = _clean_optional_text(str(meta.get("team_id") or ""))
        updates["last_status"] = str(meta.get("status") or "")
        updates["last_error"] = str(meta.get("error") or "")
    if request.is_active is not None:
        updates["is_active"] = request.is_active
    if request.workspace_id is not None:
        updates["workspace_id"] = _clean_optional_text(request.workspace_id)
    if request.team_id is not None:
        updates["team_id"] = _clean_optional_text(request.team_id)
    if request.concurrency_limit is not None:
        updates["concurrency_limit"] = max(-1, int(request.concurrency_limit))
    await container.db.update_runway_account(account_id, **updates)
    account = await container.db.get_runway_account(account_id)
    return {"success": True, "account": _runway_account_payload(account)}


@router.get("/api/admin/runway/accounts/{account_id}/teams")
async def get_runway_account_teams(
    account_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    service = _require_runway_service(container.runway_service)
    try:
        result = await service.get_account_teams(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **result}


@router.delete("/api/admin/runway/accounts/{account_id}")
async def delete_runway_account(
    account_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    await container.db.delete_runway_account(account_id)
    return {"success": True, "account_id": account_id}


@router.post("/api/admin/runway/accounts/{account_id}/test")
async def test_runway_account(
    account_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    service = _require_runway_service(container.runway_service)
    result = await service.test_account(account_id)
    account = await container.db.get_runway_account(account_id)
    return {
        "success": bool(result.get("success")),
        "status": result.get("status"),
        "error": result.get("error"),
        "account": _runway_account_payload(account) if account else None,
    }


@router.post("/api/admin/runway/models")
async def upsert_runway_model(
    request: RunwayModelRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    public_id = request.public_model_id.strip()
    if not public_id.startswith("runway-"):
        raise HTTPException(status_code=400, detail="Runway public model id must start with runway-")
    if request.kind not in {"image", "video", "audio", "upscale"}:
        raise HTTPException(status_code=400, detail="kind must be image, video, audio, or upscale")
    if not request.task_type.strip():
        raise HTTPException(status_code=400, detail="task_type is required")
    await container.db.upsert_runway_model(
        public_model_id=public_id,
        display_name=request.display_name.strip() or public_id,
        kind=request.kind,
        task_type=request.task_type.strip(),
        builder_key=request.builder_key.strip(),
        default_options=_validate_json_object_text(request.default_options, "default_options"),
        request_mapping=_validate_json_object_text(request.request_mapping, "request_mapping"),
        capability_schema=_validate_json_object_text(request.capability_schema, "capability_schema"),
        media_roles=_validate_json_text(request.media_roles, "media_roles", "[]", list),
        supported_modes=_validate_json_text(request.supported_modes, "supported_modes", "[]", list),
        limits=_validate_json_object_text(request.limits, "limits"),
        feature_flags=_validate_json_text(request.feature_flags, "feature_flags", "[]", list),
        cost_feature=request.cost_feature.strip(),
        source_version=request.source_version.strip(),
        live_available=bool(request.live_available),
        disabled_reason=request.disabled_reason.strip(),
        is_enabled=request.is_enabled,
    )
    model = await container.db.get_runway_model(public_id)
    return {"success": True, "model": _runway_model_payload(model)}


@router.patch("/api/admin/runway/models/{model_id}")
async def update_runway_model(
    model_id: int,
    request: RunwayModelUpdateRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    models = await container.db.list_runway_models()
    existing = next((m for m in models if m.id == model_id), None)
    if not existing:
        raise HTTPException(status_code=404, detail="Runway model not found")
    public_id = (request.public_model_id if request.public_model_id is not None else existing.public_model_id).strip()
    if not public_id.startswith("runway-"):
        raise HTTPException(status_code=400, detail="Runway public model id must start with runway-")
    kind = (request.kind if request.kind is not None else existing.kind).strip()
    if kind not in {"image", "video", "audio", "upscale"}:
        raise HTTPException(status_code=400, detail="kind must be image, video, audio, or upscale")
    await container.db.upsert_runway_model(
        public_model_id=public_id,
        display_name=(request.display_name if request.display_name is not None else existing.display_name).strip()
        or public_id,
        kind=kind,
        task_type=(request.task_type if request.task_type is not None else existing.task_type).strip(),
        builder_key=(request.builder_key if request.builder_key is not None else existing.builder_key).strip(),
        default_options=_validate_json_object_text(
            request.default_options if request.default_options is not None else existing.default_options,
            "default_options",
        ),
        request_mapping=_validate_json_object_text(
            request.request_mapping if request.request_mapping is not None else existing.request_mapping,
            "request_mapping",
        ),
        capability_schema=_validate_json_object_text(
            request.capability_schema if request.capability_schema is not None else existing.capability_schema,
            "capability_schema",
        ),
        media_roles=_validate_json_text(
            request.media_roles if request.media_roles is not None else existing.media_roles,
            "media_roles",
            "[]",
            list,
        ),
        supported_modes=_validate_json_text(
            request.supported_modes if request.supported_modes is not None else existing.supported_modes,
            "supported_modes",
            "[]",
            list,
        ),
        limits=_validate_json_object_text(
            request.limits if request.limits is not None else existing.limits,
            "limits",
        ),
        feature_flags=_validate_json_text(
            request.feature_flags if request.feature_flags is not None else existing.feature_flags,
            "feature_flags",
            "[]",
            list,
        ),
        cost_feature=(request.cost_feature if request.cost_feature is not None else existing.cost_feature).strip(),
        source_version=(
            request.source_version if request.source_version is not None else existing.source_version
        ).strip(),
        live_available=bool(request.live_available if request.live_available is not None else existing.live_available),
        disabled_reason=(
            request.disabled_reason if request.disabled_reason is not None else existing.disabled_reason
        ).strip(),
        is_enabled=bool(request.is_enabled if request.is_enabled is not None else existing.is_enabled),
    )
    if public_id != existing.public_model_id:
        await container.db.delete_runway_model(model_id)
    model = await container.db.get_runway_model(public_id)
    return {"success": True, "model": _runway_model_payload(model)}


@router.delete("/api/admin/runway/models/{model_id}")
async def delete_runway_model(
    model_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    await container.db.delete_runway_model(model_id)
    return {"success": True, "model_id": model_id}


@router.post("/api/admin/runway/models/sync")
async def sync_runway_models(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    service = _require_runway_service(container.runway_service)
    sync_result = await service.sync_models()
    if isinstance(sync_result, dict):
        synced = int(sync_result.get("synced") or 0)
        blocked = int(sync_result.get("blocked") or 0)
        disabled_task_types = int(sync_result.get("disabled_task_types") or 0)
    else:
        synced = int(sync_result or 0)
        blocked = 0
        disabled_task_types = 0
    return {
        "success": True,
        "synced": synced,
        "blocked": blocked,
        "disabled_task_types": disabled_task_types,
        "models": [_runway_model_payload(m) for m in await container.db.list_runway_models()],
    }


@router.get("/api/admin/geminigen/config")
async def get_geminigen_admin_config(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    cfg = await container.db.get_geminigen_config()
    accounts = await container.db.list_geminigen_accounts()
    generation_stats = await container.db.get_geminigen_account_generation_stats(
        [account.id for account in accounts if account.id]
    )
    if container.geminigen_service is not None:
        container.geminigen_service.schedule_stale_account_profile_refresh(accounts)
    return {
        "success": True,
        "config": _geminigen_config_payload(cfg),
        "accounts": [
            _geminigen_account_payload(account, generation_stats.get(int(account.id or 0), {})) for account in accounts
        ],
        "models": GeminiGenService.model_catalog(video_enabled=bool(getattr(cfg, "video_enabled", True))),
    }


@router.post("/api/admin/geminigen/config")
async def update_geminigen_admin_config(
    request: GeminiGenConfigUpdateRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    cfg = await container.db.update_geminigen_config(
        enabled=request.enabled,
        video_enabled=request.video_enabled,
        base_url=request.base_url,
        poll_interval_image_sec=request.poll_interval_image_sec,
        poll_interval_video_sec=request.poll_interval_video_sec,
        timeout_image_sec=request.timeout_image_sec,
        timeout_video_sec=request.timeout_video_sec,
        global_image_concurrency=request.global_image_concurrency,
        global_video_concurrency=request.global_video_concurrency,
        cache_outputs=request.cache_outputs,
    )
    return {
        "success": True,
        "config": _geminigen_config_payload(cfg),
    }


@router.get("/api/admin/geminigen/models/status")
async def get_geminigen_model_status(
    window: str = Query("1h"),
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    service = _require_geminigen_service(container.geminigen_service)
    try:
        return await service.get_model_status(window=window)
    except Exception as exc:
        accounts = await container.db.list_geminigen_accounts()
        cfg = await container.db.get_geminigen_config()
        return {
            "success": False,
            "status": "unavailable",
            "error": str(exc),
            "window": window,
            "generated_at": None,
            "models": [],
            "summary": {
                "operational": 0,
                "degraded": 0,
                "outage": 0,
                "unknown": 0,
                "matching_model_groups": 0,
            },
            "geminigen": {
                "enabled": bool(cfg.enabled),
                "active_account_count": len([account for account in accounts if account.is_active]),
                "image_in_flight": sum(int(account.image_in_flight or 0) for account in accounts),
                "video_in_flight": sum(int(account.video_in_flight or 0) for account in accounts),
            },
        }


@router.post("/api/admin/geminigen/accounts")
async def create_geminigen_account(
    request: GeminiGenAccountRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    if not request.bearer_token.strip():
        raise HTTPException(status_code=400, detail="GeminiGen bearer token is required")
    meta = GeminiGenService.describe_credential(request.raw_cookie, request.bearer_token, request.guard_id)
    account_id = await container.db.create_geminigen_account(
        label=request.label.strip() or "GeminiGen account",
        raw_cookie="",
        bearer_token=request.bearer_token.strip(),
        refresh_token=request.refresh_token.strip(),
        guard_id="",
        turnstile_token=request.turnstile_token.strip(),
        is_active=request.is_active,
        image_concurrency=max(-1, int(request.image_concurrency or 5)),
        video_concurrency=max(-1, int(request.video_concurrency or 5)),
        last_status=str(meta.get("status") or ""),
        last_error=str(meta.get("error") or ""),
    )
    account = await container.db.get_geminigen_account(account_id)
    return {"success": True, "account": _geminigen_account_payload(account)}


@router.patch("/api/admin/geminigen/accounts/{account_id}")
async def update_geminigen_account(
    account_id: int,
    request: GeminiGenAccountUpdateRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    account = await container.db.get_geminigen_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="GeminiGen account not found")
    updates: Dict[str, Any] = {}
    for field in ("label", "raw_cookie", "bearer_token", "refresh_token", "guard_id", "turnstile_token"):
        value = getattr(request, field)
        if value is not None:
            stripped = value.strip()
            if field in {"raw_cookie", "guard_id"}:
                updates[field] = ""
            elif field in {"bearer_token", "refresh_token"} and not stripped:
                continue
            else:
                updates[field] = stripped
    if request.is_active is not None:
        updates["is_active"] = request.is_active
    if request.image_concurrency is not None:
        updates["image_concurrency"] = max(-1, int(request.image_concurrency))
    if request.video_concurrency is not None:
        updates["video_concurrency"] = max(-1, int(request.video_concurrency))
    if "bearer_token" in updates:
        meta = GeminiGenService.describe_credential(
            "",
            updates.get("bearer_token", account.bearer_token),
            "",
        )
        updates["last_status"] = str(meta.get("status") or "")
        updates["last_error"] = str(meta.get("error") or "")
    await container.db.update_geminigen_account(account_id, **updates)
    account = await container.db.get_geminigen_account(account_id)
    return {"success": True, "account": _geminigen_account_payload(account)}


@router.delete("/api/admin/geminigen/accounts/{account_id}")
async def delete_geminigen_account(
    account_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    account = await container.db.get_geminigen_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="GeminiGen account not found")
    cleared = await container.db.clear_geminigen_queue(
        account_id=account_id,
        reason="GeminiGen account deleted by admin",
    )
    deleted = await container.db.delete_geminigen_account(account_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="GeminiGen account not found")
    return {"success": True, "account_id": account_id, **cleared}


@router.post("/api/admin/geminigen/queue/clear")
async def clear_geminigen_queue(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    result = await container.db.clear_geminigen_queue(reason="Manually cleared by admin")
    accounts = await container.db.list_geminigen_accounts()
    return {
        "success": True,
        **result,
        "accounts": [_geminigen_account_payload(account) for account in accounts],
    }


@router.post("/api/admin/geminigen/accounts/{account_id}/clear-slots")
async def clear_geminigen_account_slots(
    account_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    account = await container.db.get_geminigen_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="GeminiGen account not found")
    result = await container.db.clear_geminigen_queue(account_id=account_id, reason="Manually cleared by admin")
    account = await container.db.get_geminigen_account(account_id)
    return {
        "success": True,
        **result,
        "account": _geminigen_account_payload(account) if account else None,
    }


@router.post("/api/admin/geminigen/accounts/{account_id}/test")
async def test_geminigen_account(
    account_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    service = _require_geminigen_service(container.geminigen_service)
    result = await service.test_account(account_id)
    account = await container.db.get_geminigen_account(account_id)
    return {
        "success": bool(result.get("success")),
        "status": result.get("status"),
        "error": result.get("error"),
        "account": _geminigen_account_payload(account) if account else None,
    }


def _event_calendar_path() -> Path:
    return Path(__file__).resolve().parent.parent / "services" / "event_calendar_2026.csv"


@router.get("/api/admin/event-calendar")
async def get_event_calendar(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Read editable event calendar rows from CSV."""
    csv_path = _event_calendar_path()
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail="Event calendar CSV not found")

    events: List[Dict[str, str]] = []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                events.append(
                    {
                        "date": str(row.get("Date") or "").strip(),
                        "month": str(row.get("Month") or "").strip(),
                        "event_name": str(row.get("Event Name") or "").strip(),
                    }
                )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read event calendar CSV: {exc}") from exc

    return {"success": True, "events": events}


@router.post("/api/admin/event-calendar")
async def update_event_calendar(
    request: EventCalendarUpdateRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Replace editable event calendar CSV rows."""
    normalized_rows: List[Dict[str, str]] = []
    for idx, item in enumerate(request.events):
        date_value = item.date.strip()
        month_value = item.month.strip()
        event_name = item.event_name.strip()
        if not date_value or not month_value or not event_name:
            raise HTTPException(status_code=400, detail=f"Row {idx + 1} must include date, month, and event_name")
        normalized_rows.append(
            {
                "Date": date_value,
                "Month": month_value,
                "Event Name": event_name,
            }
        )

    csv_path = _event_calendar_path()
    try:
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["Date", "Month", "Event Name"])
            writer.writeheader()
            writer.writerows(normalized_rows)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write event calendar CSV: {exc}") from exc

    return {"success": True, "message": "Event calendar updated", "count": len(normalized_rows)}


@router.get("/api/call-logic/config")
async def get_call_logic_config(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get token call logic configuration."""
    config_obj = await container.db.get_call_logic_config()
    call_mode = getattr(config_obj, "call_mode", None)
    if call_mode not in ("default", "polling"):
        call_mode = "polling" if getattr(config_obj, "polling_mode_enabled", False) else "default"
    return {
        "success": True,
        "config": {
            "call_mode": call_mode,
            "polling_mode_enabled": call_mode == "polling",
        },
    }


@router.post("/api/call-logic/config")
async def update_call_logic_config(
    request: CallLogicConfigRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update token call logic configuration."""
    call_mode = request.call_mode if request.call_mode in ("default", "polling") else None
    if call_mode is None:
        raise HTTPException(status_code=400, detail="Invalid call_mode")

    await container.db.update_call_logic_config(call_mode)
    await container.db.reload_config_to_memory()

    return {
        "success": True,
        "message": "Token轮询模式保存成功",
        "config": {
            "call_mode": call_mode,
            "polling_mode_enabled": call_mode == "polling",
        },
    }


# ========== System Info ==========


@router.get("/api/system/info")
async def get_system_info(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get system information"""
    stats = await container.db.get_system_info_stats()

    return {
        "success": True,
        "info": {
            "total_tokens": stats["total_tokens"],
            "active_tokens": stats["active_tokens"],
            "total_credits": stats["total_credits"],
            "version": "1.0.0",
        },
    }


# ========== Additional Routes for Frontend Compatibility ==========


@router.post("/api/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
):
    """Login endpoint (alias for /api/admin/login)"""
    return await admin_login(payload, request, response)


@router.post("/api/logout")
async def logout(
    request: Request,
    response: Response,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Logout endpoint (alias for /api/admin/logout)"""
    return await admin_logout(request, response, token)


@router.get("/health")
async def health_check(
    container: AppContainer = Depends(get_container),
):
    """Public health check endpoint - no auth required"""
    try:
        snapshot = await build_public_health_snapshot(container.db)
        database_status = await container.db.health_snapshot()
        snapshot.update(database_status)
        redis_status = redis_runtime.status_snapshot()
        snapshot["redis_ready"] = redis_status["redis_ready"]
        snapshot["event_consumer_ready"] = redis_status["event_consumer_ready"]
        snapshot["maintenance"] = redis_runtime.maintenance_snapshot()
        snapshot["degraded"] = bool(
            not database_status.get("database_ready", False)
            or redis_runtime.maintenance_active
            or (
                redis_runtime.mode != "off"
                and (not redis_status["redis_ready"] or not redis_status["event_consumer_ready"])
            )
        )
        snapshot["redis"] = redis_status
        return snapshot
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "backend_running": True,
                "database_ready": False,
                "database_backend": getattr(container.db, "backend", "unknown"),
                "database_revision": getattr(container.db, "database_revision", None),
                "redis_ready": redis_runtime.ready,
                "event_consumer_ready": redis_runtime.event_consumer_ready,
                "degraded": True,
                "has_active_tokens": False,
                "error": "database_unavailable",
                "maintenance": redis_runtime.maintenance_snapshot(),
            },
        )


@router.get("/api/admin/maintenance")
async def get_maintenance_state(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    return {"success": True, "maintenance": redis_runtime.maintenance_snapshot()}


@router.post("/api/admin/maintenance")
async def update_maintenance_state(
    payload: MaintenanceRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        state = await redis_runtime.set_maintenance(
            payload.active,
            reason=payload.reason,
            owner="admin",
        )
    except RedisUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="redis_unavailable",
            headers={"Retry-After": "5"},
        ) from exc
    return {"success": True, "maintenance": state}


def _websocket_uses_api_only_host(websocket: WebSocket) -> bool:
    configured = {
        value.strip().lower()
        for value in str(os.environ.get("SUB2GEN_API_ONLY_HOST", "") or "").split(",")
        if value.strip()
    }
    if not configured:
        return False
    forwarded = str(websocket.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    host = forwarded or str(websocket.headers.get("host") or "").strip()
    hostname = urlsplit(f"//{host}").hostname or host.split(":", 1)[0]
    return hostname.lower() in configured


def _websocket_origin_is_same_origin(websocket: WebSocket) -> bool:
    origin = str(websocket.headers.get("origin") or "").strip()
    if not origin or origin.lower() == "null":
        return False
    parsed_origin = urlsplit(origin)
    if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.hostname:
        return False

    forwarded_host = str(websocket.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    expected_host = forwarded_host or str(websocket.headers.get("host") or "").strip()
    forwarded_proto = str(websocket.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    expected_scheme = forwarded_proto or ("https" if websocket.url.scheme == "wss" else "http")
    parsed_expected = urlsplit(f"{expected_scheme}://{expected_host}")

    def normalized_origin(parsed):
        default_port = 443 if parsed.scheme == "https" else 80
        return parsed.scheme, (parsed.hostname or "").lower(), parsed.port or default_port

    return normalized_origin(parsed_origin) == normalized_origin(parsed_expected)


@router.websocket("/api/admin/events/ws")
async def admin_events_websocket(
    websocket: WebSocket,
    container: AppContainer = Depends(get_container),
):
    """Authenticated dashboard event feed with Redis cursor replay."""
    if _websocket_uses_api_only_host(websocket):
        await websocket.close(code=1008, reason="admin_websocket_not_available_on_api_host")
        return
    if not _websocket_origin_is_same_origin(websocket):
        await websocket.close(code=1008, reason="same_origin_required")
        return
    session_token = str(websocket.cookies.get(ADMIN_SESSION_COOKIE_NAME) or "").strip()
    if not await is_admin_session_token_valid(session_token, container.db):
        await websocket.close(code=1008, reason="admin_session_required")
        return

    await websocket.accept()
    if not redis_runtime.ready:
        await websocket.close(code=1013, reason="redis_unavailable")
        return

    cursor = str(websocket.query_params.get("cursor") or "$").strip() or "$"
    redis_runtime.websocket_clients += 1
    try:
        if await redis_runtime.cursor_was_trimmed(cursor):
            await websocket.send_json(
                {
                    "type": "resync",
                    "data": {"reason": "cursor_trimmed"},
                    "cursor": cursor,
                }
            )
            cursor = "$"
        await websocket.send_json(
            {
                "type": "redis_state",
                "data": redis_runtime.status_snapshot(),
                "cursor": cursor,
            }
        )
        while True:
            try:
                events = await redis_runtime.read_events(cursor, block_ms=15_000, count=100)
            except RedisUnavailableError:
                await websocket.close(code=1013, reason="redis_unavailable")
                return
            if not events:
                await websocket.send_json({"type": "ping", "cursor": cursor})
                continue
            for event in events:
                cursor = event.cursor
                await websocket.send_json(event.as_dict())
    except WebSocketDisconnect:
        return
    finally:
        redis_runtime.websocket_clients = max(0, redis_runtime.websocket_clients - 1)


@router.get("/api/stats")
async def get_stats(
    request: Request,
    response: Response,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get statistics for dashboard"""
    if get_admin_token_from_cookie(request) != token:
        _set_admin_session_cookie(
            response,
            request,
            token,
            remember_me=True,
        )
    return await container.db.get_dashboard_stats()


@router.get("/api/logs")
async def get_logs(
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = Query(None, description="Search request logs by job id or text fragment"),
    exclude_operations: Optional[str] = Query(
        None,
        description="Comma-separated operation values to exclude (e.g. generate_image,generate_video)",
    ),
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get lightweight request logs for list view (paginated)."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    exclude_list = None
    if exclude_operations and exclude_operations.strip():
        exclude_list = [x.strip() for x in exclude_operations.split(",") if x.strip()]
    search_text = (search or "").strip()
    total = await container.db.count_request_logs(exclude_operations=exclude_list, search=search_text)
    logs = await container.db.get_logs(
        limit=limit, offset=offset, include_payload=False, exclude_operations=exclude_list, search=search_text
    )

    result = []
    for log in logs:
        raw_status_code = log.get("status_code")
        try:
            status_code = int(raw_status_code) if raw_status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        captcha_ua = _extract_captcha_user_agent_metadata(log.get("response_body_excerpt"))
        result.append(
            {
                "id": log.get("id"),
                "job_id": log.get("job_id") or _extract_log_job_id(log.get("response_body_excerpt")),
                "token_id": log.get("token_id"),
                "token_email": log.get("token_email"),
                "token_username": log.get("token_username"),
                "api_key_id": log.get("api_key_id"),
                "api_key_label": log.get("api_key_label"),
                "api_key_prefix": log.get("api_key_prefix"),
                "operation": log.get("operation"),
                "status_code": status_code if status_code is not None else raw_status_code,
                "duration": log.get("duration"),
                "status_text": log.get("status_text") or "",
                "progress": log.get("progress") or 0,
                "created_at": log.get("created_at"),
                "updated_at": log.get("updated_at"),
                "payload_available": bool(log.get("payload_available")),
                "payload_storage_error": log.get("payload_storage_error"),
                "error_summary": _extract_error_summary(log.get("response_body_excerpt"))
                if status_code is not None and status_code >= 400
                else "",
                "captcha_user_agent_set": captcha_ua["captcha_user_agent_set"],
                "captcha_provider": captcha_ua["captcha_provider"],
            }
        )
    return {"logs": result, "total": total, "limit": limit, "offset": offset}


@router.get("/api/logs/{log_id}")
async def get_log_detail(
    log_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get single request log detail (payload loaded on demand)"""
    log = await container.db.get_log_detail(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="日志不存在")

    request_body = log.get("request_body")
    response_body = log.get("response_body")
    payload_available = bool(log.get("payload_available"))
    if payload_available and log.get("payload_object_key") and container.db.log_payload_manager is not None:
        try:
            stored_payload = await container.db.log_payload_manager.load(str(log["payload_object_key"]))
            if stored_payload:
                request_body = stored_payload.get("request_body", request_body)
                response_body = stored_payload.get("response_body", response_body)
        except Exception as exc:
            log["payload_storage_error"] = f"payload_read_failed:{type(exc).__name__}"

    error_summary = _extract_error_summary(response_body)
    captcha_ua = _extract_captcha_user_agent_metadata(response_body)

    return {
        "id": log.get("id"),
        "job_id": log.get("job_id") or _extract_log_job_id(response_body, request_body),
        "token_id": log.get("token_id"),
        "token_email": log.get("token_email"),
        "token_username": log.get("token_username"),
        "api_key_id": log.get("api_key_id"),
        "api_key_label": log.get("api_key_label"),
        "api_key_prefix": log.get("api_key_prefix"),
        "operation": log.get("operation"),
        "status_code": log.get("status_code"),
        "duration": log.get("duration"),
        "status_text": log.get("status_text") or "",
        "progress": log.get("progress") or 0,
        "created_at": log.get("created_at"),
        "updated_at": log.get("updated_at"),
        "payload_available": payload_available,
        "payload_storage_error": log.get("payload_storage_error"),
        "request_size_bytes": log.get("request_size_bytes") or 0,
        "response_size_bytes": log.get("response_size_bytes") or 0,
        "error_summary": error_summary,
        "captcha_user_agent_set": captcha_ua["captcha_user_agent_set"],
        "captcha_provider": captcha_ua["captcha_provider"],
        "request_body": request_body,
        "response_body": response_body,
    }


@router.post("/api/logs/{log_id}/geminigen/cancel")
async def cancel_geminigen_log_task(
    log_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    result = await container.db.cancel_geminigen_task_by_log_id(log_id, reason="Cancelled by admin")
    if not result.get("success"):
        raise HTTPException(
            status_code=int(result.get("status_code") or 400),
            detail=result.get("error") or "Could not cancel GeminiGen task",
        )
    return result


@router.delete("/api/logs")
async def clear_logs(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Clear all logs"""
    try:
        await container.db.clear_all_logs()
        return {"success": True, "message": "所有日志已清空"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/admin/config")
async def get_admin_config(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get admin configuration"""
    admin_config = await container.db.get_admin_config()

    return {
        "admin_username": config.admin_username,
        "api_key": config.api_key,
        "error_ban_threshold": admin_config.error_ban_threshold,
        "error_ban_enabled": admin_config.error_ban_enabled,
        "debug_enabled": config.debug_enabled,  # Return actual debug status
    }


@router.post("/api/admin/config")
async def update_admin_config(
    request: UpdateAdminConfigRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update admin configuration (error ban threshold and/or auto-disable toggle)"""
    kwargs: Dict[str, Any] = {}
    if request.error_ban_threshold is not None:
        kwargs["error_ban_threshold"] = request.error_ban_threshold
    if request.error_ban_enabled is not None:
        kwargs["error_ban_enabled"] = 1 if request.error_ban_enabled else 0
    if not kwargs:
        raise HTTPException(status_code=400, detail="No fields to update")
    await container.db.update_admin_config(**kwargs)

    return {"success": True, "message": "配置更新成功"}


@router.post("/api/admin/password")
async def update_admin_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update admin password"""
    return await change_password(payload, request, response, token)


@router.post("/api/admin/apikey")
async def update_api_key(
    request: UpdateAPIKeyRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update API key (for external API calls, NOT for admin login)"""
    # Update API key in database
    await container.db.update_admin_config(api_key=request.new_api_key)

    # 🔥 Hot reload: sync database config to memory
    await container.db.reload_config_to_memory()

    return {"success": True, "message": "API Key更新成功"}


@router.post("/api/admin/database/restore")
async def restore_sqlite_database(
    database: UploadFile = File(...),
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Restore sub2gen.db from an uploaded SQLite file."""
    if container.db is None:
        raise HTTPException(status_code=500, detail="Database service is not initialized")
    if getattr(container.db, "backend", "sqlite") != "sqlite":
        raise HTTPException(status_code=410, detail="sqlite_restore_removed")

    db_path = Path(container.db.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path = db_path.with_name(f"{db_path.name}.upload-{int(time.time())}-{secrets.token_hex(4)}")
    backup_path = db_path.with_name(f"{db_path.name}.backup-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")

    written = 0
    try:
        async with container.database_restore_lock:
            with open(upload_path, "wb") as out:
                while True:
                    chunk = await database.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_SQLITE_RESTORE_BYTES:
                        raise HTTPException(status_code=413, detail="Uploaded database is too large")
                    out.write(chunk)

            validation = _validate_uploaded_sqlite_database(upload_path)

            await container.db.close_runtime_connections()
            if db_path.exists():
                await asyncio.to_thread(_create_sqlite_database_snapshot, db_path, backup_path)

            upload_path.replace(db_path)

            await container.db.init_db()
            await container.db.check_and_migrate_db(config.get_raw_config())
            await container.db.cache_schema_capabilities()
            await container.db.reload_config_to_memory()

        return {
            "success": True,
            "message": "Database restored. Log in again if your session is replaced by the uploaded DB.",
            "database_path": str(db_path),
            "backup_path": str(backup_path) if backup_path.exists() else None,
            "size": validation["size"],
            "table_count": validation["table_count"],
        }
    finally:
        await database.close()
        try:
            if upload_path.exists():
                upload_path.unlink()
        except Exception:
            pass


@router.get("/api/admin/database/download")
async def download_sqlite_database(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Download a consistent snapshot of the current sub2gen.db SQLite database."""
    if container.db is None:
        raise HTTPException(status_code=500, detail="Database service is not initialized")
    if getattr(container.db, "backend", "sqlite") != "sqlite":
        raise HTTPException(status_code=410, detail="sqlite_download_removed")

    db_path = Path(container.db.db_path)
    if not db_path.is_file():
        raise HTTPException(status_code=404, detail="Database file not found")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    snapshot_path = db_path.with_name(
        f"{db_path.stem}.download-{timestamp}-{secrets.token_hex(4)}{db_path.suffix or '.db'}"
    )
    download_filename = f"{db_path.stem}-{timestamp}.db"

    try:
        async with container.database_restore_lock:
            await asyncio.to_thread(_create_sqlite_database_snapshot, db_path, snapshot_path)

        if not snapshot_path.is_file() or snapshot_path.stat().st_size <= 0:
            raise HTTPException(status_code=500, detail="Database snapshot is empty")

        return FileResponse(
            path=snapshot_path,
            media_type="application/vnd.sqlite3",
            filename=download_filename,
            background=BackgroundTask(_unlink_path, snapshot_path),
        )
    except HTTPException:
        _unlink_path(snapshot_path)
        raise
    except Exception as exc:
        _unlink_path(snapshot_path)
        raise HTTPException(status_code=500, detail=f"Database download failed: {exc}") from exc


def _google_drive_backups(service: Optional[GoogleDriveBackupService]) -> GoogleDriveBackupService:
    if service is None:
        raise HTTPException(status_code=503, detail="Google Drive backup service is not initialized")
    return service


def _google_drive_error(exc: GoogleDriveBackupError) -> HTTPException:
    message = str(exc)
    status = 409 if "already running" in message else 400
    return HTTPException(status_code=status, detail=message)


@router.get("/api/admin/backups/google-drive/status")
async def google_drive_backup_status(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    return {"success": True, **_google_drive_backups(container.google_drive_backup_service).public_status()}


@router.put("/api/admin/backups/google-drive/config")
async def update_google_drive_backup_config(
    payload: GoogleDriveBackupConfigRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    service = _google_drive_backups(container.google_drive_backup_service)
    try:
        status = service.update_config(
            enabled=payload.enabled,
            schedule_time=payload.schedule_time,
            timezone_name=payload.timezone,
            retention=payload.retention,
        )
    except GoogleDriveBackupError as exc:
        raise _google_drive_error(exc) from exc
    return {"success": True, **status}


@router.post("/api/admin/backups/google-drive/oauth/start")
async def start_google_drive_oauth(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        authorization_url = _google_drive_backups(container.google_drive_backup_service).begin_oauth(token)
    except GoogleDriveBackupError as exc:
        raise _google_drive_error(exc) from exc
    return {"success": True, "authorization_url": authorization_url}


@router.get("/api/admin/backups/google-drive/oauth/callback")
async def finish_google_drive_oauth(
    request: Request,
    state: str = Query(""),
    code: str = Query(""),
    error: str = Query(""),
    container: AppContainer = Depends(get_container),
):
    if error:
        return RedirectResponse(url="/manage?google_drive=denied", status_code=303)
    session_token = get_admin_token_from_cookie(request) or ""
    if not session_token or not code or not state:
        raise HTTPException(status_code=400, detail="OAuth callback is incomplete")
    try:
        await _google_drive_backups(container.google_drive_backup_service).finish_oauth(
            state=state,
            code=code,
            admin_session=session_token,
        )
    except GoogleDriveBackupError as exc:
        raise _google_drive_error(exc) from exc
    return RedirectResponse(url="/manage?google_drive=connected", status_code=303)


@router.post("/api/admin/backups/google-drive/test")
async def test_google_drive_backup(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        return await _google_drive_backups(container.google_drive_backup_service).test_connection()
    except GoogleDriveBackupError as exc:
        raise _google_drive_error(exc) from exc


@router.post("/api/admin/backups/google-drive/disconnect")
async def disconnect_google_drive_backup(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    await _google_drive_backups(container.google_drive_backup_service).disconnect()
    return {"success": True}


@router.post("/api/admin/backups/google-drive/backups")
async def create_google_drive_backup(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        job = await _google_drive_backups(container.google_drive_backup_service).start_backup("manual")
    except GoogleDriveBackupError as exc:
        raise _google_drive_error(exc) from exc
    return JSONResponse(status_code=202, content={"success": True, "job": job})


@router.get("/api/admin/backups/google-drive/job")
async def get_google_drive_backup_job(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    return {"success": True, "job": _google_drive_backups(container.google_drive_backup_service).public_status().get("job")}


@router.get("/api/admin/backups/google-drive/backups")
async def list_google_drive_backups(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    service = _google_drive_backups(container.google_drive_backup_service)
    if not service.public_status().get("connected"):
        return {"success": True, "backups": []}
    try:
        backups = await service.list_backups()
    except GoogleDriveBackupError as exc:
        raise _google_drive_error(exc) from exc
    return {"success": True, "backups": backups}


@router.delete("/api/admin/backups/google-drive/backups/{file_id}")
async def delete_google_drive_backup(
    file_id: str,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    try:
        await _google_drive_backups(container.google_drive_backup_service).delete_backup(file_id)
    except GoogleDriveBackupError as exc:
        raise _google_drive_error(exc) from exc
    return {"success": True}


@router.post("/api/admin/backups/google-drive/backups/{file_id}/restore")
async def restore_google_drive_backup(
    file_id: str,
    payload: GoogleDriveRestoreRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    if payload.confirmation.strip() != "RESTORE":
        raise HTTPException(status_code=400, detail="Type RESTORE to confirm")
    if not await container.db.is_admin_session_recent(token, 15 * 60):
        raise HTTPException(status_code=403, detail="Log in again before restoring a backup")
    try:
        job = await _google_drive_backups(container.google_drive_backup_service).start_restore(file_id)
    except GoogleDriveBackupError as exc:
        raise _google_drive_error(exc) from exc
    return JSONResponse(status_code=202, content={"success": True, "job": job})


@router.get("/api/admin/managed-apikeys")
async def list_managed_api_keys(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    if not container.api_key_manager:
        raise HTTPException(status_code=503, detail="API key manager not initialized")
    keys = await container.db.list_api_keys()
    for row in keys:
        row["requires_rotation"] = not str(row.get("key_prefix") or "").startswith("s2g_live_")
    runtime = getattr(container.api_key_manager, "redis_runtime", None)
    if runtime is not None and runtime.ready:
        for row in keys:
            row["is_online"] = await runtime.is_present(int(row["id"]))
    return {"success": True, "keys": keys}


@router.post("/api/admin/managed-apikeys")
async def create_managed_api_key(
    request: CreateManagedApiKeyRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    if not container.api_key_manager:
        raise HTTPException(status_code=503, detail="API key manager not initialized")
    scopes_str = (request.scopes or "").strip()
    if not scopes_str:
        raise HTTPException(status_code=400, detail="scopes cannot be empty")
    try:
        created = await container.api_key_manager.create_api_key(
            client_name=request.client_name,
            label=request.label,
            scopes=scopes_str,
            account_ids=request.account_ids,
            endpoint_limits=request.endpoint_limits or {},
            expires_at=request.expires_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "message": "Managed API key created",
        "key": created,
        "warning": "Store api_key now. It is shown only once.",
    }


@router.put("/api/admin/managed-apikeys/{key_id}")
async def update_managed_api_key(
    key_id: int,
    request: UpdateManagedApiKeyRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    # Keep account mappings clean even when account_ids are not edited.
    await container.db.prune_stale_api_key_accounts(key_id)

    valid_account_ids = request.account_ids
    if request.account_ids is not None:
        cleaned_ids = sorted({int(x) for x in request.account_ids if int(x) > 0})
        if cleaned_ids:
            missing_ids = []
            for account_id in cleaned_ids:
                token_obj = await container.db.get_token(account_id)
                if token_obj is None:
                    missing_ids.append(account_id)
            if missing_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"Some account_ids do not exist: {missing_ids}",
                )
        valid_account_ids = cleaned_ids

    scopes_arg = request.scopes
    if scopes_arg is not None:
        scopes_arg = scopes_arg.strip()
        if not scopes_arg:
            raise HTTPException(status_code=400, detail="scopes cannot be empty when provided")

    await container.db.update_api_key(
        key_id,
        client_name=request.client_name,
        label=request.label,
        is_active=request.is_active,
        scopes=scopes_arg,
        expires_at=request.expires_at,
        account_ids=valid_account_ids,
        endpoint_limits=request.endpoint_limits,
    )
    if container.api_key_manager:
        await container.api_key_manager.invalidate(key_id)
    return {"success": True, "message": "Managed API key updated"}


@router.get("/api/admin/managed-apikeys/{key_id}/adobe-usage")
async def get_managed_api_key_adobe_usage(
    key_id: int,
    months: int = Query(12, ge=1, le=60),
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Monthly successful Adobe-suite request counts (from request_logs)."""
    detail = await container.db.get_api_key_detail(key_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Managed API key not found")
    months = int(months)
    stats = await container.db.get_adobe_usage_stats(key_id, months_back=months)
    return {
        "success": True,
        "key_id": key_id,
        "months": months,
        "by_month": stats.get("by_month") or [],
        "by_month_by_operation": stats.get("by_month_by_operation") or [],
    }


@router.get("/api/admin/storage/status")
async def get_storage_status(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Return sanitized persistent-volume usage and BrowserMetrics cleanup totals."""
    data_dir = get_runtime_data_dir()
    usage = await asyncio.to_thread(shutil.disk_usage, data_dir)
    return {
        "success": True,
        "storage": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
        },
        "browser_metrics_cleanup": get_last_browser_metrics_cleanup_stats(),
    }


@router.get("/api/admin/managed-apikeys/{key_id}/generation-stats")
async def get_managed_api_key_generation_stats(
    key_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Durable dashboard-style generation counters for one managed API key."""
    detail = await container.db.get_api_key_detail(key_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Managed API key not found")
    native_image_models = [
        model_id for model_id, model_config in MODEL_CONFIG.items() if model_config.get("type") == "image"
    ]
    native_video_models = [
        model_id for model_id, model_config in MODEL_CONFIG.items() if model_config.get("type") == "video"
    ]
    stats = await container.db.get_api_key_generation_stats(
        key_id,
        native_image_models=native_image_models,
        native_video_models=native_video_models,
    )
    return {"success": True, "key_id": key_id, **stats}


@router.get("/api/admin/managed-apikeys/audit")
async def list_managed_api_key_audit(
    key_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Must be registered before /managed-apikeys/{key_id} or 'audit' is parsed as key_id (422)."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    total = await container.db.count_api_key_audit_logs(key_id=key_id)
    logs = await container.db.list_api_key_audit_logs(limit=limit, offset=offset, key_id=key_id)
    return {"success": True, "logs": logs, "total": total, "limit": limit, "offset": offset}


@router.delete("/api/admin/managed-apikeys/audit")
async def clear_managed_api_key_audit(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Clear all managed API key audit logs."""
    deleted = await container.db.clear_api_key_audit_logs()
    return {"success": True, "message": "Managed API key audit logs cleared", "deleted": deleted}


@router.get("/api/admin/managed-apikeys/{key_id}/projects")
async def list_managed_api_key_projects(
    key_id: int,
    limit: int = 10,
    offset: int = 0,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Paginated VideoFX projects scoped to a managed API key + per-account current project cursors."""
    raise HTTPException(status_code=410, detail="Project management has been removed")
    detail = await container.db.get_api_key_detail(key_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Managed API key not found")
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    total = await container.db.count_projects_by_api_key(key_id)
    projects = await container.db.list_projects_by_api_key(key_id, limit=limit, offset=offset)
    projects_by_token: dict[int, list[Any]] = {}
    for p in projects:
        try:
            token_id = int(getattr(p, "token_id", 0) or 0)
        except Exception:
            token_id = 0
        if token_id <= 0:
            continue
        projects_by_token.setdefault(token_id, []).append(p)

    accounts_out = []
    active_by_token: dict[int, str] = {}
    for account_id in await container.db.get_api_key_account_ids(key_id):
        t = await container.token_manager.get_token(account_id)
        if t:
            token_scoped_projects = projects_by_token.get(int(t.id), [])
            active_project = next(
                (proj for proj in token_scoped_projects if bool(getattr(proj, "is_active", False))), None
            )
            active_pid = str(active_project.project_id) if active_project and active_project.project_id else None
            active_name = str(active_project.project_name) if active_project and active_project.project_name else None
            if active_pid:
                active_by_token[int(t.id)] = active_pid
            accounts_out.append(
                {
                    "token_id": t.id,
                    "email": t.email or None,
                    "active_project_id": active_pid,
                    "active_project_name": active_name,
                    "current_project_id": active_pid,
                    "current_project_name": active_name,
                }
            )
        else:
            accounts_out.append(
                {
                    "token_id": account_id,
                    "email": None,
                    "active_project_id": None,
                    "active_project_name": None,
                    "current_project_id": None,
                    "current_project_name": None,
                }
            )

    projects_out = []
    for p in projects:
        pd = _project_to_dict(p)
        tid_raw = pd.get("token_id")
        tid = int(tid_raw) if isinstance(tid_raw, int) else None
        is_current_for_token = bool(tid is not None and active_by_token.get(tid) == str(pd.get("project_id") or ""))
        pd["is_current_for_token"] = is_current_for_token
        pd["project_status"] = "active" if is_current_for_token else "old"
        projects_out.append(pd)

    return {
        "success": True,
        "projects": projects_out,
        "total": total,
        "limit": limit,
        "offset": offset,
        "accounts": accounts_out,
    }


@router.post("/api/admin/managed-apikeys/{key_id}/projects")
async def create_managed_api_key_project(
    key_id: int,
    request: dict,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Create a VideoFX project for a token assigned to this managed key; tags projects.api_key_id."""
    raise HTTPException(status_code=410, detail="Project management has been removed")
    detail = await container.db.get_api_key_detail(key_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Managed API key not found")

    raw_tid = request.get("token_id")
    if raw_tid is None:
        raise HTTPException(status_code=400, detail="token_id is required")
    try:
        token_id = int(raw_tid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="token_id must be an integer")
    allowed = set(await container.db.get_api_key_account_ids(key_id))
    if token_id not in allowed:
        raise HTTPException(status_code=400, detail="token_id is not assigned to this API key")

    t = await container.token_manager.get_token(token_id)
    if not t:
        raise HTTPException(status_code=404, detail="Token not found")

    title = request.get("title")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=400, detail="title must be a string")
    if isinstance(title, str):
        title = title.strip() or None
    set_as_current = request.get("set_as_current", True)
    if not isinstance(set_as_current, bool):
        raise HTTPException(status_code=400, detail="set_as_current must be a boolean")

    try:
        project = await container.token_manager.create_project_for_token(
            token_id,
            title=title,
            set_as_current=set_as_current,
            api_key_id=key_id,
        )
        updated = await container.token_manager.get_token(token_id) if set_as_current else None
        return {
            "success": True,
            "message": "Project created",
            "project": _project_to_dict(project),
            "token": (
                {
                    "id": updated.id,
                    "current_project_id": updated.current_project_id,
                    "current_project_name": updated.current_project_name,
                }
                if updated
                else None
            ),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Create project failed: {str(e)}")


@router.get("/api/admin/managed-apikeys/{key_id}")
async def get_managed_api_key(
    key_id: int,
    reveal_plaintext: bool = False,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    detail = await container.db.get_api_key_detail(key_id, include_plaintext=reveal_plaintext)
    if not detail:
        raise HTTPException(status_code=404, detail="Managed API key not found")
    return {"success": True, "key": detail}


@router.delete("/api/admin/managed-apikeys/{key_id}")
async def delete_managed_api_key(
    key_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    detail = await container.db.get_api_key_detail(key_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Managed API key not found")

    cache_rows = await container.db.list_cache_files_for_api_key_cleanup(key_id)
    cache_objects_deleted = 0
    if cache_rows:
        file_cache = getattr(container.generation_handler, "file_cache", None)
        if file_cache is None or getattr(file_cache, "backend", None) is None:
            raise HTTPException(status_code=503, detail="Cache storage is unavailable; managed API key was not deleted")
        try:
            for row in cache_rows:
                filename = Path(str(row.get("filename") or "")).name
                if filename and await file_cache.backend.delete(filename):
                    cache_objects_deleted += 1
        except Exception as exc:
            from ..core.logger import debug_logger

            debug_logger.log_error(
                f"Managed API key cache cleanup failed: key_id={key_id}, error_type={type(exc).__name__}"
            )
            raise HTTPException(
                status_code=503,
                detail="Cache storage cleanup failed; managed API key was not deleted",
            ) from exc

    result = await container.db.delete_api_key(key_id)
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail="Managed API key not found")
    if container.api_key_manager:
        await container.api_key_manager.invalidate(key_id)

    worker_sessions_terminated = 0
    try:
        from ..services.browser_captcha_extension import ExtensionCaptchaService

        extension_service = await ExtensionCaptchaService.get_instance(db=container.db)
        worker_sessions_terminated = await extension_service.kill_managed_api_key_sessions(key_id)
    except Exception as exc:
        from ..core.logger import debug_logger

        debug_logger.log_error(
            f"Managed API key worker cleanup failed: key_id={key_id}, error_type={type(exc).__name__}"
        )

    return {
        "success": True,
        "message": "Managed API key deleted",
        "key_id": key_id,
        "cache_objects_deleted": cache_objects_deleted,
        "worker_sessions_terminated": worker_sessions_terminated,
        **result,
    }


@router.get("/api/admin/captcha-worker-keys")
async def list_captcha_worker_keys(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    from ..services.browser_captcha_extension import ExtensionCaptchaService

    keys = await container.db.list_captcha_worker_keys()
    service = await ExtensionCaptchaService.get_instance(db=container.db)
    active_workers = await service.list_active_workers()
    captcha_sessions = [w for w in active_workers if w.get("captcha_worker_id") is not None]
    return {"success": True, "keys": keys, "sessions": captcha_sessions}


@router.post("/api/admin/captcha-worker-keys")
async def create_captcha_worker_key(
    request: CaptchaWorkerKeyCreateRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    raw_key = _generate_captcha_worker_key()
    key_id = await container.db.create_captcha_worker_key(
        key_prefix=_worker_key_prefix(raw_key),
        key_hash=_hash_worker_registration_key(raw_key),
        label=(request.label or "").strip(),
        key_plaintext=raw_key.strip(),
    )
    row = await container.db.get_captcha_worker_key(key_id)
    return {"success": True, "key": row, "captcha_worker_key": raw_key}


@router.patch("/api/admin/captcha-worker-keys/{key_id}")
async def update_captcha_worker_key(
    key_id: int,
    request: CaptchaWorkerKeyUpdateRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    existing = await container.db.get_captcha_worker_key(key_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Captcha worker key not found")
    fs = request.model_fields_set
    updates: Dict[str, Any] = {}
    if "label" in fs:
        updates["label"] = request.label
    if "is_active" in fs:
        updates["is_active"] = request.is_active
    await container.db.update_captcha_worker_key(key_id, **updates)
    updated = await container.db.get_captcha_worker_key(key_id)
    return {"success": True, "key": updated}


@router.delete("/api/admin/captcha-worker-keys/{key_id}")
async def delete_captcha_worker_key(
    key_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    existing = await container.db.get_captcha_worker_key(key_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Captcha worker key not found")
    await container.db.delete_captcha_worker_key(key_id)
    return {"success": True, "key_id": key_id}


@router.post("/api/admin/captcha-worker-keys/{key_id}/kill-sessions")
async def kill_captcha_worker_key_sessions(
    key_id: int,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    existing = await container.db.get_captcha_worker_key(key_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Captcha worker key not found")
    from ..services.browser_captcha_extension import ExtensionCaptchaService

    service = await ExtensionCaptchaService.get_instance(db=container.db)
    killed = await service.kill_captcha_worker_sessions_for_key(key_id)
    return {
        "success": True,
        "killed_count": killed,
        "message": f"Terminated {killed} active session(s)"
        if killed
        else "No active captcha worker sessions for this key",
    }


@router.get("/api/admin/extension/workers")
async def list_extension_workers(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    from ..services.browser_captcha_extension import ExtensionCaptchaService

    service = await ExtensionCaptchaService.get_instance(db=container.db)
    active_workers = await service.list_active_workers()
    bindings = await container.db.list_extension_worker_bindings()
    queue_stats = service.get_queue_stats()
    return {
        "success": True,
        "mode": "three_worker_types",
        "note": "Extension mode uses server-side captcha workers, managed-key end user captcha workers, and token-bound refresh workers.",
        "workers": active_workers,
        "bindings": bindings,
        "queue_stats": queue_stats,
    }


@router.post("/api/admin/extension/workers/bind")
async def bind_extension_worker(
    request: ExtensionWorkerBindRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    from ..services.browser_captcha_extension import ExtensionCaptchaService

    route_key = (request.route_key or "").strip()
    if not route_key:
        raise HTTPException(status_code=400, detail="route_key is required")
    detail = await container.db.get_api_key_detail(int(request.api_key_id))
    if not detail:
        raise HTTPException(status_code=404, detail="Managed API key not found")
    service = await ExtensionCaptchaService.get_instance(db=container.db)
    await service.bind_route_key(route_key, int(request.api_key_id))
    return {
        "success": True,
        "message": "Worker binding updated. Requests for this managed key now require this worker route.",
    }


@router.post("/api/admin/extension/workers/unbind")
async def unbind_extension_worker(
    request: ExtensionWorkerUnbindRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    from ..services.browser_captcha_extension import ExtensionCaptchaService

    route_key = (request.route_key or "").strip()
    if not route_key:
        raise HTTPException(status_code=400, detail="route_key is required")
    service = await ExtensionCaptchaService.get_instance(db=container.db)
    await service.unbind_route_key(route_key)
    return {
        "success": True,
        "message": "Worker binding removed. Requests for this route will fail until a matching worker binds again.",
    }


@router.post("/api/admin/extension/workers/kill")
async def kill_extension_worker(
    request: ExtensionWorkerKillRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    from ..services.browser_captcha_extension import ExtensionCaptchaService

    worker_session_id = (request.worker_session_id or "").strip()
    if not worker_session_id:
        raise HTTPException(status_code=400, detail="worker_session_id is required")
    service = await ExtensionCaptchaService.get_instance(db=container.db)
    killed = await service.kill_worker(worker_session_id)
    if not killed:
        return {"success": False, "message": "Worker not found"}
    return {"success": True, "message": "Worker terminated", "worker_session_id": worker_session_id}


@router.post("/api/admin/debug")
async def update_debug_config(
    request: UpdateDebugConfigRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update debug configuration"""
    try:
        # Persist to database so value survives restart/rebuild.
        await container.db.update_debug_config(enabled=request.enabled)
        # Hot reload updated value into runtime config.
        await container.db.reload_config_to_memory()

        actual_enabled = config.debug_enabled
        status = "enabled" if actual_enabled else "disabled"
        env_override_active = bool(actual_enabled != request.enabled)
        message = f"Debug mode {status}"
        if env_override_active:
            message += " (environment override active)"
        return {
            "success": True,
            "message": message,
            "enabled": actual_enabled,
            "env_override_active": env_override_active,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update debug config: {str(e)}")


@router.get("/api/generation/timeout")
async def get_generation_timeout(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get generation timeout configuration"""
    return await get_generation_config(token, container)


@router.post("/api/generation/timeout")
async def update_generation_timeout(
    request: GenerationConfigRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update generation timeout configuration"""
    captcha_cfg = await container.db.get_captcha_config()
    extension_mode_active = str(getattr(captcha_cfg, "captcha_method", "") or "").strip().lower() == "extension"
    extension_generation_enabled = bool(request.extension_generation_enabled) if extension_mode_active else False
    extension_generation_fallback_mode = request.extension_generation_fallback_mode if extension_mode_active else "none"
    await container.db.update_generation_config(
        image_timeout=request.image_timeout,
        video_timeout=request.video_timeout,
        max_retries=request.max_retries,
        extension_generation_enabled=extension_generation_enabled,
        extension_generation_fallback_mode=extension_generation_fallback_mode,
        sub2gen_gemini_api_keys=request.sub2gen_gemini_api_keys,
        sub2gen_openai_api_keys=request.sub2gen_openai_api_keys,
        sub2gen_openrouter_api_keys=request.sub2gen_openrouter_api_keys,
        sub2gen_third_party_gemini_api_keys=request.sub2gen_third_party_gemini_api_keys,
        sub2gen_third_party_gemini_base_url=request.sub2gen_third_party_gemini_base_url,
        cloudflare_account_id=request.cloudflare_account_id,
        cloudflare_api_token=request.cloudflare_api_token,
        sub2gen_csvgen_cookie=request.sub2gen_csvgen_cookie,
        sub2gen_csvgen_api_keys=request.sub2gen_csvgen_api_keys,
        sub2gen_cloning_model=request.sub2gen_cloning_model,
        sub2gen_cloning_backend=request.sub2gen_cloning_backend,
        sub2gen_cloning_provider_order=request.sub2gen_cloning_provider_order,
        sub2gen_cloning_enabled_providers=request.sub2gen_cloning_enabled_providers,
        sub2gen_cloning_provider_retry_count=request.sub2gen_cloning_provider_retry_count,
        sub2gen_cloning_gemini_api_keys=request.sub2gen_cloning_gemini_api_keys,
        sub2gen_cloning_openai_api_keys=request.sub2gen_cloning_openai_api_keys,
        sub2gen_cloning_openrouter_api_keys=request.sub2gen_cloning_openrouter_api_keys,
        sub2gen_cloning_third_party_gemini_api_keys=request.sub2gen_cloning_third_party_gemini_api_keys,
        sub2gen_cloning_third_party_gemini_base_url=request.sub2gen_cloning_third_party_gemini_base_url,
        sub2gen_cloning_cloudflare_account_id=request.sub2gen_cloning_cloudflare_account_id,
        sub2gen_cloning_cloudflare_api_token=request.sub2gen_cloning_cloudflare_api_token,
        sub2gen_metadata_backend=request.sub2gen_metadata_backend,
        sub2gen_metadata_provider_order=request.sub2gen_metadata_provider_order,
        sub2gen_metadata_enabled_providers=request.sub2gen_metadata_enabled_providers,
        sub2gen_metadata_provider_retry_count=request.sub2gen_metadata_provider_retry_count,
        sub2gen_metadata_model=request.sub2gen_metadata_model,
        sub2gen_metadata_enabled_models=request.sub2gen_metadata_enabled_models,
        sub2gen_metadata_primary_model=request.sub2gen_metadata_primary_model,
        sub2gen_metadata_fallback_models=request.sub2gen_metadata_fallback_models,
        metadata_system_prompt=request.metadata_system_prompt,
        cloning_image_system_prompt=request.cloning_image_system_prompt,
        cloning_video_system_prompt=request.cloning_video_system_prompt,
        task_tracker_device_id=request.task_tracker_device_id,
        task_tracker_device_name=request.task_tracker_device_name,
        task_tracker_cookies=request.task_tracker_cookies,
        task_tracker_device_token=request.task_tracker_device_token,
        task_tracker_turnstile_token=request.task_tracker_turnstile_token,
        task_tracker_tls_profile=request.task_tracker_tls_profile,
    )

    # 🔥 Hot reload: sync database config to memory
    await container.db.reload_config_to_memory()

    return {"success": True, "message": "生成配置更新成功"}


# ========== AT Auto Refresh Config ==========


@router.get("/api/token-refresh/config")
async def get_token_refresh_config(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get scheduled AT and protocol ST refresh configuration."""
    captcha_config = await container.db.get_captcha_config()
    protocol_config = await container.db.get_token_refresh_config()
    return {
        "success": True,
        "config": {
            "at_auto_refresh_enabled": bool(getattr(captcha_config, "session_refresh_scheduler_enabled", False)),
            "protocol_refresh_enabled": protocol_config.enabled,
            "refresh_interval_minutes": protocol_config.refresh_interval_minutes,
        },
    }


@router.post("/api/token-refresh/enabled")
async def update_token_refresh_enabled(
    request: dict,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update scheduled AT auto refresh enabled."""
    enabled = bool(request.get("enabled", False))
    await container.db.update_captcha_config(session_refresh_scheduler_enabled=enabled)
    await container.db.reload_config_to_memory()
    return {"success": True, "message": f"定时自动刷新已{'启用' if enabled else '禁用'}"}


@router.post("/api/token-refresh/config")
async def update_protocol_token_refresh_config(
    request: TokenRefreshConfigRequest,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update global protocol ST refresh settings."""
    updated = await container.db.update_token_refresh_config(
        enabled=request.enabled,
        refresh_interval_minutes=request.refresh_interval_minutes,
    )
    return {
        "success": True,
        "config": {
            "protocol_refresh_enabled": updated.enabled,
            "refresh_interval_minutes": updated.refresh_interval_minutes,
        },
    }


async def _sync_runtime_cache_config(handler: Optional[GenerationHandler]):
    if handler and handler.file_cache:
        file_cache = handler.file_cache
        file_cache.set_timeout(config.cache_timeout)
        await file_cache.configure_backend(
            config.cache_provider,
            config.cache_delivery_mode,
            validate=config.cache_provider == "digitalocean",
        )
        await file_cache.refresh_cleanup_task()


# ========== Cache Configuration Endpoints ==========


@router.get("/api/cache/config")
async def get_cache_config(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get cache configuration"""
    cache_config = await container.db.get_cache_config()
    cache_base_url = config.cache_base_url
    from . import routes

    file_cache = container.generation_handler.file_cache if container.generation_handler else None
    do_env = file_cache.digitalocean_environment(cache_config.cache_delivery_mode) if file_cache else {}

    # Calculate effective base URL
    effective_base_url = cache_base_url if cache_base_url else f"http://127.0.0.1:8000"

    ct = cache_config.cache_timeout or 0
    timeout_days = 0.0 if ct <= 0 else round(ct / 86400.0, 4)

    return {
        "success": True,
        "config": {
            "enabled": cache_config.cache_enabled,
            "timeout": cache_config.cache_timeout,
            "timeout_days": timeout_days,
            "base_url": cache_base_url or "",
            "effective_base_url": effective_base_url
            if cache_config.cache_delivery_mode != "cdn"
            else do_env.get("cdn_base_url", ""),
            "provider": cache_config.cache_provider,
            "delivery_mode": cache_config.cache_delivery_mode,
            "digitalocean": {
                "configured": bool(do_env.get("configured")),
                "healthy": bool(
                    cache_config.cache_provider == "local"
                    or (
                        file_cache is not None
                        and getattr(file_cache, "provider", "") == "digitalocean"
                        and bool(do_env.get("configured"))
                    )
                ),
                "missing": do_env.get("missing", []),
                "region": do_env.get("region", ""),
                "bucket": do_env.get("bucket", ""),
                "prefix": do_env.get("prefix", "sub2gen/cache"),
                "cdn_base_url": do_env.get("cdn_base_url", ""),
                "access_key_configured": bool(do_env.get("access_key_configured")),
                "api_token_configured": bool(do_env.get("api_token_configured")),
                "cdn_endpoint_configured": bool(do_env.get("cdn_endpoint_configured")),
            },
        },
    }


@router.get("/api/cache/stats")
async def get_cache_stats(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Disk usage for the file cache directory."""
    from . import routes

    if not container.generation_handler or not container.generation_handler.file_cache:
        raise HTTPException(status_code=503, detail="File cache not initialized")
    stats = await container.generation_handler.file_cache.get_stats()
    return {"success": True, **stats}


@router.get("/api/cache/files")
async def list_cache_files(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """List cached files for admin gallery (names, sizes, image/video/other)."""
    from . import routes

    if not container.generation_handler or not container.generation_handler.file_cache:
        raise HTTPException(status_code=503, detail="File cache not initialized")
    files = await container.generation_handler.file_cache.list_files()
    return {"success": True, "files": files}


@router.get("/api/cache/admin/file/{filename}")
async def get_cache_file_admin_preview(
    filename: str,
    request: Request,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Stream a cache file for the admin UI (Bearer auth). Plain <img src> cannot use managed-key /api/cache/blob/… URLs."""
    from . import routes

    if not container.generation_handler or not container.generation_handler.file_cache:
        raise HTTPException(status_code=503, detail="File cache not initialized")
    safe_name = Path(filename).name
    try:
        cached = await container.generation_handler.file_cache.open_cached(safe_name, request.headers.get("range"))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Cache file not found")
    except ValueError as exc:
        raise HTTPException(status_code=416, detail=str(exc))
    except Exception as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            raise HTTPException(status_code=404, detail="Cache file not found")
        raise
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(cached.content_length)}
    if cached.content_range:
        headers["Content-Range"] = cached.content_range
    if cached.etag:
        headers["ETag"] = cached.etag
    return StreamingResponse(
        cached.body,
        status_code=cached.status_code,
        media_type=cached.content_type,
        headers=headers,
    )


@router.post("/api/cache/clear")
async def clear_cache_files(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Delete all files in the cache directory (admin only)."""
    from . import routes

    if not container.generation_handler or not container.generation_handler.file_cache:
        raise HTTPException(status_code=503, detail="File cache not initialized")
    try:
        file_cache = container.generation_handler.file_cache
        removed_count, removed_bytes = await file_cache.backend.clear()
        if file_cache.db is not None and hasattr(file_cache.db, "delete_all_cache_file_metadata"):
            await file_cache.db.delete_all_cache_file_metadata()
        return {
            "success": True,
            "message": "Cache cleared",
            "removed_count": removed_count,
            "removed_bytes": removed_bytes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/cache/enabled")
async def update_cache_enabled(
    request: dict,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update cache enabled status"""
    enabled = request.get("enabled", False)
    await container.db.update_cache_config(enabled=enabled)

    # 🔥 Hot reload: sync database config to memory
    await container.db.reload_config_to_memory()
    await _sync_runtime_cache_config(container.generation_handler)

    return {"success": True, "message": f"缓存已{'启用' if enabled else '禁用'}"}


@router.post("/api/cache/config")
async def update_cache_config_full(
    request: dict,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update complete cache configuration"""
    enabled = request.get("enabled")
    timeout = request.get("timeout")
    base_url = request.get("base_url")
    provider = request.get("provider")
    delivery_mode = request.get("delivery_mode")

    if provider is not None:
        provider = str(provider).strip().lower()
        if provider not in {"local", "digitalocean"}:
            raise HTTPException(status_code=400, detail="Cache provider must be local or digitalocean")
    if delivery_mode is not None:
        delivery_mode = str(delivery_mode).strip().lower()
        if delivery_mode not in {"proxy", "cdn"}:
            raise HTTPException(status_code=400, detail="Delivery mode must be proxy or cdn")
    if provider == "local":
        delivery_mode = "proxy"

    if timeout is not None:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="缓存超时时间必须为整数")
        if timeout < 0:
            raise HTTPException(status_code=400, detail="缓存超时时间不能小于 0")
        max_cache_seconds = 7 * 86400
        if timeout > max_cache_seconds:
            raise HTTPException(
                status_code=400,
                detail=f"Cache timeout cannot exceed 7 days ({max_cache_seconds} seconds)",
            )

    from . import routes

    file_cache = container.generation_handler.file_cache if container.generation_handler else None
    current = await container.db.get_cache_config()
    target_provider = provider or current.cache_provider
    target_mode = delivery_mode or current.cache_delivery_mode
    changed_backend = target_provider != current.cache_provider or target_mode != current.cache_delivery_mode
    if file_cache and changed_backend:
        stats = await file_cache.get_stats()
        if int(stats.get("file_count", 0)) > 0:
            raise HTTPException(
                status_code=409, detail="Clear the current cache before changing provider or delivery mode"
            )
    if file_cache and target_provider == "digitalocean":
        try:
            await file_cache.validate_backend(target_provider, target_mode)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"DigitalOcean validation failed: {exc}")

    await container.db.update_cache_config(
        enabled=enabled,
        timeout=timeout,
        base_url=base_url,
        provider=provider,
        delivery_mode=delivery_mode,
    )

    # 🔥 Hot reload: sync database config to memory
    await container.db.reload_config_to_memory()
    await _sync_runtime_cache_config(container.generation_handler)

    return {"success": True, "message": "缓存配置更新成功"}


@router.post("/api/cache/provider/test")
async def test_cache_provider(
    request: dict,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    from . import routes

    if not container.generation_handler or not container.generation_handler.file_cache:
        raise HTTPException(status_code=503, detail="File cache not initialized")
    provider = str(request.get("provider") or "digitalocean").strip().lower()
    delivery_mode = str(request.get("delivery_mode") or "proxy").strip().lower()
    try:
        status = await container.generation_handler.file_cache.validate_backend(provider, delivery_mode)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Provider validation failed: {exc}")
    return {"success": True, "status": status}


@router.post("/api/cache/base-url")
async def update_cache_base_url(
    request: dict,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update cache base URL"""
    base_url = request.get("base_url", "")
    await container.db.update_cache_config(base_url=base_url)

    # 🔥 Hot reload: sync database config to memory
    await container.db.reload_config_to_memory()
    await _sync_runtime_cache_config(container.generation_handler)

    return {"success": True, "message": "缓存Base URL更新成功"}


@router.post("/api/captcha/config")
async def update_captcha_config(
    request: dict,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update captcha configuration"""
    from ..services.browser_captcha import validate_browser_proxy_url

    captcha_method = request.get("captcha_method")
    yescaptcha_api_key = request.get("yescaptcha_api_key")
    yescaptcha_base_url = request.get("yescaptcha_base_url")
    yescaptcha_task_type = normalize_yescaptcha_task_type(request.get("yescaptcha_task_type"))
    capmonster_api_key = request.get("capmonster_api_key")
    capmonster_base_url = request.get("capmonster_base_url")
    capmonster_min_score = request.get("capmonster_min_score")
    ezcaptcha_api_key = request.get("ezcaptcha_api_key")
    ezcaptcha_base_url = request.get("ezcaptcha_base_url")
    capsolver_api_key = request.get("capsolver_api_key")
    capsolver_base_url = request.get("capsolver_base_url")
    remote_browser_base_url = request.get("remote_browser_base_url")
    remote_browser_api_key = request.get("remote_browser_api_key")
    remote_browser_timeout = request.get("remote_browser_timeout", 60)
    browser_fallback_to_remote_browser = request.get("browser_fallback_to_remote_browser", True)
    browser_proxy_enabled = request.get("browser_proxy_enabled", False)
    browser_proxy_url = request.get("browser_proxy_url", "")
    browser_count = request.get("browser_count", 1)
    browser_personal_fresh_restart_every_n_solves = request.get(
        "browser_personal_fresh_restart_every_n_solves",
        10,
    )
    personal_project_pool_size = request.get("personal_project_pool_size")
    personal_max_resident_tabs = request.get("personal_max_resident_tabs")
    personal_idle_tab_ttl_seconds = request.get("personal_idle_tab_ttl_seconds")
    browser_captcha_page_url = request.get("browser_captcha_page_url")
    session_refresh_enabled = request.get("session_refresh_enabled")
    session_refresh_browser_first = request.get("session_refresh_browser_first")
    session_refresh_inject_st_cookie = request.get("session_refresh_inject_st_cookie")
    session_refresh_warmup_urls = request.get("session_refresh_warmup_urls")
    session_refresh_wait_seconds_per_url = request.get("session_refresh_wait_seconds_per_url")
    session_refresh_overall_timeout_seconds = request.get("session_refresh_overall_timeout_seconds")
    session_refresh_update_st_from_cookie = request.get("session_refresh_update_st_from_cookie")
    session_refresh_fail_if_st_refresh_fails = request.get("session_refresh_fail_if_st_refresh_fails")
    session_refresh_local_only = request.get("session_refresh_local_only")
    session_refresh_scheduler_enabled = request.get("session_refresh_scheduler_enabled")
    session_refresh_scheduler_interval_minutes = request.get("session_refresh_scheduler_interval_minutes")
    session_refresh_scheduler_batch_size = request.get("session_refresh_scheduler_batch_size")
    session_refresh_scheduler_only_expiring_within_minutes = request.get(
        "session_refresh_scheduler_only_expiring_within_minutes"
    )
    st_only_refresh_scheduler_enabled = request.get("st_only_refresh_scheduler_enabled")
    st_only_refresh_scheduler_interval_minutes = request.get("st_only_refresh_scheduler_interval_minutes")
    st_only_refresh_scheduler_batch_size = request.get("st_only_refresh_scheduler_batch_size")
    st_only_refresh_scheduler_expiring_within_minutes = request.get("st_only_refresh_scheduler_expiring_within_minutes")
    extension_queue_wait_timeout_seconds = request.get("extension_queue_wait_timeout_seconds")
    dedicated_extension_enabled = request.get("dedicated_extension_enabled")
    dedicated_extension_captcha_timeout_seconds = request.get("dedicated_extension_captcha_timeout_seconds")
    dedicated_extension_st_refresh_timeout_seconds = request.get("dedicated_extension_st_refresh_timeout_seconds")
    extension_fallback_to_managed_on_dedicated_failure = request.get(
        "extension_fallback_to_managed_on_dedicated_failure"
    )

    # 验证浏览器打码页面 URL
    if browser_captcha_page_url is not None:
        raw_page = str(browser_captcha_page_url).strip()
        if raw_page:
            from urllib.parse import urlparse

            parsed = urlparse(raw_page)
            if parsed.scheme not in ("http", "https"):
                return {"success": False, "message": "打码页面地址必须以 http:// 或 https:// 开头"}
            if not (parsed.netloc or "").strip():
                return {"success": False, "message": "打码页面地址无效"}

    # 验证浏览器代理URL格式
    if browser_proxy_enabled and browser_proxy_url:
        is_valid, error_msg = validate_browser_proxy_url(browser_proxy_url)
        if not is_valid:
            return {"success": False, "message": error_msg}

    if remote_browser_base_url:
        try:
            remote_browser_base_url = _normalize_http_base_url(remote_browser_base_url)
        except RuntimeError as e:
            return {"success": False, "message": str(e)}

    try:
        remote_browser_timeout = max(5, int(remote_browser_timeout or 60))
    except Exception:
        return {"success": False, "message": "远程打码超时时间必须是整数秒"}
    browser_fallback_to_remote_browser = bool(browser_fallback_to_remote_browser)
    try:
        browser_count = max(1, min(20, int(browser_count or 1)))
    except Exception:
        return {"success": False, "message": "browser_count must be an integer between 1 and 20"}
    if capmonster_min_score is not None:
        try:
            capmonster_min_score = float(capmonster_min_score)
        except (TypeError, ValueError):
            return {"success": False, "message": "CapMonster minimum score must be a number between 0.1 and 0.9"}
        if capmonster_min_score < 0.1 or capmonster_min_score > 0.9:
            return {"success": False, "message": "CapMonster minimum score must be between 0.1 and 0.9"}
    try:
        browser_personal_fresh_restart_every_n_solves = max(
            0,
            int(browser_personal_fresh_restart_every_n_solves),
        )
    except Exception:
        return {
            "success": False,
            "message": "browser_personal_fresh_restart_every_n_solves must be an integer >= 0",
        }
    if extension_queue_wait_timeout_seconds is not None:
        try:
            extension_queue_wait_timeout_seconds = int(extension_queue_wait_timeout_seconds)
        except Exception:
            return {"success": False, "message": "extension_queue_wait_timeout_seconds must be an integer"}
        if extension_queue_wait_timeout_seconds < 1 or extension_queue_wait_timeout_seconds > 120:
            return {"success": False, "message": "extension_queue_wait_timeout_seconds must be between 1 and 120"}
    if dedicated_extension_captcha_timeout_seconds is not None:
        try:
            dedicated_extension_captcha_timeout_seconds = int(dedicated_extension_captcha_timeout_seconds)
        except Exception:
            return {"success": False, "message": "dedicated_extension_captcha_timeout_seconds must be an integer"}
        if dedicated_extension_captcha_timeout_seconds < 5 or dedicated_extension_captcha_timeout_seconds > 180:
            return {
                "success": False,
                "message": "dedicated_extension_captcha_timeout_seconds must be between 5 and 180",
            }
    if dedicated_extension_st_refresh_timeout_seconds is not None:
        try:
            dedicated_extension_st_refresh_timeout_seconds = int(dedicated_extension_st_refresh_timeout_seconds)
        except Exception:
            return {"success": False, "message": "dedicated_extension_st_refresh_timeout_seconds must be an integer"}
        if dedicated_extension_st_refresh_timeout_seconds < 10 or dedicated_extension_st_refresh_timeout_seconds > 300:
            return {
                "success": False,
                "message": "dedicated_extension_st_refresh_timeout_seconds must be between 10 and 300",
            }

    if captcha_method == "remote_browser":
        if not (remote_browser_base_url or "").strip():
            return {"success": False, "message": "remote_browser 模式需要配置远程打码服务地址"}
        if not (remote_browser_api_key or "").strip():
            return {"success": False, "message": "remote_browser 模式需要配置远程打码服务 API Key"}

    await container.db.update_captcha_config(
        captcha_method=captcha_method,
        yescaptcha_api_key=yescaptcha_api_key,
        yescaptcha_base_url=yescaptcha_base_url,
        yescaptcha_task_type=yescaptcha_task_type,
        capmonster_api_key=capmonster_api_key,
        capmonster_base_url=capmonster_base_url,
        capmonster_min_score=capmonster_min_score,
        ezcaptcha_api_key=ezcaptcha_api_key,
        ezcaptcha_base_url=ezcaptcha_base_url,
        capsolver_api_key=capsolver_api_key,
        capsolver_base_url=capsolver_base_url,
        remote_browser_base_url=remote_browser_base_url,
        remote_browser_api_key=remote_browser_api_key,
        remote_browser_timeout=remote_browser_timeout,
        browser_fallback_to_remote_browser=browser_fallback_to_remote_browser,
        browser_proxy_enabled=browser_proxy_enabled,
        browser_proxy_url=browser_proxy_url if browser_proxy_enabled else None,
        browser_count=browser_count,
        browser_personal_fresh_restart_every_n_solves=browser_personal_fresh_restart_every_n_solves,
        personal_project_pool_size=personal_project_pool_size,
        personal_max_resident_tabs=personal_max_resident_tabs,
        personal_idle_tab_ttl_seconds=personal_idle_tab_ttl_seconds,
        browser_captcha_page_url=browser_captcha_page_url,
        session_refresh_enabled=session_refresh_enabled,
        session_refresh_browser_first=session_refresh_browser_first,
        session_refresh_inject_st_cookie=session_refresh_inject_st_cookie,
        session_refresh_warmup_urls=(
            ",".join(str(item).strip() for item in session_refresh_warmup_urls if str(item).strip())
            if isinstance(session_refresh_warmup_urls, list)
            else session_refresh_warmup_urls
        ),
        session_refresh_wait_seconds_per_url=session_refresh_wait_seconds_per_url,
        session_refresh_overall_timeout_seconds=session_refresh_overall_timeout_seconds,
        session_refresh_update_st_from_cookie=session_refresh_update_st_from_cookie,
        session_refresh_fail_if_st_refresh_fails=session_refresh_fail_if_st_refresh_fails,
        session_refresh_local_only=session_refresh_local_only,
        session_refresh_scheduler_enabled=session_refresh_scheduler_enabled,
        session_refresh_scheduler_interval_minutes=session_refresh_scheduler_interval_minutes,
        session_refresh_scheduler_batch_size=session_refresh_scheduler_batch_size,
        session_refresh_scheduler_only_expiring_within_minutes=session_refresh_scheduler_only_expiring_within_minutes,
        st_only_refresh_scheduler_enabled=st_only_refresh_scheduler_enabled,
        st_only_refresh_scheduler_interval_minutes=st_only_refresh_scheduler_interval_minutes,
        st_only_refresh_scheduler_batch_size=st_only_refresh_scheduler_batch_size,
        st_only_refresh_scheduler_expiring_within_minutes=st_only_refresh_scheduler_expiring_within_minutes,
        extension_queue_wait_timeout_seconds=extension_queue_wait_timeout_seconds,
        dedicated_extension_enabled=dedicated_extension_enabled,
        dedicated_extension_captcha_timeout_seconds=dedicated_extension_captcha_timeout_seconds,
        dedicated_extension_st_refresh_timeout_seconds=dedicated_extension_st_refresh_timeout_seconds,
        extension_fallback_to_managed_on_dedicated_failure=extension_fallback_to_managed_on_dedicated_failure,
    )

    # 🔥 Hot reload: sync database config to memory
    await container.db.reload_config_to_memory()

    # 如果使用 browser 打码，热重载浏览器数量配置
    if captcha_method in {"browser", "personal"}:
        runtime_prepare_started = _schedule_captcha_runtime_prepare(captcha_method, container)
        return {
            "success": True,
            "message": "Captcha configuration updated successfully",
            "runtime_prepare_started": runtime_prepare_started,
            "runtime_status_method": captcha_method,
        }

    return {"success": True, "message": "验证码配置更新成功"}


@router.get("/api/captcha/runtime-status")
async def get_captcha_runtime_status(
    method: str = "browser",
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    runtime_method = _normalize_runtime_method(method)
    status = get_runtime_status(runtime_method)
    status["method"] = runtime_method
    status["task_running"] = container.tasks.is_running(f"captcha-runtime-{runtime_method}")
    return status


@router.get("/api/captcha/config")
async def get_captcha_config(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get captcha configuration"""
    captcha_config = await container.db.get_captcha_config()
    return {
        "captcha_method": captcha_config.captcha_method,
        "yescaptcha_api_key": captcha_config.yescaptcha_api_key,
        "yescaptcha_base_url": captcha_config.yescaptcha_base_url,
        "yescaptcha_task_type": captcha_config.yescaptcha_task_type,
        "capmonster_api_key": captcha_config.capmonster_api_key,
        "capmonster_base_url": captcha_config.capmonster_base_url,
        "capmonster_min_score": captcha_config.capmonster_min_score,
        "ezcaptcha_api_key": captcha_config.ezcaptcha_api_key,
        "ezcaptcha_base_url": captcha_config.ezcaptcha_base_url,
        "capsolver_api_key": captcha_config.capsolver_api_key,
        "capsolver_base_url": captcha_config.capsolver_base_url,
        "remote_browser_base_url": captcha_config.remote_browser_base_url,
        "remote_browser_api_key": captcha_config.remote_browser_api_key,
        "remote_browser_timeout": captcha_config.remote_browser_timeout,
        "browser_fallback_to_remote_browser": bool(getattr(captcha_config, "browser_fallback_to_remote_browser", True)),
        "browser_proxy_enabled": captcha_config.browser_proxy_enabled,
        "browser_proxy_url": captcha_config.browser_proxy_url or "",
        "browser_count": captcha_config.browser_count,
        "browser_personal_fresh_restart_every_n_solves": int(
            getattr(captcha_config, "browser_personal_fresh_restart_every_n_solves", 10) or 10
        ),
        "personal_project_pool_size": captcha_config.personal_project_pool_size,
        "personal_max_resident_tabs": captcha_config.personal_max_resident_tabs,
        "personal_idle_tab_ttl_seconds": captcha_config.personal_idle_tab_ttl_seconds,
        "browser_captcha_page_url": (
            (getattr(captcha_config, "browser_captcha_page_url", None) or "").strip()
            or "https://labs.google/fx/api/auth/providers"
        ),
        "session_refresh_enabled": bool(getattr(captcha_config, "session_refresh_enabled", True)),
        "session_refresh_browser_first": bool(getattr(captcha_config, "session_refresh_browser_first", True)),
        "session_refresh_inject_st_cookie": bool(getattr(captcha_config, "session_refresh_inject_st_cookie", True)),
        "session_refresh_warmup_urls": [
            item.strip()
            for item in str(
                getattr(
                    captcha_config,
                    "session_refresh_warmup_urls",
                    "https://labs.google/fx/tools/flow,https://labs.google/fx",
                )
                or ""
            ).split(",")
            if item.strip()
        ],
        "session_refresh_wait_seconds_per_url": int(
            getattr(captcha_config, "session_refresh_wait_seconds_per_url", 60) or 60
        ),
        "session_refresh_overall_timeout_seconds": int(
            getattr(captcha_config, "session_refresh_overall_timeout_seconds", 180) or 180
        ),
        "session_refresh_update_st_from_cookie": bool(
            getattr(captcha_config, "session_refresh_update_st_from_cookie", True)
        ),
        "session_refresh_fail_if_st_refresh_fails": bool(
            getattr(captcha_config, "session_refresh_fail_if_st_refresh_fails", True)
        ),
        "session_refresh_local_only": bool(getattr(captcha_config, "session_refresh_local_only", True)),
        "session_refresh_scheduler_enabled": bool(getattr(captcha_config, "session_refresh_scheduler_enabled", False)),
        "session_refresh_scheduler_interval_minutes": int(
            getattr(captcha_config, "session_refresh_scheduler_interval_minutes", 30) or 30
        ),
        "session_refresh_scheduler_batch_size": int(
            getattr(captcha_config, "session_refresh_scheduler_batch_size", 10) or 10
        ),
        "session_refresh_scheduler_only_expiring_within_minutes": int(
            getattr(captcha_config, "session_refresh_scheduler_only_expiring_within_minutes", 60) or 60
        ),
        "st_only_refresh_scheduler_enabled": bool(getattr(captcha_config, "st_only_refresh_scheduler_enabled", False)),
        "st_only_refresh_scheduler_interval_minutes": int(
            getattr(captcha_config, "st_only_refresh_scheduler_interval_minutes", 5) or 5
        ),
        "st_only_refresh_scheduler_batch_size": int(
            getattr(captcha_config, "st_only_refresh_scheduler_batch_size", 20) or 20
        ),
        "st_only_refresh_scheduler_expiring_within_minutes": int(
            getattr(captcha_config, "st_only_refresh_scheduler_expiring_within_minutes", 5) or 5
        ),
        "dedicated_extension_enabled": bool(getattr(captcha_config, "dedicated_extension_enabled", False)),
        "dedicated_extension_captcha_timeout_seconds": int(
            getattr(captcha_config, "dedicated_extension_captcha_timeout_seconds", 25) or 25
        ),
        "dedicated_extension_st_refresh_timeout_seconds": int(
            getattr(captcha_config, "dedicated_extension_st_refresh_timeout_seconds", 45) or 45
        ),
        "extension_queue_wait_timeout_seconds": int(
            getattr(captcha_config, "extension_queue_wait_timeout_seconds", 20) or 20
        ),
        "extension_fallback_to_managed_on_dedicated_failure": bool(
            getattr(captcha_config, "extension_fallback_to_managed_on_dedicated_failure", False)
        ),
    }


@router.get("/api/agent-gateway/mode")
async def get_agent_gateway_mode(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Probe configured agent-gateway mode via remote_browser_base_url health."""
    captcha_config = await container.db.get_captcha_config()
    base_url = (getattr(captcha_config, "remote_browser_base_url", "") or "").strip()
    if not base_url:
        return {
            "success": False,
            "status": "not_configured",
            "message": "remote_browser_base_url is not configured",
            "agent_auth_mode": "unknown",
            "keygen_verify_mode": "",
            "gateway_reachable": False,
            "base_url": "",
        }
    try:
        probe = await _probe_agent_gateway_mode(base_url)
    except Exception as e:
        return {
            "success": False,
            "status": "unreachable",
            "message": f"agent-gateway health probe failed: {e}",
            "agent_auth_mode": "unknown",
            "keygen_verify_mode": "",
            "gateway_reachable": False,
            "base_url": base_url,
        }

    mode = probe.get("agent_auth_mode") or "legacy"
    verify_mode = probe.get("keygen_verify_mode") or ""
    return {
        "success": True,
        "status": "reachable",
        "message": "agent-gateway reachable",
        "agent_auth_mode": mode,
        "keygen_verify_mode": verify_mode,
        "gateway_reachable": bool(probe.get("ok")),
        "base_url": base_url,
        "service": probe.get("service") or "",
    }


@router.get("/api/agent-gateway/connections")
async def get_agent_gateway_connections(
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Fetch currently connected agent sessions from configured gateway."""
    captcha_config = await container.db.get_captcha_config()
    base_url = (getattr(captcha_config, "remote_browser_base_url", "") or "").strip()
    api_key = (getattr(captcha_config, "remote_browser_api_key", "") or "").strip()
    if not base_url:
        return {
            "success": False,
            "status": "not_configured",
            "message": "remote_browser_base_url is not configured",
            "connections": [],
            "count": 0,
            "base_url": "",
        }
    if not api_key:
        return {
            "success": False,
            "status": "missing_api_key",
            "message": "remote_browser_api_key is not configured",
            "connections": [],
            "count": 0,
            "base_url": base_url,
        }
    try:
        payload = await _fetch_agent_gateway_connections(base_url, api_key)
    except Exception as e:
        return {
            "success": False,
            "status": "unreachable",
            "message": f"agent-gateway connections probe failed: {e}",
            "connections": [],
            "count": 0,
            "base_url": base_url,
        }

    raw_agents = payload.get("agents")
    connections = raw_agents if isinstance(raw_agents, list) else []
    return {
        "success": True,
        "status": "reachable",
        "message": "agent-gateway connections loaded",
        "base_url": base_url,
        "count": int(payload.get("count") or len(connections)),
        "connections": connections,
    }


@router.post("/api/captcha/score-test")
async def test_captcha_score(
    _request: Optional[CaptchaScoreTestRequest] = None,
    _token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """分数测试已禁用。"""
    raise HTTPException(status_code=403, detail="已禁用分数测试")


# ========== Plugin Configuration Endpoints ==========


async def _verify_plugin_connection_token(authorization: Optional[str], database: Database) -> None:
    plugin_config = await database.get_plugin_config()
    provided_token = authorization[7:] if authorization and authorization.startswith("Bearer ") else authorization
    if not plugin_config.connection_token or not secrets.compare_digest(
        str(provided_token or ""),
        str(plugin_config.connection_token),
    ):
        raise HTTPException(status_code=401, detail="Invalid connection token")


@router.get("/api/plugin/config")
async def get_plugin_config(
    request: Request,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Get plugin configuration"""
    plugin_config = await container.db.get_plugin_config()

    # Get the actual domain and port from the request
    # This allows the connection URL to reflect the user's actual access path
    host_header = request.headers.get("host", "")

    # Generate connection URL based on actual request
    if host_header:
        # Use the actual domain/IP and port from the request
        connection_url = f"http://{host_header}/api/plugin/update-token"
    else:
        # Fallback to config-based URL
        from ..core.config import config

        server_host = config.server_host
        server_port = config.server_port

        if server_host == "0.0.0.0":
            connection_url = f"http://127.0.0.1:{server_port}/api/plugin/update-token"
        else:
            connection_url = f"http://{server_host}:{server_port}/api/plugin/update-token"

    return {
        "success": True,
        "config": {
            "connection_token": plugin_config.connection_token,
            "connection_url": connection_url,
            "auto_enable_on_update": plugin_config.auto_enable_on_update,
        },
    }


@router.post("/api/plugin/config")
async def update_plugin_config(
    request: dict,
    token: str = Depends(verify_admin_token),
    container: AppContainer = Depends(get_container),
):
    """Update plugin configuration"""
    connection_token = request.get("connection_token", "")
    auto_enable_on_update = request.get("auto_enable_on_update", True)  # 默认开启

    # Generate random token if empty
    if not connection_token:
        connection_token = secrets.token_urlsafe(32)

    await container.db.update_plugin_config(
        connection_token=connection_token, auto_enable_on_update=auto_enable_on_update
    )

    return {
        "success": True,
        "message": "插件配置更新成功",
        "connection_token": connection_token,
        "auto_enable_on_update": auto_enable_on_update,
    }


@router.post("/api/plugin/update-token")
async def plugin_update_token(
    request: dict,
    authorization: Optional[str] = Header(None),
    container: AppContainer = Depends(get_container),
):
    """Receive token update from Chrome extension (no admin auth required, uses connection_token)"""
    await _verify_plugin_connection_token(authorization, container.db)
    plugin_config = await container.db.get_plugin_config()

    # Extract session token from request
    session_token = request.get("session_token")

    if not session_token:
        raise HTTPException(status_code=400, detail="Missing session_token")

    # Step 1: Convert ST to AT to get user info (including email)
    try:
        result = await container.token_manager.flow_client.st_to_at(session_token)
        at = result["access_token"]
        expires = result.get("expires")
        user_info = result.get("user", {})
        email = user_info.get("email", "")

        if not email:
            raise HTTPException(status_code=400, detail="Failed to get email from session token")

        # Parse expiration time
        from datetime import datetime

        at_expires = None
        if expires:
            try:
                at_expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except:
                pass

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid session token: {str(e)}")

    # Step 2: Check if token with this email exists
    existing_token = await container.db.get_token_by_email(email)

    if existing_token:
        # Update existing token
        try:
            # Update token
            await container.token_manager.update_token(
                token_id=existing_token.id,
                st=session_token,
                at=at,
                at_expires=at_expires,
                protocol_mode=request.get("protocol_mode"),
                google_cookies=request.get("google_cookies"),
                login_account=request.get("login_account"),
                login_password=request.get("login_password"),
                proxy_url=request.get("proxy_url"),
                auto_refresh_enabled=request.get("auto_refresh_enabled"),
                refresh_interval_minutes=request.get("refresh_interval_minutes"),
            )

            # Check if auto-enable is enabled and token is disabled
            if plugin_config.auto_enable_on_update and not existing_token.is_active:
                await container.token_manager.enable_token(existing_token.id)
                return {
                    "success": True,
                    "message": f"Token updated and auto-enabled for {email}",
                    "action": "updated",
                    "auto_enabled": True,
                }

            return {"success": True, "message": f"Token updated for {email}", "action": "updated"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update token: {str(e)}")
    else:
        # Add new token
        try:
            new_token = await container.token_manager.add_token(
                st=session_token,
                remark="Added by Chrome Extension",
                protocol_mode=request.get("protocol_mode", "session"),
                google_cookies=request.get("google_cookies"),
                login_account=request.get("login_account"),
                login_password=request.get("login_password"),
                proxy_url=request.get("proxy_url"),
                auto_refresh_enabled=request.get("auto_refresh_enabled", True),
                refresh_interval_minutes=request.get("refresh_interval_minutes", 120),
            )

            return {
                "success": True,
                "message": f"Token added for {new_token.email}",
                "action": "added",
                "token_id": new_token.id,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to add token: {str(e)}")


@router.post("/api/plugin/check-tokens")
async def plugin_check_tokens(
    request: Optional[dict] = None,
    authorization: Optional[str] = Header(None),
    container: AppContainer = Depends(get_container),
):
    """Return token status for external syncers using the plugin connection token."""
    await _verify_plugin_connection_token(authorization, container.db)

    requested_emails = (request or {}).get("emails") if isinstance(request, dict) else None
    email_filter = {
        str(email or "").strip().lower()
        for email in (requested_emails if isinstance(requested_emails, list) else [])
        if str(email or "").strip()
    }
    result = []
    for row in await container.db.get_all_tokens_with_stats():
        email = str(row.get("email") or "").strip()
        if email_filter and email.lower() not in email_filter:
            continue
        try:
            token_obj = Token(**row)
        except Exception:
            token_obj = None
        result.append(
            {
                "id": row.get("id"),
                "email": email,
                "is_active": bool(row.get("is_active")),
                "needs_refresh": container.token_manager.needs_at_refresh(token_obj) if token_obj else True,
                "at_expires": _to_iso(row.get("at_expires")) if row.get("at_expires") else None,
                "last_used_at": _to_iso(row.get("last_used_at")) if row.get("last_used_at") else None,
                "protocol_mode": row.get("protocol_mode") or "session",
                "auto_refresh_enabled": bool(row.get("auto_refresh_enabled", True)),
                "refresh_interval_minutes": row.get("refresh_interval_minutes") or 120,
                "last_st_refresh_at": _to_iso(row.get("last_st_refresh_at")) if row.get("last_st_refresh_at") else None,
                "last_st_refresh_result": row.get("last_st_refresh_result") or "",
                "credits": row.get("credits", 0),
            }
        )
    return {"success": True, "tokens": result}


# Keep the CLIProxy surface in a small, auditable allowlist module. Every route
# inherits the same persistent admin-session check used throughout this API.
from .cliproxy_admin import router as cliproxy_admin_router

router.include_router(
    cliproxy_admin_router,
    prefix="/api/admin/cliproxy",
    dependencies=[Depends(verify_admin_token)],
)

# Keep the public aggregate stable while assigning every route to one focused
# transport router. Handler dependencies move to AppContainer in Phase 7.
from ..transport.admin import build_admin_router

router = build_admin_router(router, admin_dependency=verify_admin_token)
