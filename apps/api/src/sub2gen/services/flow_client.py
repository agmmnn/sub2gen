"""Flow API Client for VideoFX (Veo)"""
import asyncio
import json
import contextvars
import os
import re
import time
import uuid
import random
import base64
import gzip
import ssl
from typing import Dict, Any, Optional, List, Union, Callable, Awaitable
from urllib.parse import quote, urlparse
import urllib.error
import urllib.request
from curl_cffi.requests import AsyncSession
from ..core.logger import debug_logger
from ..core.config import config, get_runtime_tmp_dir, get_yescaptcha_min_score
from sub2gen_provider_google_flow import (
    FlowAuthResource,
    FlowImagesResource,
    FlowMediaResource,
    FlowModelResource,
    FlowProjectsResource,
    FlowTransport,
    FlowVideosResource,
)
from .extension_generation_service import ExtensionGenerationService
from .browser_captcha_extension import NoExtensionGenerationWorkerError

try:
    import httpx
except ImportError:
    httpx = None


def _proxy_endpoint_for_log(url: Optional[str]) -> str:
    """Host:port (scheme) for logs; strips credentials."""
    if not url or not isinstance(url, str):
        return "direct"
    u = url.strip()
    if not u:
        return "direct"
    if "://" not in u:
        u = f"http://{u}"
    try:
        p = urlparse(u)
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
        scheme = (p.scheme or "http").lower()
        return f"{scheme}://{host}{port}"
    except Exception:
        return "unparseable"


PollTaskProgressHook = Optional[Callable[[Dict[str, Any]], Awaitable[None]]]


def _http_status_from_flow_error(message: str) -> int:
    if not message:
        return 0
    marker = "HTTP Error "
    if marker not in message:
        return 0
    start = message.index(marker) + len(marker)
    digits = ""
    for i in range(start, min(start + 5, len(message))):
        ch = message[i]
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else 0


def classify_recaptcha_upstream_failure(status_code: int, error_detail: str) -> Optional[str]:
    """Return ``upstream_rejected`` when error text matches upstream captcha heuristics (else None)."""
    if status_code < 400:
        return None
    detail = (error_detail or "").strip().replace("\n", " ")
    detail_lower = detail.lower()
    if len(detail) > 240:
        detail = detail[:240] + "..."
    if "public_error_unusual_activity" in detail_lower or "recaptcha evaluation failed" in detail_lower:
        return "upstream_rejected"
    if "recaptcha" in detail_lower or "public_error" in detail_lower:
        return "upstream_rejected"
    return None


async def _emit_poll_task_progress(hook: PollTaskProgressHook, updates: Dict[str, Any]) -> None:
    if hook is None:
        return
    payload = {k: v for k, v in updates.items() if v is not None}
    if not payload:
        return
    try:
        await hook(payload)
    except Exception as exc:
        debug_logger.log_warning(f"[poll_task_progress] update failed: {exc}")


_flow_extension_upstream_req_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "flow_extension_upstream_req_id",
    default=None,
)


class FlowClient:
    """VideoFX API客户端"""

    FLOW_PUBLIC_API_KEY = "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"
    FLOW_BROWSER_CHANNEL_HEADER = "stable"
    FLOW_BROWSER_COPYRIGHT_HEADER = "Copyright 2026 Google LLC. All Rights Reserved."
    FLOW_BROWSER_VALIDATION_HEADER = "MRCPrt/rS3JY47x2Yiz9h3ag4U8="
    FLOW_BROWSER_YEAR_HEADER = "2026"

    def __init__(self, proxy_manager, db=None):
        self.proxy_manager = proxy_manager
        self.db = db  # Database instance for captcha config
        self.labs_base_url = config.flow_labs_base_url  # https://labs.google/fx/api
        self.api_base_url = config.flow_api_base_url    # https://aisandbox-pa.googleapis.com/v1
        self.timeout = config.flow_timeout
        self.flow_max_retries = config.flow_max_retries
        # 缓存每个账号的 User-Agent
        self._user_agent_cache = {}
        # 当前请求链路绑定的浏览器指纹（基于 contextvar，避免并发串扰）
        self._request_fingerprint_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
            "flow_request_fingerprint",
            default=None
        )
        # Per-request marker: defer remote fallback until a target retry attempt index.
        # -1 means disabled for current request chain.
        self._remote_fallback_attempt_ctx: contextvars.ContextVar[int] = contextvars.ContextVar(
            "flow_remote_fallback_attempt",
            default=-1
        )
        # Request-scoped managed API key identity for captcha worker isolation.
        self._managed_api_key_id_ctx: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
            "flow_managed_api_key_id",
            default=None,
        )
        self._remote_browser_prefill_last_sent: Dict[str, float] = {}
        self.extension_generation_service = ExtensionGenerationService(db=db)
        self._force_local_http_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
            "flow_force_local_http",
            default=False,
        )
        self._active_generation_token_id_ctx: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
            "flow_active_generation_token_id",
            default=None,
        )
        # Last reCAPTCHA action for narrative logs (IMAGE_GENERATION vs VIDEO_GENERATION, etc.)
        self._last_recaptcha_action: Optional[str] = None
        self.transport = FlowTransport(self)
        self.auth = FlowAuthResource(self)
        self.projects = FlowProjectsResource(self)
        self.media = FlowMediaResource(self)
        self.images = FlowImagesResource(self)
        self.videos = FlowVideosResource(self)
        self.models = FlowModelResource()

        # Default "real browser" headers (macOS Chrome Desktop) to reduce upstream 4xx/5xx instability.
        # NOTE: Platform headers are synchronized per request from the selected User-Agent.
        # These will be applied as defaults (won't override caller-provided headers).
        self._default_client_headers = {
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"macOS\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "x-browser-channel": self.FLOW_BROWSER_CHANNEL_HEADER,
            "x-browser-copyright": self.FLOW_BROWSER_COPYRIGHT_HEADER,
            "x-browser-validation": self.FLOW_BROWSER_VALIDATION_HEADER,
            "x-browser-year": self.FLOW_BROWSER_YEAR_HEADER,
        }
        # 发车策略改为“请求到就发”：
        # 不在 sub2gen 本地对提交做批次整形或排队，避免把同批请求打成阶梯。

    async def _get_personal_browser_identity(
        self,
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Return the latest Personal browser fingerprint and User-Agent."""
        try:
            from .browser_captcha_personal import BrowserCaptchaService

            service = await BrowserCaptchaService.get_instance(self.db)
            if service is None:
                return None, None

            fingerprint: Optional[Dict[str, Any]] = None
            fingerprint_getter = getattr(service, "get_last_fingerprint", None)
            if callable(fingerprint_getter):
                raw_fingerprint = fingerprint_getter()
                if isinstance(raw_fingerprint, dict):
                    fingerprint = dict(raw_fingerprint)

            fingerprint_user_agent = None
            if fingerprint is not None:
                fingerprint_user_agent = str(fingerprint.get("user_agent") or "").strip() or None

            current_user_agent = fingerprint_user_agent
            if not current_user_agent:
                user_agent_getter = getattr(service, "get_current_user_agent", None)
                if callable(user_agent_getter):
                    current_user_agent = str(await user_agent_getter() or "").strip() or None

            return fingerprint, current_user_agent
        except Exception as e:
            debug_logger.log_warning(f"[FlowClient] Failed to get Personal captcha browser identity: {e}")
            return None, None

    async def _generate_real_browser_user_agent(self) -> Optional[str]:
        """Return the latest Personal captcha browser User-Agent when available."""
        cached_user_agent = self._user_agent_cache.get("_real_ua")
        if isinstance(cached_user_agent, str) and cached_user_agent.strip():
            return cached_user_agent.strip()

        _, user_agent = await self._get_personal_browser_identity()
        if user_agent:
            self._user_agent_cache["_real_ua"] = user_agent
            debug_logger.log_info("[FlowClient] Using User-Agent from Personal captcha browser runtime")
        return user_agent

    def _generate_user_agent(self, account_id: str = None) -> str:
        """基于账号ID生成固定的 User-Agent
        
        Args:
            account_id: 账号标识（如 email 或 token_id），相同账号返回相同 UA
            
        Returns:
            User-Agent 字符串
        """
        # 如果没有提供账号ID，生成随机UA
        if not account_id:
            account_id = f"random_{random.randint(1, 999999)}"
        
        # 如果已缓存，直接返回
        if account_id in self._user_agent_cache:
            return self._user_agent_cache[account_id]
        
        # 使用账号ID作为随机种子，确保同一账号生成相同的UA
        import hashlib
        seed = int(hashlib.md5(account_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        
        # Chrome 版本池 - 匹配真实 Mac mini Chrome 环境
        chrome_versions = ["149.0.0.0"]
        ch_version = rng.choice(chrome_versions)
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ch_version} Safari/537.36"
        )
        
        # 缓存结果
        self._user_agent_cache[account_id] = user_agent
        
        return user_agent

    def _set_request_fingerprint(self, fingerprint: Optional[Dict[str, Any]]):
        """设置当前请求链路的浏览器指纹上下文。"""
        self._request_fingerprint_ctx.set(dict(fingerprint) if fingerprint else None)

    def get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """获取当前请求链路绑定的浏览器指纹快照。"""
        fingerprint = self._request_fingerprint_ctx.get()
        if not isinstance(fingerprint, dict) or not fingerprint:
            return None
        return dict(fingerprint)

    def clear_request_fingerprint(self):
        """清理请求链路绑定的浏览器指纹。"""
        self._set_request_fingerprint(None)

    def _get_primary_accept_language(self, fallback: str = "en-US") -> str:
        fingerprint = self.get_request_fingerprint()
        value = str((fingerprint or {}).get("accept_language") or "").strip()
        return value or fallback

    def _infer_sec_ch_ua_from_user_agent(self, user_agent: Optional[str]) -> str:
        ua = str(user_agent or "").strip()
        if not ua:
            return ""
        match = re.search(r"(?:Chrome|Chromium)/(\d+)", ua, re.IGNORECASE)
        major = match.group(1) if match else "124"
        return f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not)A;Brand";v="24"'

    def _normalize_sec_ch_ua_header(
        self,
        sec_ch_ua: Optional[str],
        *,
        user_agent: Optional[str] = None,
    ) -> str:
        raw = str(sec_ch_ua or "").strip()
        inferred = self._infer_sec_ch_ua_from_user_agent(user_agent)
        if not raw:
            return inferred
        if "chrome/" in str(user_agent or "").lower() and "google chrome" not in raw.lower():
            return inferred
        return raw or inferred

    @staticmethod
    def _normalize_accept_language_header(
        accept_language: Optional[str],
        fallback: str = "zh-CN,zh;q=0.9",
    ) -> str:
        raw = str(accept_language or "").strip()
        if not raw:
            return fallback
        if "," in raw:
            normalized_parts: list[str] = []
            for index, item in enumerate(raw.split(",")):
                candidate = str(item or "").strip()
                if not candidate:
                    continue
                language = candidate.split(";", 1)[0].strip()
                if not language:
                    continue
                if index == 0:
                    normalized_parts.append(language)
                    continue
                q_match = re.search(r";\s*q=([0-9.]+)", candidate, re.IGNORECASE)
                q_value = q_match.group(1) if q_match else f"{max(0.1, 1 - (index * 0.1)):.1f}"
                normalized_parts.append(f"{language};q={q_value}")
            return ",".join(normalized_parts) or fallback
        if "-" in raw:
            primary = raw.split("-", 1)[0].strip()
            if len(primary) == 2 and primary.isalpha():
                return f"{raw},{primary};q=0.9"
        return raw

    @staticmethod
    def _should_attach_runtime_session_cookies(url: str) -> bool:
        host = str(urlparse(str(url or "")).hostname or "").lower()
        if not host:
            return False
        return any(
            host == candidate or host.endswith(f".{candidate}")
            for candidate in ("google.com", "googleapis.com", "labs.google", "recaptcha.net")
        )

    @staticmethod
    def _merge_cookie_header(
        existing_cookie_header: Optional[str],
        extra_cookies: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        cookie_items: Dict[str, str] = {}
        for part in str(existing_cookie_header or "").split(";"):
            item = str(part or "").strip()
            if not item or "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip()
            if key:
                cookie_items[key] = value.strip()
        if isinstance(extra_cookies, dict):
            for key, value in extra_cookies.items():
                normalized_key = str(key or "").strip()
                normalized_value = str(value or "").strip()
                if normalized_key and normalized_value and normalized_key not in cookie_items:
                    cookie_items[normalized_key] = normalized_value
        if not cookie_items:
            return str(existing_cookie_header or "").strip() or None
        return "; ".join(f"{key}={value}" for key, value in cookie_items.items())

    def _get_effective_request_user_agent(self, account_id: Optional[str] = None) -> str:
        fingerprint = self.get_request_fingerprint()
        value = str((fingerprint or {}).get("user_agent") or "").strip()
        return value or self._generate_user_agent(account_id)

    @staticmethod
    def _build_flow_project_page_url(project_id: str) -> str:
        return f"https://labs.google/fx/tools/flow/project/{project_id}"

    def _build_current_flow_media_headers(
        self,
        *,
        content_type: str = "application/json",
    ) -> Dict[str, str]:
        return {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9"),
            "Content-Type": content_type,
            "Origin": "https://labs.google",
            "Priority": "u=1, i",
            "Referer": "https://labs.google/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "sec-fetch-storage-access": "active",
            "x-browser-channel": self.FLOW_BROWSER_CHANNEL_HEADER,
            "x-browser-copyright": self.FLOW_BROWSER_COPYRIGHT_HEADER,
            "x-browser-validation": self.FLOW_BROWSER_VALIDATION_HEADER,
            "x-browser-year": self.FLOW_BROWSER_YEAR_HEADER,
        }

    def _build_labs_request_context_headers(self, project_id: Optional[str]) -> Dict[str, str]:
        headers = self._build_current_flow_media_headers()
        if project_id:
            headers["Referer"] = self._build_flow_project_page_url(project_id)
        return headers

    @staticmethod
    def _extract_project_id_from_request_payload(payload: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        client_context = payload.get("clientContext")
        if isinstance(client_context, dict):
            project_id = str(client_context.get("projectId") or "").strip()
            if project_id:
                return project_id
        requests = payload.get("requests")
        if isinstance(requests, list):
            for item in requests:
                item_context = item.get("clientContext") if isinstance(item, dict) else None
                project_id = str((item_context or {}).get("projectId") or "").strip()
                if project_id:
                    return project_id
        return None

    @staticmethod
    def _compact_json_dumps(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _encode_trpc_input(self, payload: Dict[str, Any]) -> str:
        return quote(self._compact_json_dumps(payload), safe="")

    async def _get_token_st_by_id(self, token_id: Optional[int]) -> Optional[str]:
        if not token_id or self.db is None or not hasattr(self.db, "get_token"):
            return None
        token = await self.db.get_token(int(token_id))
        value = str(getattr(token, "st", "") or "").strip() if token else ""
        return value or None

    @staticmethod
    def _resolve_runtime_impersonate() -> str:
        return "chrome124"

    def _set_remote_fallback_attempt(self, attempt_index: int) -> None:
        self._remote_fallback_attempt_ctx.set(int(attempt_index))

    def set_managed_api_key_id(self, api_key_id: Optional[int]) -> None:
        """Bind managed API key id to current request context."""
        try:
            normalized = int(api_key_id) if api_key_id is not None else None
        except (TypeError, ValueError):
            normalized = None
        self._managed_api_key_id_ctx.set(normalized)

    def get_managed_api_key_id(self) -> Optional[int]:
        value = self._managed_api_key_id_ctx.get()
        return int(value) if isinstance(value, int) else None

    def clear_managed_api_key_id(self) -> None:
        self._managed_api_key_id_ctx.set(None)

    def set_force_local_http(self, enabled: bool) -> None:
        self._force_local_http_ctx.set(bool(enabled))

    def clear_force_local_http(self) -> None:
        self._force_local_http_ctx.set(False)

    def set_active_generation_token_id(self, token_id: Optional[int]) -> None:
        self._active_generation_token_id_ctx.set(token_id if token_id is not None else None)

    def get_active_generation_token_id(self) -> Optional[int]:
        return self._active_generation_token_id_ctx.get()

    def clear_active_generation_token_id(self) -> None:
        self._active_generation_token_id_ctx.set(None)

    def _is_headed_docker_runtime(self) -> bool:
        raw = str(os.environ.get("ALLOW_DOCKER_HEADED_CAPTCHA", "")).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _should_use_deferred_remote_fallback(
        self,
        *,
        captcha_method: str,
        retry_attempt: int,
    ) -> bool:
        if captcha_method != "browser":
            return False
        if not bool(config.browser_fallback_to_remote_browser):
            return False
        if not self._is_headed_docker_runtime():
            return False
        target_attempt = int(self._remote_fallback_attempt_ctx.get())
        return target_attempt >= 0 and int(retry_attempt) >= target_attempt

    def _can_use_browser_gateway_fallback(self) -> bool:
        return bool(config.browser_fallback_to_remote_browser) and self._is_headed_docker_runtime()

    def _should_submit_generation_via_extension(self, method: str, url: str, json_data: Optional[Dict[str, Any]]) -> bool:
        if bool(self._force_local_http_ctx.get()):
            return False
        if not bool(config.extension_generation_enabled):
            return False
        if str(config.captcha_method).strip().lower() != "extension":
            return False
        if str(method or "").upper() != "POST":
            return False
        return self._is_generation_request_with_recaptcha(url, json_data)

    async def _token_allows_extension_generation(self, token_id: Optional[int]) -> bool:
        if token_id is None:
            return True
        if not self.db or not hasattr(self.db, "get_token"):
            return True
        try:
            row = await self.db.get_token(int(token_id))
            if row is None:
                return True
            return bool(getattr(row, "use_extension_for_generation", True))
        except Exception as exc:
            debug_logger.log_warning(f"[EXT-GEN] token extension-generation flag lookup failed: {exc}")
            return True

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        use_media_proxy: bool = False,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        allow_urllib_fallback: bool = True,
        apply_default_client_headers: bool = True,
        impersonate: str = "chrome124",
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """统一HTTP请求处理

        Args:
            method: HTTP方法 (GET/POST)
            url: 完整URL
            headers: 请求头
            json_data: JSON请求体
            use_st: 是否使用ST认证 (Cookie方式)
            st_token: Session Token
            use_at: 是否使用AT认证 (Bearer方式)
            at_token: Access Token
            timeout: 自定义超时时间(秒)，不传则使用默认值
            use_media_proxy: 是否使用图片上传/下载代理
            respect_fingerprint_proxy: 是否优先使用打码浏览器指纹里的代理
            force_no_proxy: 是否强制直连（忽略所有代理）
            allow_urllib_fallback: curl_cffi 失败后是否允许 urllib 兜底
        """
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if not force_no_proxy:
            if self.proxy_manager:
                if use_media_proxy and hasattr(self.proxy_manager, "get_media_proxy_url"):
                    proxy_url = await self.proxy_manager.get_media_proxy_url()
                elif hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()

            if respect_fingerprint_proxy and isinstance(fingerprint, dict) and "proxy_url" in fingerprint:
                proxy_url = fingerprint.get("proxy_url")
                if proxy_url == "":
                    proxy_url = None
        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}
        else:
            headers = dict(headers)

        # ST认证 - 使用Cookie
        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        # AT认证 - 使用Bearer
        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        # 确定账号标识（优先使用 token 的前16个字符作为标识）
        account_id = None
        if st_token:
            account_id = st_token[:16]  # 使用 ST 的前16个字符
        elif at_token:
            account_id = at_token[:16]  # 使用 AT 的前16个字符

        # 通用请求头 - 优先使用打码浏览器指纹中的 UA
        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            fingerprint_user_agent = fingerprint.get("user_agent")
        if (
            not fingerprint_user_agent
            and str(getattr(config, "captcha_method", "")).strip().lower() == "personal"
        ):
            browser_fingerprint, browser_user_agent = await self._get_personal_browser_identity()
            if not isinstance(fingerprint, dict) and isinstance(browser_fingerprint, dict):
                fingerprint = browser_fingerprint
            if isinstance(fingerprint, dict):
                fingerprint_user_agent = str(fingerprint.get("user_agent") or "").strip() or None
            fingerprint_user_agent = fingerprint_user_agent or browser_user_agent
            if fingerprint_user_agent:
                self._user_agent_cache["_real_ua"] = fingerprint_user_agent

        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", fingerprint_user_agent or self._generate_user_agent(account_id))
        fingerprint_accept_language = ""
        if isinstance(fingerprint, dict):
            fingerprint_accept_language = str(fingerprint.get("accept_language") or "").strip()
        headers.setdefault(
            "Accept-Language",
            self._normalize_accept_language_header(
                fingerprint_accept_language or self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9")
            ),
        )

        # 若存在打码浏览器指纹，覆盖关键客户端提示头，保证提交请求与打码时一致。
        if isinstance(fingerprint, dict):
            if fingerprint.get("accept_language"):
                headers["Accept-Language"] = fingerprint["accept_language"]
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = self._normalize_sec_ch_ua_header(
                    fingerprint["sec_ch_ua"],
                    user_agent=headers.get("User-Agent"),
                )
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = fingerprint["sec_ch_ua_mobile"]
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = fingerprint["sec_ch_ua_platform"]
            if self._should_attach_runtime_session_cookies(url):
                origin = str(fingerprint.get("origin") or "").strip() or "https://labs.google"
                referer = str(fingerprint.get("referer") or "").strip()
                if not referer:
                    fingerprint_project_id = str(fingerprint.get("project_id") or "").strip()
                    if fingerprint_project_id:
                        referer = self._build_flow_project_page_url(fingerprint_project_id)
                headers.setdefault("Origin", origin)
                if referer:
                    headers.setdefault("Referer", referer)
                merged_cookie_header = self._merge_cookie_header(
                    headers.get("Cookie"),
                    fingerprint.get("session_cookies"),
                )
                if merged_cookie_header:
                    headers["Cookie"] = merged_cookie_header

        if self._should_attach_runtime_session_cookies(url):
            derived_project_id = self._extract_project_id_from_request_payload(json_data)
            headers.setdefault("Origin", "https://labs.google")
            if derived_project_id:
                headers.setdefault("Referer", self._build_flow_project_page_url(derived_project_id))

        # Add default Chromium/Android client headers (do not override explicitly provided values).
        if apply_default_client_headers:
            for key, value in self._default_client_headers.items():
                headers.setdefault(key, value)

        # Dynamic fix for sec-ch-ua headers when fingerprint is missing to avoid UA/Platform mismatch
        if not isinstance(fingerprint, dict) or not fingerprint.get("sec_ch_ua_platform"):
            ua_lower = headers.get("User-Agent", "").lower()
            if "android" in ua_lower:
                headers["sec-ch-ua-platform"] = "\"Android\""
                headers["sec-ch-ua-mobile"] = "?1"
            elif "mac" in ua_lower:
                headers["sec-ch-ua-platform"] = "\"macOS\""
                headers["sec-ch-ua-mobile"] = "?0"
            elif "linux" in ua_lower or "x11" in ua_lower:
                headers["sec-ch-ua-platform"] = "\"Linux\""
                headers["sec-ch-ua-mobile"] = "?0"
            else:
                headers["sec-ch-ua-platform"] = "\"Windows\""
                headers["sec-ch-ua-mobile"] = "?0"
        if not headers.get("sec-ch-ua"):
            headers["sec-ch-ua"] = self._infer_sec_ch_ua_from_user_agent(headers.get("User-Agent"))

        if "aisandbox" in url:
            print(f"[DEBUG-DEEP] API REQUEST to: {url[:80]}")
            print(f"[DEBUG-DEEP] fingerprint: {fingerprint}")
            print(f"[DEBUG-DEEP] proxy_url: {proxy_url}")
            print(f"[DEBUG-DEEP] UA: {headers.get('User-Agent', '')[:100]}")
            print(f"[DEBUG-DEEP] sec-ch-ua-platform: {headers.get('sec-ch-ua-platform', 'NOT SET')}")
            print(f"[DEBUG-DEEP] sec-ch-ua-mobile: {headers.get('sec-ch-ua-mobile', 'NOT SET')}")

        # Log request
        if config.debug_enabled:
            if isinstance(fingerprint, dict):
                proxy_for_log = proxy_url if proxy_url else "direct"
                debug_logger.log_info(
                    f"[FINGERPRINT] 使用打码浏览器指纹提交请求: UA={headers.get('User-Agent', '')[:120]}, proxy={proxy_for_log}"
                )
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=raw_body if raw_body is not None else json_data,
                proxy=proxy_url
            )

        start_time = time.time()

        if self._should_submit_generation_via_extension(method, url, json_data):
            managed_api_key_id = self.get_managed_api_key_id()
            routing_token_id = token_id if token_id is not None else self.get_active_generation_token_id()
            if await self._token_allows_extension_generation(routing_token_id):
                try:
                    debug_logger.log_info(
                        f"[EXT-GEN] submit dispatch via extension: method={method.upper()}, managed_api_key_id={managed_api_key_id}, token_id={routing_token_id}"
                    )
                    return await self.extension_generation_service.submit_generation(
                        url=url,
                        method=method.upper(),
                        headers=headers,
                        json_data=json_data if isinstance(json_data, dict) else {},
                        timeout_seconds=int(request_timeout),
                        token_id=routing_token_id,
                        managed_api_key_id=managed_api_key_id,
                    )
                except NoExtensionGenerationWorkerError as no_ext:
                    debug_logger.log_warning(f"[EXT-GEN] no generation worker, using local HTTP: {no_ext}")
                except Exception as ext_err:
                    debug_logger.log_error(f"[EXT-GEN] extension submit failed: {ext_err}")
                    raise Exception(f"Flow API request failed: {ext_err}")

        try:
            async with AsyncSession(trust_env=False) as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate=impersonate
                    )
                else:  # POST
                    request_kwargs = {
                        "headers": headers,
                        "proxy": proxy_url,
                        "timeout": request_timeout,
                        "impersonate": impersonate,
                    }
                    if raw_body is not None:
                        request_kwargs["data"] = raw_body
                    else:
                        request_kwargs["json"] = json_data
                    response = await session.post(url, **request_kwargs)

                duration_ms = (time.time() - start_time) * 1000

                # Log response
                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=response.text,
                        duration_ms=duration_ms
                    )

                # 检查HTTP错误
                if response.status_code >= 400:
                    # 解析错误响应
                    error_reason = f"HTTP Error {response.status_code}"
                    try:
                        error_body = response.json()
                        # 提取 Google API 错误格式中的 reason
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            # 从 details 中提取 reason
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except:
                        error_reason = f"HTTP Error {response.status_code}: {response.text[:200]}"
                    
                    # 失败时输出请求体和错误内容到控制台
                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(f"[API FAILED] Request Body: {raw_body if raw_body is not None else json_data}")
                    debug_logger.log_error(f"[API FAILED] Response: {response.text}")
                    
                    self._log_recaptcha_verdict_from_response(
                        url=url,
                        json_data=json_data,
                        status_code=response.status_code,
                        error_reason=error_reason,
                        response_text=response.text,
                    )
                    await self._send_extension_upstream_verdict_if_needed(
                        url,
                        json_data,
                        response.status_code,
                        error_reason,
                        response.text,
                    )
                    raise Exception(error_reason)

                self._log_recaptcha_verdict_from_response(
                    url=url,
                    json_data=json_data,
                    status_code=response.status_code,
                    error_reason="",
                    response_text=response.text,
                )
                parsed_body = response.json()
                await self._send_extension_upstream_verdict_if_needed(
                    url,
                    json_data,
                    response.status_code,
                    "",
                    response.text,
                )

                return parsed_body

        except Exception as e:
            await self._abandon_extension_upstream_verdict_if_needed()
            duration_ms = (time.time() - start_time) * 1000
            error_msg = str(e)

            # 如果不是我们自己抛出的异常，记录日志
            if "HTTP Error" not in error_msg and not any(x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(f"[API FAILED] Request Body: {raw_body if raw_body is not None else json_data}")
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")

            http2_transport_error = self._is_http2_transport_error(error_msg)
            if http2_transport_error:
                debug_logger.log_warning(
                    "🚨 [HTTP2 TRANSPORT] curl_cffi/libcurl HTTP/2 failure detected: "
                    f"method={method.upper()}, url={url}, proxy={_proxy_endpoint_for_log(proxy_url)}, "
                    f"timeout={request_timeout}s, allow_urllib_fallback={allow_urllib_fallback}, "
                    f"error={error_msg[:240]}"
                )

            if allow_urllib_fallback and self._should_fallback_to_urllib(error_msg):
                debug_logger.log_warning(
                    f"⚠️ [HTTP FALLBACK] curl_cffi request failed, falling back to urllib: {method.upper()} {url}"
                )
                try:
                    urllib_result = await asyncio.to_thread(
                        self._sync_json_request_via_urllib,
                        method.upper(),
                        url,
                        headers,
                        json_data,
                        proxy_url,
                        request_timeout,
                    )
                    await self._send_extension_upstream_verdict_if_needed(
                        url,
                        json_data,
                        200,
                        "",
                        "",
                    )
                    return urllib_result
                except Exception as fallback_error:
                    debug_logger.log_error(
                        f"[HTTP FALLBACK] urllib 回退也失败: {fallback_error}"
                    )
                    raise Exception(
                        f"Flow API request failed: curl={error_msg}; urllib={fallback_error}"
                    )

            raise Exception(f"Flow API request failed: {error_msg}")

    async def _make_text_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        apply_default_client_headers: bool = True,
        impersonate: str = "chrome124",
    ) -> str:
        """Execute a request whose response is text, including SSE streams."""
        fingerprint = self.get_request_fingerprint()
        proxy_url = None
        if not force_no_proxy and self.proxy_manager:
            getter = getattr(self.proxy_manager, "get_request_proxy_url", None)
            if not callable(getter):
                getter = getattr(self.proxy_manager, "get_proxy_url", None)
            if callable(getter):
                proxy_url = await getter()
        if respect_fingerprint_proxy and isinstance(fingerprint, dict) and "proxy_url" in fingerprint:
            proxy_url = fingerprint.get("proxy_url") or None

        request_headers = dict(headers or {})
        if use_st and st_token:
            request_headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"
        if use_at and at_token:
            request_headers["authorization"] = f"Bearer {at_token}"
        account_id = (st_token or at_token or "")[:16] or None
        request_headers.setdefault("Content-Type", "application/json")
        request_headers.setdefault("User-Agent", self._get_effective_request_user_agent(account_id))
        request_headers.setdefault(
            "Accept-Language",
            self._normalize_accept_language_header(
                str((fingerprint or {}).get("accept_language") or "")
                or self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9")
            ),
        )
        if isinstance(fingerprint, dict):
            if fingerprint.get("sec_ch_ua"):
                request_headers["sec-ch-ua"] = self._normalize_sec_ch_ua_header(
                    fingerprint.get("sec_ch_ua"),
                    user_agent=request_headers.get("User-Agent"),
                )
            if fingerprint.get("sec_ch_ua_mobile"):
                request_headers["sec-ch-ua-mobile"] = str(fingerprint["sec_ch_ua_mobile"])
            if fingerprint.get("sec_ch_ua_platform"):
                request_headers["sec-ch-ua-platform"] = str(fingerprint["sec_ch_ua_platform"])
            if self._should_attach_runtime_session_cookies(url):
                request_headers.setdefault("Origin", str(fingerprint.get("origin") or "https://labs.google"))
                referer = str(fingerprint.get("referer") or "").strip()
                if referer:
                    request_headers.setdefault("Referer", referer)
                merged_cookie_header = self._merge_cookie_header(
                    request_headers.get("Cookie"),
                    fingerprint.get("session_cookies"),
                )
                if merged_cookie_header:
                    request_headers["Cookie"] = merged_cookie_header
        if self._should_attach_runtime_session_cookies(url):
            project_id = self._extract_project_id_from_request_payload(json_data)
            request_headers.setdefault("Origin", "https://labs.google")
            if project_id:
                request_headers.setdefault("Referer", self._build_flow_project_page_url(project_id))
        if apply_default_client_headers:
            for key, value in self._default_client_headers.items():
                request_headers.setdefault(key, value)

        async with AsyncSession(trust_env=False) as session:
            request_kwargs = {
                "headers": request_headers,
                "proxy": proxy_url,
                "timeout": timeout or self.timeout,
                "impersonate": impersonate,
            }
            if method.upper() == "GET":
                response = await session.get(url, **request_kwargs)
            else:
                if raw_body is not None:
                    request_kwargs["data"] = raw_body
                else:
                    request_kwargs["json"] = json_data
                response = await session.post(url, **request_kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP Error {response.status_code}: {(response.text or '')[:500]}")
        return response.text or ""

    def _is_generation_request_with_recaptcha(self, url: str, json_data: Optional[Dict[str, Any]]) -> bool:
        """Best-effort detection for generation requests carrying recaptchaContext token."""
        if not isinstance(json_data, dict):
            return False
        lowered = (url or "").lower()
        if not any(key in lowered for key in [
            "batchgenerateimages",
            "batchasyncgeneratevideotext",
            "batchasyncgeneratevideoimage",
            "batchasyncgeneratevideoreferenceimages",
            "batchasyncgeneratevideostartandendimage",
            "batchasyncgeneratevideostartimage",
            "batchasyncgeneratevideoextendvideo",
            "batchasyncgeneratevideoupsamplevideo",
            "upsampleimage",
            "remix",
        ]):
            return False
        cc = json_data.get("clientContext")
        if not isinstance(cc, dict):
            return False
        rc = cc.get("recaptchaContext")
        if not isinstance(rc, dict):
            return False
        token = rc.get("token")
        return isinstance(token, str) and bool(token.strip())

    def _log_recaptcha_verdict_from_response(
        self,
        url: str,
        json_data: Optional[Dict[str, Any]],
        status_code: int,
        error_reason: str,
        response_text: str,
    ) -> None:
        """Emit explicit accepted/rejected verdict for recaptcha-protected generation endpoints."""
        if not debug_logger.should_log_recaptcha():
            return
        if not self._is_generation_request_with_recaptcha(url, json_data):
            return

        short_url = (url or "").split("?")[0]
        if status_code < 400:
            debug_logger.log_recaptcha_proxy_check(
                f"[reCAPTCHA verdict] ACCEPTED by upstream endpoint: status={status_code}, url={short_url}"
            )
            return

        detail = (error_reason or response_text or "").strip().replace("\n", " ")
        detail_lower = detail.lower()
        if len(detail) > 240:
            detail = detail[:240] + "..."

        if "public_error_unusual_activity" in detail_lower or "recaptcha evaluation failed" in detail_lower:
            debug_logger.log_recaptcha_proxy_check(
                f"[reCAPTCHA verdict] REJECTED by upstream endpoint: status={status_code}, reason={detail}"
            )
            return

        if "recaptcha" in detail_lower or "public_error" in detail_lower:
            debug_logger.log_recaptcha_proxy_check(
                f"[reCAPTCHA verdict] UPSTREAM captcha-related error: status={status_code}, reason={detail}"
            )

    async def _send_extension_upstream_verdict_if_needed(
        self,
        url: str,
        json_data: Optional[Dict[str, Any]],
        status_code: int,
        error_reason: str,
        response_text: str,
    ) -> None:
        """Notify Chrome extension whether Flow accepted the reCAPTCHA token (extension captcha mode only)."""
        if config.captcha_method != "extension":
            return
        if not self._is_generation_request_with_recaptcha(url, json_data):
            return
        req_id = _flow_extension_upstream_req_id.get()
        if not req_id:
            return
        accepted = status_code < 400
        merged = f"{error_reason or ''} {(response_text or '')[:800]}".strip()
        captcha_rej = (not accepted) and (
            classify_recaptcha_upstream_failure(status_code, merged) == "upstream_rejected"
        )
        try:
            from .browser_captcha_extension import ExtensionCaptchaService

            svc = await ExtensionCaptchaService.get_instance(self.db)
            await svc.notify_upstream_verdict(
                req_id,
                accepted=accepted,
                captcha_rejected=captcha_rej,
                detail=merged[:500] if merged else None,
            )
        except Exception as exc:
            debug_logger.log_warning(f"[Extension Captcha] upstream verdict notify failed: {exc}")
        finally:
            _flow_extension_upstream_req_id.set(None)

    async def _abandon_extension_upstream_verdict_if_needed(self) -> None:
        if config.captcha_method != "extension":
            return
        req_id = _flow_extension_upstream_req_id.get()
        if not req_id:
            return
        try:
            from .browser_captcha_extension import ExtensionCaptchaService

            svc = await ExtensionCaptchaService.get_instance(self.db)
            await svc.abandon_upstream_verdict(req_id)
        except Exception:
            pass
        finally:
            _flow_extension_upstream_req_id.set(None)

    def _should_fallback_to_urllib(self, error_message: str) -> bool:
        """判断是否应从 curl_cffi 回退到 urllib。"""
        if self._is_http2_transport_error(error_message):
            return True
        error_lower = (error_message or "").lower()
        return any(
            keyword in error_lower
            for keyword in [
                "curl: (6)",
                "curl: (7)",
                "curl: (28)",
                "curl: (35)",
                "curl: (52)",
                "curl: (56)",
                "connection timed out",
                "could not connect",
                "failed to connect",
                "ssl connect error",
                "tls connect error",
                "network is unreachable",
            ]
        )

    def _sync_json_request_via_urllib(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, Any]],
        json_data: Optional[Dict[str, Any]],
        proxy_url: Optional[str],
        timeout: int,
    ) -> Dict[str, Any]:
        """使用 urllib 执行 JSON 请求，作为 curl_cffi 的网络回退。"""
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept", "application/json")
        request_headers["Accept-Encoding"] = "identity"

        data = None
        if method.upper() != "GET" and json_data is not None:
            data = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler(
                    {"http": proxy_url, "https": proxy_url}
                )
            )

        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(
            url=url,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )

        try:
            with opener.open(
                request,
                timeout=timeout,
            ) as response:
                payload = response.read()
                status_code = int(response.getcode() or 0)
                content_encoding = str(response.headers.get("Content-Encoding") or "").lower()
        except urllib.error.HTTPError as exc:
            payload = exc.read() if hasattr(exc, "read") else b""
            status_code = int(getattr(exc, "code", 500) or 500)
            content_encoding = str(getattr(exc, "headers", {}).get("Content-Encoding") or "").lower()
            if content_encoding == "gzip" and payload:
                try:
                    payload = gzip.decompress(payload)
                except Exception:
                    pass
            body_text = payload.decode("utf-8", errors="replace")
            raise Exception(f"HTTP Error {status_code}: {body_text[:200]}") from exc
        except Exception as exc:
            raise Exception(str(exc)) from exc

        if content_encoding == "gzip" and payload:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        body_text = payload.decode("utf-8", errors="replace")
        if status_code >= 400:
            raise Exception(f"HTTP Error {status_code}: {body_text[:200]}")

        try:
            return json.loads(body_text) if body_text else {}
        except Exception as exc:
            raise Exception(f"Invalid JSON response: {body_text[:200]}") from exc

    def _is_timeout_error(self, error: Exception) -> bool:
        """判断是否为网络超时，便于快速失败重试。"""
        error_lower = str(error).lower()
        return any(keyword in error_lower for keyword in [
            "timed out",
            "timeout",
            "curl: (28)",
            "connection timed out",
            "operation timed out",
        ])

    def _is_proxy_connection_error(self, error: Exception) -> bool:
        """识别本地/上游代理不可用导致的连接失败。"""
        error_lower = str(error).lower()
        return any(keyword in error_lower for keyword in [
            "failed to connect to 127.0.0.1 port",
            "failed to connect to localhost port",
            "proxyerror",
            "proxy error",
            "failed to connect to proxy",
            "couldn't connect to server",
            "curl: (7)",
        ])

    def _is_http2_transport_error(self, error_message: str) -> bool:
        """Recognize curl/libcurl HTTP/2 transport failures for targeted fallback logs."""
        error_lower = (error_message or "").lower()
        return any(keyword in error_lower for keyword in [
            "curl: (16)",
            "curle_http2",
            "http/2 framing",
            "http2 framing",
            "http/2 stream",
            "http2 stream",
        ])

    def _is_retryable_network_error(self, error_str: str) -> bool:
        """识别可重试的 TLS/连接类网络错误。"""
        if self._is_http2_transport_error(error_str):
            return True
        error_lower = (error_str or "").lower()
        return any(keyword in error_lower for keyword in [
            "curl: (35)",
            "curl: (52)",
            "curl: (56)",
            "ssl_error_syscall",
            "tls connect error",
            "ssl connect error",
            "connection reset",
            "connection aborted",
            "connection was reset",
            "unexpected eof",
            "empty reply from server",
            "recv failure",
            "send failure",
            "connection refused",
            "network is unreachable",
            "remote host closed connection",
            "connection timed out",
            "curl: (28)",
            "timed out",
            "timeout",
        ])

    def _get_video_submit_timeout(self) -> int:
        """视频提交接口应快速返回 operation，避免单次网络挂死拖满整条链路。"""
        return max(30, min(int(self.timeout or 0) or 120, 75))

    def _get_video_poll_timeout(self) -> int:
        """视频状态查询是轻量轮询，请求超时不应超过下一轮轮询太久。"""
        return max(10, min(int(self.timeout or 0) or 120, 45))

    async def _make_video_api_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        timeout: int,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """视频 API 加硬截止，避免底层请求偶发卡住导致整条请求悬挂。"""
        project_id = self._extract_project_id_from_request_payload(json_data)
        raw_body = json.dumps(json_data, ensure_ascii=False, separators=(",", ":"))
        try:
            return await asyncio.wait_for(
                self._make_request(
                    method="POST",
                    url=url,
                    headers=self._build_labs_request_context_headers(project_id),
                    json_data=json_data,
                    raw_body=raw_body,
                    use_at=True,
                    at_token=at,
                    timeout=timeout,
                    allow_urllib_fallback=False,
                    token_id=token_id,
                ),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError as exc:
            raise Exception(f"Flow video API request timed out after {timeout}s") from exc

    def _get_control_plane_timeout(self) -> int:
        """控制轻量控制面请求的超时，避免认证/项目接口长时间挂起。"""
        return max(5, min(int(self.timeout or 0) or 120, 10))

    async def _acquire_image_launch_gate(
        self,
        token_id: Optional[int],
        token_image_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """图片请求不再做本地发车排队，直接进入取 token 并提交上游。"""
        return True, 0, 0

    async def _release_image_launch_gate(self, token_id: Optional[int]):
        """保留接口形状，当前无需释放任何本地发车状态。"""
        return

    async def _acquire_video_launch_gate(
        self,
        token_id: Optional[int],
        token_video_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """视频请求不再做本地发车排队，直接进入取 token 并提交上游。"""
        return True, 0, 0

    async def _release_video_launch_gate(self, token_id: Optional[int]):
        """保留接口形状，当前无需释放任何本地发车状态。"""
        return

    async def _make_image_generation_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        attempt_trace: Optional[Dict[str, Any]] = None,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """图片生成请求使用更短超时，并在网络超时时快速重试。"""
        request_timeout = config.flow_image_request_timeout
        total_attempts = max(1, config.flow_image_timeout_retry_count + 1)
        retry_delay = config.flow_image_timeout_retry_delay

        # 对于浏览器/远程浏览器打码链路，优先保持与打码时一致的出口。
        # 否则在首跳改走媒体代理时，容易触发 reCAPTCHA 校验失败并放大长尾。
        fingerprint = self._request_fingerprint_ctx.get()
        has_fingerprint_context = bool(isinstance(fingerprint, dict) and fingerprint)

        has_media_proxy = False
        if self.proxy_manager and config.flow_image_timeout_use_media_proxy_fallback:
            try:
                has_media_proxy = bool(await self.proxy_manager.get_media_proxy_url())
            except Exception:
                has_media_proxy = False
        prefer_media_first = bool(has_media_proxy and config.flow_image_prefer_media_proxy)

        if has_fingerprint_context and prefer_media_first:
            prefer_media_first = False
            debug_logger.log_info(
                "[IMAGE] 检测到打码浏览器指纹上下文，首跳固定走打码链路；"
                "媒体代理仅在网络超时时作为兜底回退。"
            )

        last_error: Optional[Exception] = None

        for attempt_index in range(total_attempts):
            if has_media_proxy:
                # 两次重试时采用“主链路 + 备链路”策略，避免每次都先卡在错误链路上。
                if attempt_index == 0:
                    prefer_media_proxy = prefer_media_first
                elif attempt_index == 1:
                    prefer_media_proxy = not prefer_media_first
                else:
                    prefer_media_proxy = prefer_media_first
            else:
                prefer_media_proxy = False
            route_label = "媒体代理链路" if prefer_media_proxy else "打码链路"
            http_attempt_started_at = time.time()
            http_attempt_info: Optional[Dict[str, Any]] = None
            if isinstance(attempt_trace, dict):
                http_attempt_info = {
                    "attempt": attempt_index + 1,
                    "route": route_label,
                    "timeout_seconds": request_timeout,
                    "used_media_proxy": bool(prefer_media_proxy),
                }
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    headers=self._build_labs_request_context_headers(
                        self._extract_project_id_from_request_payload(json_data)
                    ),
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    timeout=request_timeout,
                    use_media_proxy=prefer_media_proxy,
                    respect_fingerprint_proxy=not prefer_media_proxy,
                    token_id=token_id,
                )
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int((time.time() - http_attempt_started_at) * 1000)
                    http_attempt_info["success"] = True
                    attempt_trace.setdefault("http_attempts", []).append(http_attempt_info)
                return result
            except Exception as e:
                last_error = e
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int((time.time() - http_attempt_started_at) * 1000)
                    http_attempt_info["success"] = False
                    http_attempt_info["timeout_error"] = bool(self._is_timeout_error(e))
                    http_attempt_info["error"] = str(e)[:240]
                    attempt_trace.setdefault("http_attempts", []).append(http_attempt_info)
                if not self._is_timeout_error(e) or attempt_index >= total_attempts - 1:
                    raise

                if has_media_proxy and total_attempts > 1:
                    next_prefer_media_proxy = (
                        not prefer_media_proxy if attempt_index == 0 else prefer_media_proxy
                    )
                else:
                    next_prefer_media_proxy = prefer_media_proxy
                next_route_label = "媒体代理链路" if next_prefer_media_proxy else "打码链路"
                debug_logger.log_warning(
                    f"[IMAGE] 图片生成请求网络超时，准备快速重试 "
                    f"({attempt_index + 2}/{total_attempts})，当前链路={route_label}，"
                    f"下一链路={next_route_label}，timeout={request_timeout}s"
                )
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("图片生成请求失败")

    # ========== 认证相关 (使用ST) ==========

    async def st_to_at(self, st: str) -> dict:
        return await self.auth.exchange_session_token(st)

    async def _st_to_at_legacy(self, st: str) -> dict:
        """ST转AT

        Args:
            st: Session Token

        Returns:
            {
                "access_token": "AT",
                "expires": "2025-11-15T04:46:04.000Z",
                "user": {...}
            }
        """
        url = f"{self.labs_base_url}/auth/session"
        result = await self._make_request(
            method="GET",
            url=url,
            use_st=True,
            st_token=st,
            timeout=self._get_control_plane_timeout(),
        )
        return result

    # ========== 项目管理 (使用ST) ==========

    async def create_project(self, st: str, title: str) -> str:
        return await self.projects.create(st, title)

    async def _create_project_legacy(self, st: str, title: str) -> str:
        """创建项目,返回project_id

        Args:
            st: Session Token
            title: 项目标题

        Returns:
            project_id (UUID)
        """
        url = f"{self.labs_base_url}/trpc/project.createProject"
        json_data = {
            "json": {
                "projectTitle": title,
                "toolName": "PINHOLE"
            }
        }
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        request_timeout = max(self._get_control_plane_timeout(), min(self.timeout, 15))
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_st=True,
                    st_token=st,
                    timeout=request_timeout,
                )
                project_result = (
                    result.get("result", {})
                    .get("data", {})
                    .get("json", {})
                    .get("result", {})
                )
                project_id = project_result.get("projectId")
                if not project_id:
                    raise Exception("Invalid project.createProject response: missing projectId")
                return project_id
            except Exception as e:
                last_error = e
                retry_reason = "网络超时" if self._is_timeout_error(e) else self._get_retry_reason(str(e))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[PROJECT] 创建项目失败，准备重试 ({retry_attempt + 2}/{max_retries}) "
                        f"title={title!r}, reason={retry_reason}: {e}"
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("创建项目失败")

    async def delete_project(self, st: str, project_id: str):
        return await self.projects.delete(st, project_id)

    async def _delete_project_legacy(self, st: str, project_id: str):
        """删除项目

        Args:
            st: Session Token
            project_id: 项目ID
        """
        url = f"{self.labs_base_url}/trpc/project.deleteProject"
        json_data = {
            "json": {
                "projectToDeleteId": project_id
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st,
            timeout=self._get_control_plane_timeout(),
        )

    async def get_media(self, at: str, media_name: str) -> Dict[str, Any]:
        return await self.media.get(at, media_name)

    async def _get_media_legacy(self, at: str, media_name: str) -> Dict[str, Any]:
        """Fetch a generated media resource, including base64 video fallback data."""
        normalized_at = str(at or "").strip()
        normalized_media_name = str(media_name or "").strip()
        if not normalized_at:
            raise ValueError("get_media: AT token is required")
        if not normalized_media_name:
            raise ValueError("get_media: media_name is required")

        return await self._make_request(
            method="GET",
            url=f"{self.api_base_url}/media/{quote(normalized_media_name, safe='')}",
            headers=self._build_labs_request_context_headers(None),
            use_at=True,
            at_token=normalized_at,
            timeout=max(60, int(self.timeout or 120)),
        )

    # ========== 余额查询 (使用AT) ==========

    async def get_credits(self, at: str) -> dict:
        return await self.auth.get_credits(at)

    async def _get_credits_legacy(self, at: str) -> dict:
        """查询余额

        Args:
            at: Access Token

        Returns:
            {
                "credits": 920,
                "userPaygateTier": "PAYGATE_TIER_ONE"
            }
        """
        url = f"{self.api_base_url}/credits"
        result = await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at,
            timeout=self._get_control_plane_timeout(),
        )
        return result

    # ========== 图片上传 (使用AT) ==========

    def _detect_image_mime_type(self, image_bytes: bytes) -> str:
        """通过文件头 magic bytes 检测图片 MIME 类型

        Args:
            image_bytes: 图片字节数据

        Returns:
            MIME 类型字符串，默认 image/jpeg
        """
        return self.models.detect_image_mime_type(image_bytes)

    def _convert_to_jpeg(self, image_bytes: bytes) -> bytes:
        """将图片转换为 JPEG 格式

        Args:
            image_bytes: 原始图片字节数据

        Returns:
            JPEG 格式的图片字节数据
        """
        from io import BytesIO
        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        # 如果有透明通道，转换为 RGB
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        
        output = BytesIO()
        img.save(output, format='JPEG', quality=95)
        return output.getvalue()

    async def upload_image(self, *args: Any, **kwargs: Any) -> str:
        return await self.images.upload(*args, **kwargs)

    async def _upload_image_impl(
        self,
        at: str,
        image_bytes: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
        project_id: Optional[str] = None
    ) -> str:
        """上传图片,返回mediaId

        Args:
            at: Access Token
            image_bytes: 图片字节数据
            aspect_ratio: 图片或视频宽高比（会自动转换为图片格式）
            project_id: 项目ID（新上传接口可使用）

        Returns:
            mediaId
        """
        # 转换视频aspect_ratio为图片aspect_ratio
        # VIDEO_ASPECT_RATIO_LANDSCAPE -> IMAGE_ASPECT_RATIO_LANDSCAPE
        # VIDEO_ASPECT_RATIO_PORTRAIT -> IMAGE_ASPECT_RATIO_PORTRAIT
        if aspect_ratio.startswith("VIDEO_"):
            aspect_ratio = aspect_ratio.replace("VIDEO_", "IMAGE_")

        # 自动检测图片 MIME 类型
        mime_type = self._detect_image_mime_type(image_bytes)

        # 编码为base64 (去掉前缀)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')

        # 优先尝试新版上传接口: /v1/flow/uploadImage
        # 若失败则自动回退到旧接口,保证兼容
        ext = "png" if "png" in mime_type else "jpg"
        upload_file_name = f"sub2gen_upload_{int(time.time() * 1000)}.{ext}"
        new_url = f"{self.api_base_url}/flow/uploadImage"
        normalized_project_id = str(project_id or "").strip()
        new_client_context = {
            "sessionId": self._generate_session_id(),
            "tool": "PINHOLE"
        }
        if normalized_project_id:
            new_client_context["projectId"] = normalized_project_id

        new_json_data = {
            "clientContext": new_client_context,
            "fileName": upload_file_name,
            "imageBytes": image_base64,
            "isHidden": False,
            "isUserUploaded": True,
            "mimeType": mime_type
        }

        # 兼容回退：旧接口 :uploadUserImage
        legacy_url = f"{self.api_base_url}:uploadUserImage"
        legacy_json_data = {
            "imageInput": {
                "rawImageBytes": image_base64,
                "mimeType": mime_type,
                "isUserUploaded": True,
                "aspectRatio": aspect_ratio
            },
            "clientContext": {
                "sessionId": self._generate_session_id(),
                "tool": "ASSET_MANAGER"
            }
        }
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                new_result = await self._make_request(
                    method="POST",
                    url=new_url,
                    json_data=new_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True
                )
                media_id = (
                    self._extract_media_name(new_result.get("media"))
                    or new_result.get("mediaGenerationId", {}).get("mediaGenerationId")
                )
                if media_id:
                    return media_id
                raise Exception(f"Invalid upload response: missing media id, keys={list(new_result.keys())}")
            except Exception as new_upload_error:
                last_error = new_upload_error
                retry_reason = "网络超时" if self._is_timeout_error(new_upload_error) else self._get_retry_reason(str(new_upload_error))

                # 旧接口不携带 projectId，带项目上下文的上传一旦回退就可能把图片挂到错误项目。
                if normalized_project_id:
                    if retry_reason and retry_attempt < max_retries - 1:
                        debug_logger.log_warning(
                            f"[UPLOAD] Project-scoped upload 遇到{retry_reason}，准备重试新版接口 "
                            f"({retry_attempt + 2}/{max_retries}, project_id={normalized_project_id})..."
                        )
                        await asyncio.sleep(1)
                        continue
                    raise RuntimeError(
                        "Project-scoped image upload failed via /flow/uploadImage; "
                        "legacy :uploadUserImage fallback is disabled because it may attach media "
                        f"to a different project (project_id={normalized_project_id})."
                    ) from new_upload_error

                debug_logger.log_warning(
                    f"[UPLOAD] New upload API failed, fallback to legacy endpoint: {new_upload_error}"
                )

            try:
                legacy_result = await self._make_request(
                    method="POST",
                    url=legacy_url,
                    json_data=legacy_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True
                )

                media_id = (
                    legacy_result.get("mediaGenerationId", {}).get("mediaGenerationId")
                    or legacy_result.get("media", {}).get("name")
                )
                if media_id:
                    return media_id
                raise Exception(f"Legacy upload response missing media id: keys={list(legacy_result.keys())}")
            except Exception as legacy_upload_error:
                last_error = legacy_upload_error
                retry_reason = self._get_retry_reason(str(legacy_upload_error))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[UPLOAD] 上传遇到{retry_reason}，准备重试 ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("上传图片失败")

    # ========== 图片生成 (使用AT) - 同步返回 ==========

    async def generate_image(self, *args: Any, **kwargs: Any) -> Any:
        return await self.images.generate(*args, **kwargs)

    async def _generate_image_impl(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        image_inputs: Optional[List[Dict]] = None,
        token_id: Optional[int] = None,
        token_image_concurrency: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int], Awaitable[None]]] = None,
        poll_task_progress: PollTaskProgressHook = None,
    ) -> tuple[dict, str, Dict[str, Any]]:
        """生成图片(同步返回)

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_name: NARWHAL / GEM_PIX / GEM_PIX_2 / IMAGEN_3_5
            aspect_ratio: 图片宽高比
            image_inputs: 参考图片列表(图生图时使用)

        Returns:
            (result, session_id, perf_trace)
            result: 上游返回的生成结果
            session_id: 本次成功图片生成请求使用的 sessionId
            perf_trace: 生成重试与链路耗时轨迹
        """
        url = f"{self.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages"

        # 403/reCAPTCHA 重试逻辑
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        perf_trace: Dict[str, Any] = {
            "max_retries": max_retries,
            "generation_attempts": [],
        }
        
        for retry_attempt in range(max_retries):
            attempt_trace: Dict[str, Any] = {
                "attempt": retry_attempt + 1,
                "recaptcha_ok": False,
            }
            attempt_started_at = time.time()
            # 每次重试都重新获取 reCAPTCHA token
            recaptcha_started_at = time.time()
            if progress_callback is not None:
                await progress_callback("solving_image_captcha", 38)
            launch_gate_acquired = False
            launch_ok, launch_queue_ms, launch_stagger_ms = await self._acquire_image_launch_gate(
                token_id=token_id,
                token_image_concurrency=token_image_concurrency,
            )
            attempt_trace["launch_queue_ms"] = launch_queue_ms
            attempt_trace["launch_stagger_ms"] = launch_stagger_ms
            if not launch_ok:
                last_error = Exception("Image launch queue wait timeout")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                raise last_error

            launch_gate_acquired = True
            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_captcha", "captcha_status": "pending"},
                )
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="IMAGE_GENERATION",
                    token_id=token_id,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    poll_task_progress=poll_task_progress,
                )
            finally:
                if launch_gate_acquired:
                    await self._release_image_launch_gate(token_id)
            attempt_trace["recaptcha_ms"] = int((time.time() - recaptcha_started_at) * 1000)
            attempt_trace["recaptcha_ok"] = bool(recaptcha_token)
            if not recaptcha_token:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "job_phase": "generation_captcha",
                        "captcha_status": "token_failed",
                        "captcha_detail": "Failed to obtain reCAPTCHA token",
                    },
                )
                last_error = Exception("Failed to obtain reCAPTCHA token")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] 生成",
                )
                if should_retry:
                    continue
                raise last_error
            await _emit_poll_task_progress(
                poll_task_progress,
                {"job_phase": "generation_submitted", "captcha_status": "token_acquired"},
            )
            if progress_callback is not None:
                await progress_callback("submitting_image", 48)
            session_id = self._generate_session_id()

            # 构建请求 - 新版接口在外层和 requests 内都带 clientContext
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE"
            }

            # 新版图片接口使用结构化提示词 + new media 开关
            request_data = {
                "clientContext": client_context,
                "seed": random.randint(1, 999999),
                "imageModelName": model_name,
                "imageAspectRatio": aspect_ratio,
                "structuredPrompt": {
                    "parts": [{
                        "text": prompt
                    }]
                },
                "imageInputs": image_inputs or []
            }

            json_data = {
                "clientContext": client_context,
                "mediaGenerationContext": {
                    "batchId": str(uuid.uuid4())
                },
                "useNewMedia": True,
                "requests": [request_data]
            }

            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting"},
                )
                self.set_active_generation_token_id(token_id)
                result = await self._make_image_generation_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    attempt_trace=attempt_trace,
                    token_id=token_id,
                )
                attempt_trace["success"] = True
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                perf_trace["final_success_attempt"] = retry_attempt + 1
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"captcha_status": "idle"},
                )
                return result, session_id, perf_trace
            except Exception as e:
                last_error = e
                err_text = str(e)
                status_code = _http_status_from_flow_error(err_text)
                cap = classify_recaptcha_upstream_failure(status_code, err_text)
                if cap:
                    await _emit_poll_task_progress(
                        poll_task_progress,
                        {
                            "job_phase": "generation_awaiting",
                            "captcha_status": cap,
                            "captcha_detail": err_text[:240],
                        },
                    )
                attempt_trace["success"] = False
                attempt_trace["error"] = str(e)[:240]
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] 生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                self.clear_active_generation_token_id()
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        perf_trace["final_success_attempt"] = None
        raise last_error

    async def upsample_image(self, *args: Any, **kwargs: Any) -> Any:
        return await self.images.upsample(*args, **kwargs)

    async def _upsample_image_impl(
        self,
        at: str,
        project_id: str,
        media_id: str,
        target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K",
        user_paygate_tier: str = "PAYGATE_TIER_NOT_PAID",
        session_id: Optional[str] = None,
        token_id: Optional[int] = None,
        poll_task_progress: PollTaskProgressHook = None,
    ) -> str:
        """放大图片到 2K/4K

        Args:
            at: Access Token
            project_id: 项目ID
            media_id: 图片的 mediaId (从 batchGenerateImages 返回的 media[0]["name"])
            target_resolution: UPSAMPLE_IMAGE_RESOLUTION_2K 或 UPSAMPLE_IMAGE_RESOLUTION_4K
            user_paygate_tier: 用户等级 (如 PAYGATE_TIER_NOT_PAID / PAYGATE_TIER_ONE)
            session_id: 可选，复用图片生成请求的 sessionId

        Returns:
            base64 编码的图片数据
        """
        url = f"{self.api_base_url}/flow/upsampleImage"

        # 403/reCAPTCHA/500 重试逻辑 - 使用配置的最大重试次数
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None

        for retry_attempt in range(max_retries):
            await _emit_poll_task_progress(
                poll_task_progress,
                {"job_phase": "upscale_captcha", "captcha_status": "pending"},
            )
            # 获取 reCAPTCHA token - 使用 IMAGE_GENERATION action
            recaptcha_token, browser_id = await self._get_recaptcha_token(
                project_id,
                action="IMAGE_GENERATION",
                token_id=token_id,
                retry_attempt=retry_attempt,
                max_retries=max_retries,
                poll_task_progress=poll_task_progress,
            )
            if not recaptcha_token:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "job_phase": "upscale_captcha",
                        "captcha_status": "token_failed",
                        "captcha_detail": "Failed to obtain reCAPTCHA token",
                    },
                )
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "status": "failed",
                        "job_phase": "failed",
                        "upscale_status": "failed",
                        "captcha_status": "token_failed",
                        "captcha_detail": "Failed to obtain reCAPTCHA token",
                    },
                )
                raise last_error
            upsample_session_id = session_id or self._generate_session_id()

            json_data = {
                "mediaId": media_id,
                "targetResolution": target_resolution,
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": upsample_session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                }
            }

            # 4K/2K 放大使用专用超时，因为返回的 base64 数据量很大
            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "job_phase": "upscale_submitted",
                        "captcha_status": "token_acquired",
                        "upscale_status": "processing",
                    },
                )
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "upscale_awaiting"},
                )
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    timeout=config.upsample_timeout,
                    token_id=token_id,
                )

                # 返回 base64 编码的图片
                encoded_image = result.get("encodedImage", "")
                if not encoded_image:
                    await _emit_poll_task_progress(
                        poll_task_progress,
                        {
                            "status": "failed",
                            "job_phase": "failed",
                            "upscale_status": "failed",
                            "captcha_status": "idle",
                            "captcha_detail": "Upscale response missing encodedImage",
                        },
                    )
                    return ""
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"captcha_status": "idle"},
                )
                return encoded_image
            except Exception as e:
                last_error = e
                err_text = str(e)
                status_code = _http_status_from_flow_error(err_text)
                cap = classify_recaptcha_upstream_failure(status_code, err_text)
                if cap:
                    await _emit_poll_task_progress(
                        poll_task_progress,
                        {
                            "job_phase": "upscale_awaiting",
                            "captcha_status": cap,
                            "captcha_detail": err_text[:240],
                        },
                    )
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "status": "failed",
                        "job_phase": "failed",
                        "upscale_status": "failed",
                        "captcha_status": cap or "idle",
                        "captcha_detail": err_text[:240],
                    },
                )
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        if last_error is not None:
            err_text = str(last_error)
            cap = classify_recaptcha_upstream_failure(_http_status_from_flow_error(err_text), err_text)
            await _emit_poll_task_progress(
                poll_task_progress,
                {
                    "status": "failed",
                    "job_phase": "failed",
                    "upscale_status": "failed",
                    "captcha_status": cap or "idle",
                    "captcha_detail": err_text[:240],
                },
            )
        raise last_error

    # ========== 视频生成 (使用AT) - 异步返回 ==========

    def _build_video_text_input(self, prompt: str, use_v2_model_config: bool = False) -> Dict[str, Any]:
        if use_v2_model_config:
            return {
                "structuredPrompt": {
                    "parts": [{
                        "text": prompt
                    }]
                }
            }
        return {
            "prompt": prompt
        }

    def _extract_media_name(self, media: Any) -> Optional[str]:
        """从新版 media 对象或数组中提取 media id。"""
        if isinstance(media, list):
            for item in media:
                media_name = self._extract_media_name(item)
                if media_name:
                    return media_name
            return None
        if isinstance(media, dict):
            name = media.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def _extract_video_media_id(self, media: Dict[str, Any]) -> Optional[str]:
        if not isinstance(media, dict):
            return None
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        for candidate in (
            media.get("mediaGenerationId"),
            media.get("mediaId"),
            video.get("mediaGenerationId"),
            video.get("mediaId"),
            self._find_nested_string(media.get("mediaMetadata", {}), ("mediaGenerationId", "mediaId")),
            self._extract_media_name(media),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    def _build_video_media_generation_context(self, batch_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "batchId": batch_id or str(uuid.uuid4()),
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
        }

    def _find_nested_string(self, value: Any, keys: tuple[str, ...]) -> Optional[str]:
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for candidate in value.values():
                found = self._find_nested_string(candidate, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_nested_string(item, keys)
                if found:
                    return found
        return None

    def _extract_video_status_from_media(self, media: Dict[str, Any]) -> tuple[Optional[str], Dict[str, Any]]:
        status_block = (
            media.get("mediaMetadata", {}).get("mediaStatus", {})
            or media.get("mediaStatus", {})
            or {}
        )
        status = (
            status_block.get("mediaGenerationStatus")
            or status_block.get("status")
            or media.get("status")
        )
        return status, status_block if isinstance(status_block, dict) else {}

    def _extract_video_url_from_media(self, media: Dict[str, Any]) -> Optional[str]:
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        direct_url_keys = (
            "fifeUrl",
            "videoUrl",
            "outputUri",
            "downloadUri",
            "servingBaseUri",
            "servingUri",
            "mediaUrl",
            "downloadUrl",
        )
        candidates = [
            self._find_nested_string(video, direct_url_keys),
            self._find_nested_string(media, direct_url_keys),
            self._find_nested_string(video, ("uri", "url")),
            self._find_nested_string(media, ("uri", "url")),
        ]
        for candidate in candidates:
            if candidate and (candidate.startswith("http://") or candidate.startswith("https://") or candidate.startswith("/")):
                return candidate
        media_id = self._extract_video_media_id(media)
        if media_id:
            return f"{self.labs_base_url}/trpc/media.getMediaUrlRedirect?name={quote(media_id, safe='')}"
        return None

    def _build_media_url_redirect_endpoint(self, media_id: str) -> str:
        clean_id = (media_id or "").strip()
        if not clean_id:
            raise ValueError("media_id is required")
        return (
            f"{self.labs_base_url}/trpc/media.getMediaUrlRedirect"
            f"?name={quote(clean_id, safe='')}"
        )

    def _build_media_redirect_request_headers(self, st: str) -> Dict[str, str]:
        fingerprint = self._request_fingerprint_ctx.get()
        account_id = (st or "")[:16] or None
        headers: Dict[str, str] = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://labs.google/fx/tools/flow",
            "Origin": "https://labs.google",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cookie": f"__Secure-next-auth.session-token={(st or '').strip()}",
        }
        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            fingerprint_user_agent = fingerprint.get("user_agent")
            if fingerprint.get("accept_language"):
                headers["Accept-Language"] = str(fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = str(fingerprint["sec_ch_ua"])
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = str(fingerprint["sec_ch_ua_mobile"])
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = str(fingerprint["sec_ch_ua_platform"])
        headers["User-Agent"] = fingerprint_user_agent or self._generate_user_agent(account_id)
        return headers

    @staticmethod
    def _sanitize_media_redirect_url_for_log(url: Optional[str]) -> str:
        parsed = urlparse(str(url or "").strip())
        host = parsed.hostname or ""
        if not parsed.scheme or not host:
            return "<unavailable>"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path}"

    @staticmethod
    def _sanitize_media_redirect_headers_for_log(headers: Dict[str, str]) -> Dict[str, str]:
        sanitized: Dict[str, str] = {}
        for key, value in dict(headers or {}).items():
            if str(key).strip().lower() in {"authorization", "cookie", "proxy-authorization"}:
                sanitized[str(key)] = "<redacted>"
            else:
                sanitized[str(key)] = str(value)
        return sanitized

    @classmethod
    def _sanitize_media_redirect_error_for_log(cls, error: Exception) -> str:
        text = str(error or "")

        def sanitize_url(match) -> str:
            return cls._sanitize_media_redirect_url_for_log(match.group(0))

        text = re.sub(r"https?://[^\s\"'<>]+", sanitize_url, text)
        text = re.sub(
            r"(?i)(signature|token|keyname|expires)=([^&\s,]+)",
            r"\1=<redacted>",
            text,
        )
        return text[:500]

    async def _resolve_media_redirect_proxy(self) -> Optional[str]:
        if not self.proxy_manager:
            return None
        try:
            if hasattr(self.proxy_manager, "get_media_proxy_url"):
                return await self.proxy_manager.get_media_proxy_url()
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                return await self.proxy_manager.get_request_proxy_url()
            if hasattr(self.proxy_manager, "get_proxy_url"):
                return await self.proxy_manager.get_proxy_url()
        except Exception as exc:
            debug_logger.log_warning(f"[MEDIA REDIRECT] proxy resolve failed: {exc}")
        return None

    @staticmethod
    def _extract_redirect_location(response) -> Optional[str]:
        location = response.headers.get("location") or response.headers.get("Location")
        if isinstance(location, str) and location.strip():
            return location.strip()
        return None

    @staticmethod
    def _is_allowed_flow_media_url(url: str) -> bool:
        if not isinstance(url, str) or not url.strip():
            return False
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        if host == "flow-content.google":
            return True
        if host.endswith(".googleusercontent.com"):
            return True
        return False

    async def _fetch_media_redirect_location(
        self,
        redirect_url: str,
        headers: Dict[str, str],
        proxy_url: Optional[str],
    ) -> str:
        last_error: Optional[Exception] = None
        for client_name, request_fn in (
            ("curl_cffi", self._fetch_media_redirect_location_curl),
            ("httpx", self._fetch_media_redirect_location_httpx),
        ):
            attempt_started = time.perf_counter()
            try:
                status_code, location = await request_fn(redirect_url, headers, proxy_url)
                duration_ms = (time.perf_counter() - attempt_started) * 1000
                safe_location = self._sanitize_media_redirect_url_for_log(location)
                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=status_code,
                        headers={"Location": safe_location},
                        body={
                            "transport": client_name,
                            "redirect_received": bool(location),
                        },
                        duration_ms=duration_ms,
                    )
                if status_code in (301, 302, 303, 307, 308):
                    if location and self._is_allowed_flow_media_url(location):
                        return location
                    raise Exception(
                        f"Media redirect returned HTTP {status_code} without a valid CDN location"
                    )
                if status_code == 200 and location and self._is_allowed_flow_media_url(location):
                    return location
                if status_code in (401, 403):
                    raise Exception(
                        f"Media redirect rejected by Flow media endpoint (HTTP {status_code})"
                    )
                raise Exception(f"Media redirect failed with HTTP {status_code}")
            except Exception as exc:
                last_error = exc
                duration_ms = (time.perf_counter() - attempt_started) * 1000
                safe_error = self._sanitize_media_redirect_error_for_log(exc)
                debug_logger.log_warning(
                    f"[MEDIA REDIRECT] {client_name} failed for {urlparse(redirect_url).hostname} "
                    f"after {duration_ms:.2f}ms: {safe_error}"
                )
                debug_logger.log_error(
                    f"[MEDIA REDIRECT] request failed: transport={client_name}, "
                    f"duration_ms={duration_ms:.2f}, error={safe_error}"
                )
        if last_error is not None:
            raise last_error
        raise RuntimeError("Media redirect resolution failed")

    async def _fetch_media_redirect_location_curl(
        self,
        redirect_url: str,
        headers: Dict[str, str],
        proxy_url: Optional[str],
    ) -> tuple[int, Optional[str]]:
        async with AsyncSession(trust_env=False) as session:
            response = await session.get(
                redirect_url,
                headers=headers,
                proxy=proxy_url,
                timeout=self.timeout,
                impersonate="chrome124",
                allow_redirects=False,
                verify=False,
            )
            return response.status_code, self._extract_redirect_location(response)

    async def _fetch_media_redirect_location_httpx(
        self,
        redirect_url: str,
        headers: Dict[str, str],
        proxy_url: Optional[str],
    ) -> tuple[int, Optional[str]]:
        if httpx is None:
            raise RuntimeError("httpx is not installed")
        timeout = httpx.Timeout(float(self.timeout or 60), connect=30.0)
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            verify=False,
            proxy=proxy_url,
        ) as client:
            response = await client.get(redirect_url, headers=headers)
            return response.status_code, self._extract_redirect_location(response)

    async def _resolve_media_redirect_via_extension(
        self,
        redirect_url: str,
        st: str,
        token_id: Optional[int],
    ) -> str:
        headers = self._build_media_redirect_request_headers(st)
        managed_api_key_id = self.get_managed_api_key_id()
        result = await self.extension_generation_service.submit_generation(
            url=redirect_url,
            method="GET",
            headers=headers,
            json_data={},
            timeout_seconds=int(self.timeout or 60),
            token_id=token_id,
            managed_api_key_id=managed_api_key_id,
        )
        for key in ("redirect_url", "final_url", "url", "location"):
            candidate = result.get(key)
            if isinstance(candidate, str) and self._is_allowed_flow_media_url(candidate):
                return candidate
        response_headers = result.get("response_headers")
        if isinstance(response_headers, dict):
            location = response_headers.get("location") or response_headers.get("Location")
            if isinstance(location, str) and self._is_allowed_flow_media_url(location):
                return location
        raise Exception("Extension media redirect did not return a CDN location")

    async def resolve_media_download_url(
        self,
        media_id: str,
        st: str,
        at: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> str:
        """Resolve a Flow media ID to a signed CDN download URL."""
        redirect_url = self._build_media_url_redirect_endpoint(media_id)
        headers = self._build_media_redirect_request_headers(st)
        proxy_url = await self._resolve_media_redirect_proxy()
        fingerprint = self._request_fingerprint_ctx.get()
        if isinstance(fingerprint, dict) and fingerprint.get("proxy_url") is not None:
            proxy_url = fingerprint.get("proxy_url") or None

        total_started = time.perf_counter()
        debug_logger.log_info(
            f"[MEDIA REDIRECT] resolving media_id={media_id} via ST cookie "
            f"(proxy={_proxy_endpoint_for_log(proxy_url)})"
        )
        if config.debug_enabled:
            debug_logger.log_request(
                method="GET",
                url=self._sanitize_media_redirect_url_for_log(redirect_url),
                headers=self._sanitize_media_redirect_headers_for_log(headers),
                proxy=_proxy_endpoint_for_log(proxy_url),
            )

        try:
            location = await self._fetch_media_redirect_location(redirect_url, headers, proxy_url)
            duration_ms = (time.perf_counter() - total_started) * 1000
            debug_logger.log_info(
                f"[MEDIA REDIRECT] resolved media_id={media_id}, transport=server, "
                f"location={self._sanitize_media_redirect_url_for_log(location)}, "
                f"duration_ms={duration_ms:.2f}"
            )
            return location
        except Exception as primary_error:
            safe_primary_error = self._sanitize_media_redirect_error_for_log(primary_error)
            if token_id is not None and await self._token_allows_extension_generation(token_id):
                try:
                    debug_logger.log_warning(
                        "[MEDIA REDIRECT] server redirect failed, trying extension GET: "
                        f"{safe_primary_error}"
                    )
                    location = await self._resolve_media_redirect_via_extension(
                        redirect_url,
                        st,
                        token_id,
                    )
                    duration_ms = (time.perf_counter() - total_started) * 1000
                    debug_logger.log_info(
                        f"[MEDIA REDIRECT] resolved media_id={media_id}, transport=extension, "
                        f"location={self._sanitize_media_redirect_url_for_log(location)}, "
                        f"duration_ms={duration_ms:.2f}"
                    )
                    return location
                except Exception as ext_error:
                    debug_logger.log_error(
                        "[MEDIA REDIRECT] extension fallback failed: "
                        f"{self._sanitize_media_redirect_error_for_log(ext_error)}"
                    )
            duration_ms = (time.perf_counter() - total_started) * 1000
            debug_logger.log_error(
                f"[MEDIA REDIRECT] resolution failed: media_id={media_id}, "
                f"duration_ms={duration_ms:.2f}, "
                f"error={safe_primary_error}"
            )
            raise Exception(
                f"Failed to resolve media download URL: {primary_error}"
            ) from primary_error

    async def get_media_url_redirect(self, st: str, media_name: str) -> str:
        """Compatibility wrapper for the upstream Flow media redirect method."""
        normalized_media_name = str(media_name or "").strip()
        if not normalized_media_name:
            raise ValueError("get_media_url_redirect: media_name is required")

        normalized_st = str(st or "").strip()
        if not normalized_st:
            raise ValueError("get_media_url_redirect: ST token is required")

        return await self.resolve_media_download_url(
            media_id=normalized_media_name,
            st=normalized_st,
        )

    def _media_to_video_operation(
        self,
        media: Dict[str, Any],
        fallback_project_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(media, dict):
            return None

        media_name = self._extract_media_name(media)
        video_media_id = self._extract_video_media_id(media) or media_name
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        video_operation = video.get("operation") if isinstance(video.get("operation"), dict) else {}
        operation_name = (
            video_operation.get("name")
            or self._find_nested_string(video_operation, ("name",))
            or media_name
        )
        if not operation_name:
            return None

        project_id = media.get("projectId") or fallback_project_id
        status, status_block = self._extract_video_status_from_media(media)
        operation: Dict[str, Any] = {
            "operation": {
                "name": operation_name,
            },
            "status": status or "MEDIA_GENERATION_STATUS_PENDING",
        }
        if media_name:
            operation["mediaName"] = media_name
        if project_id:
            operation["projectId"] = project_id

        scene_id = (
            media.get("sceneId")
            or media.get("workflowStepId")
            or video_operation.get("sceneId")
        )
        if scene_id:
            operation["sceneId"] = scene_id

        video_url = self._extract_video_url_from_media(media)
        aspect_ratio = (
            self._find_nested_string(video, ("aspectRatio", "videoAspectRatio"))
            or self._find_nested_string(media.get("mediaMetadata", {}), ("videoAspectRatio", "aspectRatio"))
        )
        video_metadata: Dict[str, Any] = {}
        if video_url:
            video_metadata["fifeUrl"] = video_url
        if video_media_id:
            video_metadata["mediaGenerationId"] = video_media_id
        if aspect_ratio:
            video_metadata["aspectRatio"] = aspect_ratio
        if video_metadata:
            operation["operation"]["metadata"] = {"video": video_metadata}

        error = status_block.get("error") if isinstance(status_block, dict) else None
        if isinstance(error, dict):
            operation["operation"]["error"] = error

        return operation

    def _merge_video_operations_with_media(
        self,
        operations: List[Dict[str, Any]],
        media_operations: List[Dict[str, Any]],
        fallback_project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        media_by_name: Dict[str, Dict[str, Any]] = {}
        for item in media_operations:
            media_name = item.get("mediaName") or (item.get("operation") or {}).get("name")
            if media_name:
                media_by_name[media_name] = item

        merged: List[Dict[str, Any]] = []
        for raw_operation in operations:
            operation = dict(raw_operation) if isinstance(raw_operation, dict) else {}
            operation_body = dict(operation.get("operation") or {})
            operation["operation"] = operation_body
            name = operation_body.get("name") or operation.get("mediaName")
            media_operation = media_by_name.get(name) if name else None
            if media_operation:
                operation.setdefault("mediaName", media_operation.get("mediaName"))
                operation.setdefault("projectId", media_operation.get("projectId"))
                operation.setdefault("status", media_operation.get("status"))
                operation.setdefault("sceneId", media_operation.get("sceneId"))
                if "metadata" not in operation_body and (media_operation.get("operation") or {}).get("metadata"):
                    operation_body["metadata"] = (media_operation.get("operation") or {}).get("metadata")
                if "error" not in operation_body and (media_operation.get("operation") or {}).get("error"):
                    operation_body["error"] = (media_operation.get("operation") or {}).get("error")
            elif fallback_project_id:
                operation.setdefault("projectId", fallback_project_id)
            merged.append(operation)

        return merged

    def _normalize_video_generation_response(
        self,
        result: Dict[str, Any],
        fallback_project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return result

        normalized = dict(result)
        media_items = normalized.get("media")
        media_operations: List[Dict[str, Any]] = []
        if isinstance(media_items, list):
            for media in media_items:
                operation = self._media_to_video_operation(media, fallback_project_id=fallback_project_id)
                if operation:
                    media_operations.append(operation)

        operations = normalized.get("operations")
        if isinstance(operations, list) and operations:
            normalized["operations"] = self._merge_video_operations_with_media(
                operations,
                media_operations,
                fallback_project_id=fallback_project_id,
            )
        elif media_operations:
            normalized["operations"] = media_operations

        return normalized

    def _operations_to_media_refs(
        self,
        operations: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        media_refs: List[Dict[str, str]] = []
        for operation in operations or []:
            if not isinstance(operation, dict):
                continue
            operation_body = operation.get("operation") or {}
            media_name = (
                operation.get("mediaName")
                or operation.get("name")
                or operation_body.get("name")
            )
            project_id = (
                operation.get("projectId")
                or operation.get("project_id")
                or operation_body.get("projectId")
            )
            if isinstance(media_name, str) and media_name.strip() and isinstance(project_id, str) and project_id.strip():
                media_refs.append({
                    "name": media_name.strip(),
                    "projectId": project_id.strip(),
                })
        return media_refs

    async def generate_video_text(self, *args: Any, **kwargs: Any) -> Any:
        return await self.videos.call("generate_video_text", *args, **kwargs)

    async def _generate_video_text_impl(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        poll_task_progress: PollTaskProgressHook = None,
    ) -> dict:
        """文生视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_t2v_fast 等
            aspect_ratio: 视频宽高比
            user_paygate_tier: 用户等级

        Returns:
            {
                "operations": [{
                    "operation": {"name": "task_id"},
                    "sceneId": "uuid",
                    "status": "MEDIA_GENERATION_STATUS_PENDING"
                }],
                "remainingCredits": 900
            }
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        # 403/reCAPTCHA 重试逻辑 - 使用配置的最大重试次数
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_captcha", "captcha_status": "pending"},
                )
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    poll_task_progress=poll_task_progress,
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "job_phase": "generation_captcha",
                        "captcha_status": "token_failed",
                        "captcha_detail": "Failed to obtain reCAPTCHA token",
                    },
                )
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] 生成",
                )
                if should_retry:
                    continue
                raise last_error
            await _emit_poll_task_progress(
                poll_task_progress,
                {"job_phase": "generation_submitted", "captcha_status": "token_acquired"},
            )
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier
            }
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": random.randint(1, 99999),
                "textInput": self._build_video_text_input(prompt, use_v2_model_config=use_v2_model_config),
                "videoModelKey": model_key,
                "metadata": {
                    "sceneId": scene_id
                }
            }
            json_data = {
                "clientContext": client_context,
                "requests": [request_data]
            }
            if use_v2_model_config:
                json_data["mediaGenerationContext"] = self._build_video_media_generation_context()
                json_data["useV2ModelConfig"] = True

            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting"},
                )
                self.set_active_generation_token_id(token_id)
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    token_id=token_id,
                )
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting", "captcha_status": "idle"},
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                err_text = str(e)
                status_code = _http_status_from_flow_error(err_text)
                cap = classify_recaptcha_upstream_failure(status_code, err_text)
                if cap:
                    await _emit_poll_task_progress(
                        poll_task_progress,
                        {
                            "job_phase": "generation_awaiting",
                            "captcha_status": cap,
                            "captcha_detail": err_text[:240],
                        },
                    )
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] 生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        raise last_error

    async def generate_video_reference_images(self, *args: Any, **kwargs: Any) -> Any:
        return await self.videos.call("generate_video_reference_images", *args, **kwargs)

    async def _generate_video_reference_images_impl(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        reference_images: List[Dict],
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        poll_task_progress: PollTaskProgressHook = None,
    ) -> dict:
        """图生视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_r2v_fast_landscape
            aspect_ratio: 视频宽高比
            reference_images: 参考图片列表 [{"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": "..."}]
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoReferenceImages"

        # 403/reCAPTCHA 重试逻辑 - 使用配置的最大重试次数
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_captcha", "captcha_status": "pending"},
                )
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    poll_task_progress=poll_task_progress,
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "job_phase": "generation_captcha",
                        "captcha_status": "token_failed",
                        "captcha_detail": "Failed to obtain reCAPTCHA token",
                    },
                )
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] 生成",
                )
                if should_retry:
                    continue
                raise last_error
            await _emit_poll_task_progress(
                poll_task_progress,
                {"job_phase": "generation_submitted", "captcha_status": "token_acquired"},
            )
            session_id = self._generate_session_id()
            batch_id = str(uuid.uuid4())
            scene_id = str(uuid.uuid4())

            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(batch_id),
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "structuredPrompt": {
                            "parts": [{
                                "text": prompt
                            }]
                        }
                    },
                    "videoModelKey": model_key,
                    "referenceImages": reference_images,
                    "metadata": {
                        "sceneId": scene_id
                    }
                }],
                "useV2ModelConfig": True
            }

            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting"},
                )
                self.set_active_generation_token_id(token_id)
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    token_id=token_id,
                )
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting", "captcha_status": "idle"},
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                err_text = str(e)
                status_code = _http_status_from_flow_error(err_text)
                cap = classify_recaptcha_upstream_failure(status_code, err_text)
                if cap:
                    await _emit_poll_task_progress(
                        poll_task_progress,
                        {
                            "job_phase": "generation_awaiting",
                            "captcha_status": cap,
                            "captcha_detail": err_text[:240],
                        },
                    )
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] 生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        raise last_error

    async def generate_video_start_end(self, *args: Any, **kwargs: Any) -> Any:
        return await self.videos.call("generate_video_start_end", *args, **kwargs)

    async def _generate_video_start_end_impl(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        end_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        poll_task_progress: PollTaskProgressHook = None,
    ) -> dict:
        """收尾帧生成视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_i2v_s_fast_fl
            aspect_ratio: 视频宽高比
            start_media_id: 起始帧mediaId
            end_media_id: 结束帧mediaId
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartAndEndImage"

        # 403/reCAPTCHA 重试逻辑 - 使用配置的最大重试次数
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_captcha", "captcha_status": "pending"},
                )
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    poll_task_progress=poll_task_progress,
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "job_phase": "generation_captcha",
                        "captcha_status": "token_failed",
                        "captcha_detail": "Failed to obtain reCAPTCHA token",
                    },
                )
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首尾帧生成",
                )
                if should_retry:
                    continue
                raise last_error
            await _emit_poll_task_progress(
                poll_task_progress,
                {"job_phase": "generation_submitted", "captcha_status": "token_acquired"},
            )
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier
            }
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": random.randint(1, 99999),
                "textInput": self._build_video_text_input(prompt, use_v2_model_config=use_v2_model_config),
                "videoModelKey": model_key,
                "startImage": {
                    "mediaId": start_media_id
                },
                "endImage": {
                    "mediaId": end_media_id
                },
                "metadata": {
                    "sceneId": scene_id
                }
            }
            json_data = {
                "clientContext": client_context,
                "requests": [request_data]
            }
            if use_v2_model_config:
                json_data["mediaGenerationContext"] = self._build_video_media_generation_context()
                json_data["useV2ModelConfig"] = True

            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting"},
                )
                self.set_active_generation_token_id(token_id)
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    token_id=token_id,
                )
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting", "captcha_status": "idle"},
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                err_text = str(e)
                status_code = _http_status_from_flow_error(err_text)
                cap = classify_recaptcha_upstream_failure(status_code, err_text)
                if cap:
                    await _emit_poll_task_progress(
                        poll_task_progress,
                        {
                            "job_phase": "generation_awaiting",
                            "captcha_status": cap,
                            "captcha_detail": err_text[:240],
                        },
                    )
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首尾帧生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        raise last_error

    async def generate_video_start_image(self, *args: Any, **kwargs: Any) -> Any:
        return await self.videos.call("generate_video_start_image", *args, **kwargs)

    async def _generate_video_start_image_impl(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        poll_task_progress: PollTaskProgressHook = None,
    ) -> dict:
        """仅首帧生成视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_i2v_s_fast_fl等
            aspect_ratio: 视频宽高比
            start_media_id: 起始帧mediaId
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartImage"

        # 403/reCAPTCHA 重试逻辑 - 使用配置的最大重试次数
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_captcha", "captcha_status": "pending"},
                )
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    poll_task_progress=poll_task_progress,
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "job_phase": "generation_captcha",
                        "captcha_status": "token_failed",
                        "captcha_detail": "Failed to obtain reCAPTCHA token",
                    },
                )
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首帧生成",
                )
                if should_retry:
                    continue
                raise last_error
            await _emit_poll_task_progress(
                poll_task_progress,
                {"job_phase": "generation_submitted", "captcha_status": "token_acquired"},
            )
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier
            }
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": random.randint(1, 99999),
                "textInput": self._build_video_text_input(prompt, use_v2_model_config=use_v2_model_config),
                "videoModelKey": model_key,
                "startImage": {
                    "mediaId": start_media_id
                },
                # 注意: 没有endImage字段,只用首帧
                "metadata": {
                    "sceneId": scene_id
                }
            }
            json_data = {
                "clientContext": client_context,
                "requests": [request_data]
            }
            if use_v2_model_config:
                json_data["mediaGenerationContext"] = self._build_video_media_generation_context()
                json_data["useV2ModelConfig"] = True

            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting"},
                )
                self.set_active_generation_token_id(token_id)
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    token_id=token_id,
                )
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting", "captcha_status": "idle"},
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                err_text = str(e)
                status_code = _http_status_from_flow_error(err_text)
                cap = classify_recaptcha_upstream_failure(status_code, err_text)
                if cap:
                    await _emit_poll_task_progress(
                        poll_task_progress,
                        {
                            "job_phase": "generation_awaiting",
                            "captcha_status": cap,
                            "captcha_detail": err_text[:240],
                        },
                    )
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首帧生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        raise last_error

    async def generate_video_extend(self, *args: Any, **kwargs: Any) -> Any:
        return await self.videos.call("generate_video_extend", *args, **kwargs)

    async def _generate_video_extend_impl(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        video_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        poll_task_progress: PollTaskProgressHook = None,
    ) -> dict:
        """视频续写,基于已生成的视频延伸7秒"""
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoExtendVideo"
        max_retries = 3
        last_error = None
        for retry_attempt in range(max_retries):
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error
            launch_gate_acquired = True
            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_captcha", "captcha_status": "pending"},
                )
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                    poll_task_progress=poll_task_progress,
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "job_phase": "generation_captcha",
                        "captcha_status": "token_failed",
                        "captcha_detail": "Failed to obtain reCAPTCHA token",
                    },
                )
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] 续写",
                )
                if should_retry:
                    continue
                raise last_error
            await _emit_poll_task_progress(
                poll_task_progress,
                {"job_phase": "generation_submitted", "captcha_status": "token_acquired"},
            )
            session_id = self._generate_session_id()
            workflow_id = str(uuid.uuid4())
            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier,
                },
                "mediaGenerationContext": self._build_video_media_generation_context(),
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
                    "videoInput": {"mediaId": video_media_id},
                    "videoModelKey": model_key,
                    "metadata": {"workflowId": workflow_id},
                }],
                "useV2ModelConfig": True,
            }
            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting"},
                )
                self.set_active_generation_token_id(token_id)
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    token_id=token_id,
                )
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "generation_awaiting", "captcha_status": "idle"},
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                err_text = str(e)
                status_code = _http_status_from_flow_error(err_text)
                cap = classify_recaptcha_upstream_failure(status_code, err_text)
                if cap:
                    await _emit_poll_task_progress(
                        poll_task_progress,
                        {
                            "job_phase": "generation_awaiting",
                            "captcha_status": cap,
                            "captcha_detail": err_text[:240],
                        },
                    )
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] 续写",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        raise last_error

    async def run_concatenation(self, *args: Any, **kwargs: Any) -> Any:
        return await self.videos.call("run_concatenation", *args, **kwargs)

    async def _run_concatenation_impl(
        self,
        at: str,
        original_media_id: str,
        extend_media_id: str,
    ) -> dict:
        """调用 Google runVideoFxConcatenation API 拼接视频"""
        url = f"{self.api_base_url}:runVideoFxConcatenation"
        json_data = {
            "inputVideos": [
                {
                    "mediaGenerationId": original_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "0s",
                    "endTimeOffset": "8s",
                },
                {
                    "mediaGenerationId": extend_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "1s",
                    "endTimeOffset": "8s",
                },
            ]
        }
        return await self._make_video_api_request(
            url=url,
            json_data=json_data,
            at=at,
            timeout=self._get_video_submit_timeout(),
            token_id=None,
        )

    async def poll_concatenation_status(self, *args: Any, **kwargs: Any) -> Any:
        return await self.videos.call("poll_concatenation_status", *args, **kwargs)

    async def _poll_concatenation_status_impl(
        self,
        at: str,
        operation_name: str,
        timeout: int = 300,
        poll_interval: int = 3,
    ) -> dict:
        """轮询拼接任务状态，直到完成或超时"""
        url = f"{self.api_base_url}:runVideoFxCheckConcatenationStatus"
        json_data = {"operation": {"operation": {"name": operation_name}}}
        start_time = time.time()
        while time.time() - start_time < timeout:
            result = await self._make_video_api_request(
                url=url,
                json_data=json_data,
                at=at,
                timeout=self._get_video_poll_timeout(),
                token_id=self.get_active_generation_token_id(),
            )
            status = result.get("status", "")
            output_uri = result.get("outputUri", "")
            encoded_video = result.get("encodedVideo", "")
            if output_uri:
                return result
            if encoded_video and "SUCCESSFUL" in status:
                video_bytes = base64.b64decode(encoded_video)
                video_filename = f"concat_{uuid.uuid4().hex[:12]}.mp4"
                save_dir = get_runtime_tmp_dir()
                save_dir.mkdir(parents=True, exist_ok=True)
                save_path = save_dir / video_filename
                with open(save_path, "wb") as f:
                    f.write(video_bytes)
                result["outputUri"] = f"/tmp/{video_filename}"
                result["local_file"] = str(save_path)
                return result
            if "FAILED" in status or "ERROR" in status:
                raise Exception(f"视频拼接失败: {status}")
            await asyncio.sleep(poll_interval)
        raise Exception(f"视频拼接超时 ({timeout}s)")

    # ========== 视频放大 (Video Upsampler) ==========

    async def upsample_video(self, *args: Any, **kwargs: Any) -> Any:
        return await self.videos.call("upsample_video", *args, **kwargs)

    async def _upsample_video_impl(
        self,
        at: str,
        project_id: str,
        video_media_id: str,
        aspect_ratio: str,
        resolution: str,
        model_key: str,
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        poll_task_progress: PollTaskProgressHook = None,
    ) -> dict:
        """视频放大到 4K/1080P，返回 task_id

        Args:
            at: Access Token
            project_id: 项目ID
            video_media_id: 视频的 mediaId
            aspect_ratio: 视频宽高比 VIDEO_ASPECT_RATIO_PORTRAIT/LANDSCAPE
            resolution: VIDEO_RESOLUTION_4K 或 VIDEO_RESOLUTION_1080P
            model_key: veo_3_1_upsampler_4k 或 veo_3_1_upsampler_1080p

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoUpsampleVideo"

        # 403/reCAPTCHA 重试逻辑 - 使用配置的最大重试次数
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        
        for retry_attempt in range(max_retries):
            await _emit_poll_task_progress(
                poll_task_progress,
                {"job_phase": "upscale_captcha", "captcha_status": "pending"},
            )
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    poll_task_progress=poll_task_progress,
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "job_phase": "upscale_captcha",
                        "captcha_status": "token_failed",
                        "captcha_detail": "Failed to obtain reCAPTCHA token",
                    },
                )
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise last_error
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "resolution": resolution,
                    "seed": random.randint(1, 99999),
                    "videoInput": {
                        "mediaId": video_media_id
                    },
                    "videoModelKey": model_key,
                    "metadata": {
                        "sceneId": scene_id
                    }
                }],
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                }
            }

            try:
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "job_phase": "upscale_submitted",
                        "captcha_status": "token_acquired",
                        "upscale_status": "processing",
                    },
                )
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"job_phase": "upscale_awaiting"},
                )
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    token_id=token_id,
                )
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {"captcha_status": "idle"},
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                err_text = str(e)
                status_code = _http_status_from_flow_error(err_text)
                cap = classify_recaptcha_upstream_failure(status_code, err_text)
                if cap:
                    await _emit_poll_task_progress(
                        poll_task_progress,
                        {
                            "job_phase": "upscale_awaiting",
                            "captcha_status": cap,
                            "captcha_detail": err_text[:240],
                        },
                    )
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        raise last_error

    # ========== 任务轮询 (使用AT) ==========

    async def check_video_status(self, *args: Any, **kwargs: Any) -> Any:
        return await self.videos.call("check_video_status", *args, **kwargs)

    async def _check_video_status_impl(self, at: str, operations: List[Dict]) -> dict:
        """查询视频生成状态

        Args:
            at: Access Token
            operations: 操作列表 [{"operation": {"name": "task_id"}, "sceneId": "...", "status": "..."}]

        Returns:
            {
                "operations": [{
                    "operation": {
                        "name": "task_id",
                        "metadata": {...}  # 完成时包含视频信息
                    },
                    "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                }]
            }
        """
        url = f"{self.api_base_url}/video:batchCheckAsyncVideoGenerationStatus"

        media_refs = self._operations_to_media_refs(operations)
        json_data = {"media": media_refs} if media_refs else {"operations": operations}
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_poll_timeout(),
                    token_id=self.get_active_generation_token_id(),
                )
                return self._normalize_video_generation_response(result)
            except Exception as e:
                if media_refs:
                    try:
                        result = await self._make_video_api_request(
                            url=url,
                            json_data={"operations": operations},
                            at=at,
                            timeout=self._get_video_poll_timeout(),
                            token_id=self.get_active_generation_token_id(),
                        )
                        return self._normalize_video_generation_response(result)
                    except Exception:
                        pass
                last_error = e
                retry_reason = self._get_retry_reason(str(e))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[VIDEO POLL] 状态查询遇到{retry_reason}，准备重试 ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("视频状态查询失败")

    async def check_video_status_via_extension_poll(self, *args: Any, **kwargs: Any) -> Any:
        return await self.videos.call("check_video_status_via_extension_poll", *args, **kwargs)

    async def _check_video_status_via_extension_poll_impl(self, at: str, operations: List[Dict]) -> dict:
        """Fallback video poll path using extension browser context."""
        routing_token_id = self.get_active_generation_token_id()
        if not await self._token_allows_extension_generation(routing_token_id):
            return await self.check_video_status(at, operations)
        url = f"{self.api_base_url}/video:batchCheckAsyncVideoGenerationStatus"
        headers = {
            "Authorization": f"Bearer {at}",
            "Content-Type": "application/json;charset=utf-8",
            "Origin": self.labs_base_url,
            "Referer": f"{self.labs_base_url}/",
        }
        managed_api_key_id = self.get_managed_api_key_id()
        routing_token_id = self.get_active_generation_token_id()
        media_refs = self._operations_to_media_refs(operations)
        json_data = {"media": media_refs} if media_refs else {"operations": operations}
        try:
            result = await self.extension_generation_service.poll_generation(
                url=url,
                method="POST",
                headers=headers,
                json_data=json_data,
                timeout_seconds=self._get_video_poll_timeout(),
                token_id=routing_token_id,
                managed_api_key_id=managed_api_key_id,
            )
            return self._normalize_video_generation_response(result)
        except Exception:
            if media_refs:
                result = await self.extension_generation_service.poll_generation(
                    url=url,
                    method="POST",
                    headers=headers,
                    json_data={"operations": operations},
                    timeout_seconds=self._get_video_poll_timeout(),
                    token_id=routing_token_id,
                    managed_api_key_id=managed_api_key_id,
                )
                return self._normalize_video_generation_response(result)
            raise

    # ========== 媒体删除 (使用ST) ==========

    async def delete_media(self, st: str, media_names: List[str]):
        """删除媒体

        Args:
            st: Session Token
            media_names: 媒体ID列表
        """
        url = f"{self.labs_base_url}/trpc/media.deleteMedia"
        json_data = {
            "json": {
                "names": media_names
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st
        )

    # ========== 辅助方法 ==========

    def _build_browser_style_control_headers(
        self,
        referer: str,
        origin: Optional[str] = None,
        account_id: Optional[str] = None,
        content_type: Optional[str] = None,
        accept_language: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": referer,
            "User-Agent": self._get_effective_request_user_agent(account_id),
            "Accept-Language": accept_language or self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9"),
            "Priority": "u=1, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }
        if origin:
            headers["Origin"] = origin
            if origin == "https://labs.google":
                headers.setdefault("sec-fetch-storage-access", "active")
        if content_type:
            headers["Content-Type"] = content_type
        if api_key:
            headers["x-goog-api-key"] = api_key
        headers.setdefault("x-browser-channel", self.FLOW_BROWSER_CHANNEL_HEADER)
        headers.setdefault("x-browser-copyright", self.FLOW_BROWSER_COPYRIGHT_HEADER)
        headers.setdefault("x-browser-validation", self.FLOW_BROWSER_VALIDATION_HEADER)
        headers.setdefault("x-browser-year", self.FLOW_BROWSER_YEAR_HEADER)
        return headers

    async def _labs_trpc_get_with_st(
        self,
        path_with_query: str,
        st: str,
        project_id: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await self._make_request(
            "GET",
            f"{self.labs_base_url}/trpc/{path_with_query}",
            headers=self._build_browser_style_control_headers(
                self._build_flow_project_page_url(project_id),
                account_id=st[:16],
                content_type="application/json",
            ),
            use_st=True,
            st_token=st,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _labs_trpc_post_with_st(
        self,
        trpc_path: str,
        payload: Dict[str, Any],
        st: str,
        project_id: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await self._make_request(
            "POST",
            f"{self.labs_base_url}/trpc/{trpc_path}",
            headers=self._build_browser_style_control_headers(
                self._build_flow_project_page_url(project_id),
                origin="https://labs.google",
                account_id=st[:16],
                content_type="application/json",
            ),
            json_data=payload,
            use_st=True,
            st_token=st,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _aisandbox_request(
        self,
        method: str,
        path: str,
        at: Optional[str],
        *,
        json_data: Optional[Dict[str, Any]] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        content_type: Optional[str] = "text/plain;charset=UTF-8",
        accept_language: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._make_request(
            method,
            f"{self.api_base_url}{path}",
            headers=self._build_browser_style_control_headers(
                "https://labs.google/",
                origin="https://labs.google",
                account_id=account_id,
                content_type=content_type,
                accept_language=accept_language,
                api_key=api_key,
            ),
            json_data=json_data,
            raw_body=raw_body,
            use_at=bool(at),
            at_token=at,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _warmup_flow_video_frontend_context(
        self,
        *,
        at: str,
        project_id: str,
        token_id: Optional[int],
        session_id: str,
        user_paygate_tier: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
    ) -> None:
        page_url = self._build_flow_project_page_url(project_id)
        session_create_time = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        st = await self._get_token_st_by_id(token_id)
        if st:
            null_input = self._encode_trpc_input({"json": None, "meta": {"values": ["undefined"]}})
            paths = (
                f"general.fetchUserPreferences?input={null_input}",
                f"videoFx.getFlowAppConfig?input={null_input}",
                f"videoFx.getUserSettings?input={null_input}",
            )
            for path in paths:
                try:
                    await self._labs_trpc_get_with_st(path, st, project_id)
                except Exception as exc:
                    debug_logger.log_warning(f"[VIDEO WARMUP] Labs initialization failed ({path}): {exc}")
            batch_log_payload = {
                "json": {
                    "appEvents": [
                        {
                            "event": "PAGE_VIEW",
                            "eventProperties": [
                                {"key": "URL", "stringValue": page_url},
                                {
                                    "key": "USER_AGENT",
                                    "stringValue": self._get_effective_request_user_agent(st[:16]),
                                },
                                {"key": "IS_DESKTOP"},
                            ],
                            "activeExperiments": [],
                            "eventMetadata": {"sessionId": session_id},
                            "eventTime": session_create_time,
                        }
                    ]
                }
            }
            try:
                await self._labs_trpc_post_with_st(
                    "general.submitBatchLog",
                    batch_log_payload,
                    st,
                    project_id,
                )
            except Exception as exc:
                debug_logger.log_warning(f"[VIDEO WARMUP] Labs submitBatchLog failed: {exc}")
        try:
            await self._aisandbox_request(
                "POST",
                ":checkAppAvailability",
                at=None,
                raw_body=self._compact_json_dumps({"clientContext": {"tool": "PINHOLE"}}),
                api_key=self.FLOW_PUBLIC_API_KEY,
                account_id=at[:16] if at else None,
            )
        except Exception as exc:
            debug_logger.log_warning(f"[VIDEO WARMUP] checkAppAvailability failed: {exc}")

    @staticmethod
    def _video_aspect_ratio_to_agent_aspect_ratio(aspect_ratio: str) -> str:
        return {
            "VIDEO_ASPECT_RATIO_LANDSCAPE": "16:9",
            "VIDEO_ASPECT_RATIO_PORTRAIT": "9:16",
            "VIDEO_ASPECT_RATIO_SQUARE": "1:1",
        }.get(str(aspect_ratio or "").strip(), "16:9")

    @staticmethod
    def _parse_sse_json_events(raw_text: str) -> List[Dict[str, Any]]:
        events = []
        for block in str(raw_text or "").split("\n\n"):
            lines = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
            payload_text = "\n".join(lines).strip()
            if not payload_text or payload_text == "[DONE]":
                continue
            try:
                payload = json.loads(payload_text)
            except Exception:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    @staticmethod
    def _extract_agent_session_id(payload: Dict[str, Any]) -> Optional[str]:
        for session in payload.get("sessions", []) if isinstance(payload, dict) else []:
            if isinstance(session, dict) and session.get("agentSessionId"):
                return str(session["agentSessionId"])
        info = payload.get("sessionInfo") if isinstance(payload, dict) else None
        return str(info.get("agentSessionId")) if isinstance(info, dict) and info.get("agentSessionId") else None

    @staticmethod
    def _extract_flow_entity_id(payload: Dict[str, Any]) -> Optional[str]:
        candidates = [payload]
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict):
            candidates.append(result)
            data = result.get("data")
            if isinstance(data, dict):
                candidates.append(data)
                node = data.get("json")
                if isinstance(node, dict):
                    candidates.extend([node, node.get("result")])
        for candidate in candidates:
            if isinstance(candidate, dict):
                for key in ("entityId", "id", "parentEntityId"):
                    if candidate.get(key):
                        return str(candidate[key])
        return None

    @staticmethod
    def _extract_turn_count(payload: Dict[str, Any]) -> int:
        turns = payload.get("turns") if isinstance(payload, dict) else None
        return len(turns) if isinstance(turns, list) else 0

    @staticmethod
    def _extract_generate_video_with_references_result(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for event in events:
            message = event.get("agentMessage") if isinstance(event, dict) else None
            for agent_event in message.get("agentEvents", []) if isinstance(message, dict) else []:
                wrapper = agent_event.get("toolResult") if isinstance(agent_event, dict) else None
                if isinstance(wrapper, dict) and wrapper.get("toolName") == "generate_video_with_references":
                    result = wrapper.get("toolResult")
                    if isinstance(result, dict):
                        return result
        return None

    async def get_flow_creation_agent_session(
        self,
        at: str,
        project_id: str,
        *,
        account_id: Optional[str] = None,
        allow_global_fallback: bool = True,
    ) -> Optional[str]:
        payload = await self._aisandbox_request(
            "GET",
            f"/flowCreationAgent/sessions?projectId={quote(project_id, safe='')}",
            at,
            content_type=None,
            account_id=account_id,
        )
        session_id = self._extract_agent_session_id(payload)
        if session_id or not allow_global_fallback:
            return session_id
        payload = await self._aisandbox_request("GET", "/flowCreationAgent/sessions", at, content_type=None, account_id=account_id)
        return self._extract_agent_session_id(payload)

    async def get_flow_creation_agent_session_detail(
        self,
        at: str,
        agent_session_id: str,
        *,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._aisandbox_request(
            "GET",
            f"/flowCreationAgent/sessions/{quote(agent_session_id, safe='')}",
            at,
            content_type=None,
            account_id=account_id,
        )

    async def create_flow_entity(self, st: str, project_id: str) -> str:
        payload = await self._labs_trpc_post_with_st("flow.createEntity", {"json": {"projectId": project_id}}, st, project_id)
        entity_id = self._extract_flow_entity_id(payload)
        if not entity_id:
            raise RuntimeError("flow.createEntity response did not include entityId")
        return entity_id

    async def copy_project_media_to_character_slot(
        self,
        at: str,
        *,
        project_id: str,
        media_id: str,
        entity_id: str,
        image_reference_index: int,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "mediaId": media_id,
            "destinationProjectId": project_id,
            "destinationMediaContext": {"entityContext": {"entityId": entity_id, "characterSlot": {"imageReferenceIndex": int(image_reference_index)}}},
        }
        return await self._aisandbox_request(
            "POST", "/flow:copyProjectMedia", at,
            raw_body=self._compact_json_dumps(payload), account_id=account_id,
        )

    async def stream_flow_creation_agent(
        self,
        at: str,
        payload: Dict[str, Any],
        *,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        action: str = "VIDEO_GENERATION",
        account_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        url = f"{self.api_base_url}/flowCreationAgent:streamChat?alt=sse"
        headers = self._build_browser_style_control_headers(
            "https://labs.google/", origin="https://labs.google", account_id=account_id,
            content_type="application/json", accept_language=self._get_primary_accept_language(),
        )
        headers["Accept"] = "text/event-stream, text/event-stream"
        raw_text = ""
        if config.captcha_method == "browser" and project_id:
            from .browser_captcha import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(self.db)
            submit = getattr(service, "submit_flow_request", None)
            if callable(submit):
                response, _browser_ref, fingerprint = await submit(
                    project_id=project_id, action=action, token_id=token_id,
                    url=url, at_token=at, json_data=payload, timeout=self._get_video_submit_timeout(),
                )
                self._set_request_fingerprint(fingerprint or None)
                if int(response.get("status") or 0) >= 400:
                    raise RuntimeError(f"HTTP Error {response.get('status')}: {str(response.get('text') or '')[:500]}")
                raw_text = str(response.get("text") or "")
        if not raw_text:
            raw_text = await self._make_text_request(
                "POST", url, headers=headers, json_data=payload, use_at=True, at_token=at,
                timeout=self._get_video_submit_timeout(), apply_default_client_headers=False,
                impersonate=self._resolve_runtime_impersonate(),
            )
        return self._parse_sse_json_events(raw_text)

    async def generate_omni_reference_video(
        self,
        at: str,
        st: str,
        project_id: str,
        prompt: str,
        aspect_ratio: str,
        reference_media_ids: List[str],
        model_usage_key: str = "abra_r2v_8s",
        model_display_name: str = "Omni Flash",
        duration: int = 8,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        poll_task_progress: PollTaskProgressHook = None,
    ) -> Dict[str, Any]:
        """Generate Omni reference video through Flow Creation Agent."""
        if not st:
            raise RuntimeError("Omni reference video requires an ST token")
        if not reference_media_ids:
            raise RuntimeError("Omni reference video requires at least one reference image")

        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        retry_attempt = 0
        last_error: Optional[Exception] = None
        client_session_id = self._generate_session_id()
        entity_id: Optional[str] = None
        frontend_warmed = False
        account_id = at[:16] if at else None
        agent_ratio = self._video_aspect_ratio_to_agent_aspect_ratio(aspect_ratio)
        agent_prompt = f"{prompt}\n\nUse a {agent_ratio} aspect ratio." if prompt else f"Use a {agent_ratio} aspect ratio."

        while retry_attempt < max_retries:
            browser_id = None
            approval_browser_id = None
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                raise RuntimeError("Video launch queue wait timeout")
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                    poll_task_progress=poll_task_progress,
                )
            finally:
                await self._release_video_launch_gate(token_id)

            if not recaptcha_token:
                last_error = RuntimeError("Failed to obtain reCAPTCHA token")
                if await self._handle_missing_recaptcha_token(
                    retry_attempt, max_retries, browser_id, project_id, "[VIDEO OMNI-R2V]",
                ):
                    retry_attempt += 1
                    continue
                raise last_error

            try:
                if not entity_id:
                    entity_id = await self.create_flow_entity(st, project_id)
                    for index, media_id in enumerate(reference_media_ids):
                        await self.copy_project_media_to_character_slot(
                            at, project_id=project_id, media_id=media_id, entity_id=entity_id,
                            image_reference_index=index, account_id=account_id,
                        )

                if not frontend_warmed:
                    await self._warmup_flow_video_frontend_context(
                        at=at, project_id=project_id, token_id=token_id,
                        session_id=client_session_id, user_paygate_tier=user_paygate_tier,
                        prompt=agent_prompt, model_key=model_usage_key, aspect_ratio=aspect_ratio,
                    )
                    frontend_warmed = True

                agent_session_id = await self.get_flow_creation_agent_session(
                    at, project_id, account_id=account_id, allow_global_fallback=True,
                )
                if not agent_session_id:
                    raise RuntimeError("Flow Creation Agent session was not found")
                detail = await self.get_flow_creation_agent_session_detail(
                    at, agent_session_id, account_id=account_id,
                )
                turn_number = self._extract_turn_count(detail) + 1

                def build_payload(text: str, captcha: str, turn: int, include_entity: bool) -> Dict[str, Any]:
                    message: Dict[str, Any] = {"userPrompt": {"parts": [{"text": text}]}}
                    if include_entity:
                        message["entityReferences"] = [{"entityId": entity_id, "handle": "entity-0"}]
                    return {
                        "agentSessionId": agent_session_id,
                        "agentClientContext": {
                            "projectId": f"projects/{project_id}",
                            "clientSessionId": client_session_id,
                            "recaptchaContext": {"token": captcha, "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"},
                            "turnNumber": turn,
                        },
                        "userMessage": message,
                    }

                events = await self.stream_flow_creation_agent(
                    at, build_payload(agent_prompt, recaptcha_token, turn_number, True),
                    project_id=project_id, token_id=token_id, account_id=account_id,
                )
                tool_result = self._extract_generate_video_with_references_result(events)
                if not tool_result:
                    approval_token, approval_browser_id = await self._get_recaptcha_token(
                        project_id,
                        action="VIDEO_GENERATION",
                        token_id=token_id,
                        poll_task_progress=poll_task_progress,
                    )
                    if not approval_token:
                        raise RuntimeError("Omni approval could not obtain a reCAPTCHA token")
                    events = await self.stream_flow_creation_agent(
                        at, build_payload("Approve", approval_token, turn_number + 1, False),
                        project_id=project_id, token_id=token_id, account_id=account_id,
                    )
                    tool_result = self._extract_generate_video_with_references_result(events)
                if not tool_result or not str(tool_result.get("media_id") or "").strip():
                    raise RuntimeError("Creation Agent did not return generate_video_with_references media_id")

                media_id = str(tool_result["media_id"]).strip()
                resolved_project_id = str(tool_result.get("project_id") or project_id)
                operation = {
                    "operation": {"name": media_id}, "name": media_id, "mediaName": media_id,
                    "projectId": resolved_project_id, "workflowId": tool_result.get("workflow_id"),
                    "batchId": tool_result.get("batch_id"), "status": "MEDIA_GENERATION_STATUS_ACTIVE",
                }
                return {
                    "operations": [operation], "agentToolResult": tool_result,
                    "projectId": resolved_project_id, "entityId": entity_id, "entityHandle": "entity-0",
                    "modelUsageKey": model_usage_key, "aspectRatio": tool_result.get("aspect_ratio") or aspect_ratio,
                    "duration": duration, "modelDisplayName": model_display_name,
                }
            except Exception as exc:
                last_error = exc
                if await self._handle_retryable_generation_error(
                    exc, retry_attempt, max_retries, browser_id, project_id,
                    "[VIDEO OMNI-R2V]", defer_browser_error_notification=True,
                ):
                    max_retries = self._resolve_generation_retry_budget(max_retries, exc)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
                await self._notify_browser_captcha_request_finished(approval_browser_id)
        raise last_error or RuntimeError("Omni reference generation failed")

    def _resolve_generation_retry_budget(
        self,
        base_max_retries: int,
        error: Optional[Union[Exception, str]] = None,
    ) -> int:
        """Return the bounded retry budget for generation/captcha failures."""
        try:
            budget = max(1, int(base_max_retries or 1))
        except Exception:
            budget = 1
        error_text = str(error or "").lower()
        if config.captcha_method == "browser" or "recaptcha evaluation failed" in error_text:
            budget = max(budget, config.browser_captcha_generation_retries)
        return max(1, min(20, budget))

    async def _handle_retryable_generation_error(
        self,
        error: Exception,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
        defer_browser_error_notification: bool = False,
    ) -> bool:
        """统一处理生成链路的重试判定与打码自愈通知。"""
        error_str = str(error)
        error_lower = error_str.lower()
        retry_reason = self._get_retry_reason(error_str)
        notify_reason = retry_reason or error_str[:120] or type(error).__name__
        if not defer_browser_error_notification:
            await self._notify_browser_captcha_error(
                browser_id=browser_id,
                project_id=project_id,
                error_reason=notify_reason,
                error_message=error_str,
            )
        if not retry_reason:
            return False

        should_defer_remote = any(
            key in error_lower
            for key in [
                "public_error_unusual_activity",
                "recaptcha evaluation failed",
                "recaptcha 验证失败",
            ]
        )
        if (
            should_defer_remote
            and config.captcha_method == "browser"
            and self._can_use_browser_gateway_fallback()
        ):
            final_attempt_index = max(0, int(max_retries) - 1)
            self._set_remote_fallback_attempt(final_attempt_index)
            debug_logger.log_warning(
                f"{log_prefix}检测到上游拒绝本地 reCAPTCHA，固定策略: "
                f"attempt {retry_attempt + 1}/{max_retries}=local，"
                f"将在 attempt {final_attempt_index + 1}/{max_retries} 切换 remote_browser。"
            )

        is_terminal_attempt = retry_attempt >= max_retries - 1

        if is_terminal_attempt:
            debug_logger.log_warning(
                f"{log_prefix}遇到{retry_reason}，已达到最大重试次数({max_retries})，本次请求失败并执行关闭回收。"
            )
            return False

        debug_logger.log_warning(
            f"{log_prefix}遇到{retry_reason}，正在重新获取验证码重试 ({retry_attempt + 2}/{max_retries})..."
        )
        await asyncio.sleep(1)
        return True

    async def _handle_missing_recaptcha_token(
        self,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
    ) -> bool:
        token_error = Exception("Failed to obtain reCAPTCHA token")
        return await self._handle_retryable_generation_error(
            error=token_error,
            retry_attempt=retry_attempt,
            max_retries=max_retries,
            browser_id=browser_id,
            project_id=project_id,
            log_prefix=log_prefix,
        )

    def _get_retry_reason(self, error_str: str) -> Optional[str]:
        """判断是否需要重试，返回日志提示内容"""
        error_lower = error_str.lower()
        if "403" in error_lower:
            return "403错误"
        if "429" in error_lower or "too many requests" in error_lower:
            return "429限流"
        if self._is_retryable_network_error(error_str):
            return "网络/TLS错误"
        if "recaptcha evaluation failed" in error_lower:
            return "reCAPTCHA 验证失败"
        if "recaptcha" in error_lower:
            return "reCAPTCHA 错误"
        if any(keyword in error_lower for keyword in [
            "http error 500",
            "public_error",
            "internal error",
            "reason=internal",
            "reason: internal",
            "\"reason\":\"internal\"",
            "server error",
            "upstream error",
        ]):
            return "500/内部错误"
        return None

    async def _notify_browser_captcha_error(
        self,
        browser_id: Optional[Union[int, str]] = None,
        project_id: Optional[str] = None,
        error_reason: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        """通知浏览器打码服务执行失败自愈。
        
        Args:
            browser_id: browser 模式使用的浏览器 ID
            project_id: personal 模式使用的 project_id
            error_reason: 已归类的错误原因
            error_message: 原始错误文本
        """
        if config.captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_error(
                    browser_id,
                    error_reason=error_reason,
                    error_message=error_message,
                )
            except Exception:
                pass
        elif config.captcha_method == "personal" and project_id:
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                )
            except Exception:
                pass
        elif config.captcha_method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/error",
                    json_data={"error_reason": error_reason or error_message or "upstream_error"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] 上报 error 失败: {e}")
        elif config.captcha_method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService
                service = await ExtensionCaptchaService.get_instance(self.db)
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                )
            except Exception:
                pass

    async def _notify_browser_captcha_request_finished(self, browser_id: Optional[Union[int, str]] = None):
        """通知有头浏览器：上游图片/视频请求已结束，可关闭对应打码浏览器。"""
        if config.captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_request_finished(browser_id)
            except Exception:
                pass
        elif config.captcha_method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/finish",
                    json_data={"status": "success"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] 上报 finish 失败: {e}")

    def _generate_session_id(self) -> str:
        """生成sessionId: ;timestamp"""
        return self.models.generate_session_id()

    def _generate_scene_id(self) -> str:
        """生成sceneId: UUID"""
        return self.models.generate_scene_id()

    def _get_remote_browser_service_config(self) -> tuple[str, str, int]:
        base_url = (config.remote_browser_base_url or "").strip().rstrip("/")
        api_key = (config.remote_browser_api_key or "").strip()
        timeout = max(5, int(config.remote_browser_timeout or 60))

        if not base_url:
            raise RuntimeError("remote_browser 服务地址未配置")
        if not api_key:
            raise RuntimeError("remote_browser API Key 未配置")

        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise RuntimeError("remote_browser 服务地址格式错误")

        return base_url, api_key, timeout

    @staticmethod
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

    @staticmethod
    def _parse_json_response_text(text: str) -> Optional[Any]:
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
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
            raise RuntimeError(f"remote_browser 请求失败: {e}") from e

        return status_code, FlowClient._parse_json_response_text(text), text

    @staticmethod
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
            "timeout": FlowClient._build_remote_browser_http_timeout(timeout),
        }

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_kwargs["json"] = payload

        if httpx is None:
            return await FlowClient._stdlib_json_http_request(
                method=method,
                url=url,
                headers=req_headers,
                payload=payload,
                timeout=timeout,
            )

        try:
            # remote_browser 控制面只需要稳定传输 JSON，不需要浏览器指纹伪装。
            # 使用 httpx 可以避免 curl_cffi 在当前环境下 POST body 被吞掉。
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as session:
                response = await session.request(
                    method=request_method,
                    url=url,
                    **request_kwargs,
                )
        except Exception as e:
            raise RuntimeError(f"remote_browser 请求失败: {e}") from e

        status_code = int(getattr(response, "status_code", 0) or 0)
        text = response.text or ""
        parsed = FlowClient._parse_json_response_text(text)

        return status_code, parsed, text

    async def _call_remote_browser_service(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        timeout_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        base_url, api_key, timeout = self._get_remote_browser_service_config()
        url = f"{base_url}{path}"
        effective_timeout = max(5, int(timeout_override or timeout))

        status_code, payload, response_text = await self._sync_json_http_request(
            method=method,
            url=url,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=json_data,
            timeout=effective_timeout,
        )

        if status_code >= 400:
            detail = ""
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or str(payload)
            if not detail:
                detail = (response_text or "").strip() or f"HTTP {status_code}"
            raise RuntimeError(f"remote_browser 请求失败: {detail}")

        if not isinstance(payload, dict):
            raise RuntimeError("remote_browser 返回格式错误")

        return payload

    async def prefill_remote_browser_pool(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        cooldown_seconds: float = 8.0,
    ) -> bool:
        """让本地 remote_browser 服务提前开始补池，尽量把取 token 等待搬到前面。"""
        if config.captcha_method != "remote_browser":
            return False

        normalized_project = str(project_id or "").strip()
        normalized_action = str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        if not normalized_project:
            return False

        cache_key = f"{normalized_project}|{normalized_action}"
        now_value = time.monotonic()
        last_sent = float(self._remote_browser_prefill_last_sent.get(cache_key, 0.0) or 0.0)
        if (now_value - last_sent) < max(0.5, float(cooldown_seconds)):
            return False

        try:
            await self._call_remote_browser_service(
                method="POST",
                path="/api/v1/prefill",
                json_data={
                    "project_id": normalized_project,
                    "action": normalized_action,
                },
                timeout_override=3,
            )
            self._remote_browser_prefill_last_sent[cache_key] = now_value
            return True
        except Exception as e:
            debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] prefill 失败: {e}")
            return False

    async def prefill_remote_browser_for_tokens(self, tokens: List[Any], action: str = "IMAGE_GENERATION") -> int:
        if config.captcha_method != "remote_browser":
            return 0

        unique_projects: List[str] = []
        seen_projects = set()
        for token in tokens or []:
            project_id = str(getattr(token, "current_project_id", "") or "").strip()
            if not project_id or project_id in seen_projects:
                continue
            seen_projects.add(project_id)
            unique_projects.append(project_id)

        warmed = 0
        for project_id in unique_projects:
            if await self.prefill_remote_browser_pool(project_id, action=action):
                warmed += 1
        return warmed

    def _resolve_remote_browser_solve_timeout(self, action: str) -> int:
        base_timeout = max(5, int(config.remote_browser_timeout or 60))
        action_name = str(action or "").strip().upper()

        # 这里只是拿 reCAPTCHA token，不应该跟整条生成链路共用数百秒级超时。
        target_timeout = 45 if action_name == "VIDEO_GENERATION" else 35
        return max(12, min(base_timeout, target_timeout))

    async def _log_recaptcha_headed_proxy_context(self, captcha_method: str, token_id: Optional[int]) -> None:
        """Log which proxy headed captcha will use, before narrative action lines."""
        if not debug_logger.should_log_recaptcha():
            return
        if captcha_method not in ("browser", "personal"):
            return
        if not self.db:
            debug_logger.log_recaptcha_proxy_check(
                "[DEBUG] reCAPTCHA headed-browser proxy check: database unavailable (endpoint unknown)"
            )
            return
        try:
            cc = await self.db.get_captcha_config()
        except Exception as e:
            debug_logger.log_recaptcha_proxy_check(
                f"[DEBUG] reCAPTCHA headed-browser proxy check: captcha config read failed: {type(e).__name__}: {e}"
            )
            return

        bp_on = bool(getattr(cc, "browser_proxy_enabled", False))
        bp_url = (getattr(cc, "browser_proxy_url", None) or "").strip()

        if captcha_method == "browser":
            token_url = ""
            if token_id:
                try:
                    tok = await self.db.get_token(token_id)
                    token_url = (tok.captcha_proxy_url or "").strip() if tok else ""
                except Exception as e:
                    debug_logger.log_recaptcha_proxy_check(
                        f"[DEBUG] reCAPTCHA headed-browser proxy check: token lookup failed: {type(e).__name__}: {e}"
                    )
            if token_url:
                src = "token"
                eff = token_url
            elif bp_on and bp_url:
                src = "global_captcha_browser"
                eff = bp_url
            else:
                src = "none"
                eff = None
            ep = _proxy_endpoint_for_log(eff) if eff else "direct"
            debug_logger.log_recaptcha_proxy_check(
                f"[DEBUG] reCAPTCHA headed-browser proxy check: chosen_source={src} endpoint={ep} "
                f"(captcha_browser_proxy_enabled={bp_on})"
            )
            return

        rq_on = False
        rq_url = ""
        try:
            pc = await self.db.get_proxy_config()
            if pc:
                rq_on = bool(pc.enabled)
                rq_url = (pc.proxy_url or "").strip() if pc.proxy_url else ""
        except Exception:
            pass
        if bp_on and bp_url:
            src = "global_captcha_browser"
            eff = bp_url
        elif rq_on and rq_url:
            src = "request_proxy"
            eff = rq_url
        else:
            src = "none"
            eff = None
        ep = _proxy_endpoint_for_log(eff) if eff else "direct"
        debug_logger.log_recaptcha_proxy_check(
            f"[DEBUG] reCAPTCHA personal-browser proxy check: chosen_source={src} endpoint={ep} "
            f"(captcha_browser_proxy_enabled={bp_on}, request_proxy_enabled={rq_on})"
        )

    def _recaptcha_begin_request(self, action: str) -> None:
        """Narrative-style lines when starting a reCAPTCHA solve (image/video job)."""
        if not debug_logger.should_log_recaptcha():
            return
        last = self._last_recaptcha_action
        if last is not None and last != action:
            debug_logger.log_recaptcha_action_switch(last, action)
        debug_logger.log_recaptcha_request_action(action)
        debug_logger.log_recaptcha_generating(action)
        self._last_recaptcha_action = action

    async def _get_recaptcha_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        retry_attempt: int = 0,
        max_retries: int = 1,
        poll_task_progress: PollTaskProgressHook = None,
    ) -> tuple[Optional[str], Optional[Union[int, str]]]:
        """获取reCAPTCHA token - 支持多种打码方式
        
        Args:
            project_id: 项目ID
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成和2K/4K图片放大 (默认)
                - VIDEO_GENERATION: 视频生成和视频放大
            token_id: 当前业务 token id（browser 模式下用于读取 token 级打码代理）
        
        Returns:
            (token, browser_id) 元组。
            - browser 模式: browser_id 为本地浏览器 ID
            - remote_browser 模式: browser_id 为远程 session_id
            - 其他模式: browser_id 为 None
        """
        captcha_method = config.captcha_method
        managed_api_key_id = self.get_managed_api_key_id()
        if int(retry_attempt) == 0:
            # Start of a new request chain.
            self._set_remote_fallback_attempt(-1)
        debug_logger.log_info(
            f"[reCAPTCHA] 开始获取 token: method={captcha_method}, project_id={project_id}, action={action}, "
            f"managed_api_key_id={managed_api_key_id}"
        )
        await self._log_recaptcha_headed_proxy_context(captcha_method, token_id)
        self._recaptcha_begin_request(action)

        if captcha_method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService
                service = await ExtensionCaptchaService.get_instance(self.db)
                default_timeout = 45 if action == "VIDEO_GENERATION" else 25
                extension_timeout = int(
                    getattr(config, "dedicated_extension_captcha_timeout_seconds", default_timeout)
                    if getattr(config, "dedicated_extension_enabled", False)
                    else default_timeout
                )
                token, ext_req_id = await service.get_token(
                    project_id,
                    action,
                    timeout=extension_timeout,
                    token_id=token_id,
                    managed_api_key_id=managed_api_key_id,
                )
                extension_user_agent = None
                consume_user_agent = getattr(service, "consume_token_user_agent", None)
                if callable(consume_user_agent) and ext_req_id:
                    extension_user_agent = consume_user_agent(ext_req_id)
                if token and extension_user_agent:
                    self._set_request_fingerprint(
                        {
                            "user_agent": extension_user_agent,
                            "accept_language": self._get_primary_accept_language(),
                            "sec_ch_ua": self._infer_sec_ch_ua_from_user_agent(
                                extension_user_agent
                            ),
                            "project_id": project_id,
                            "origin": "https://labs.google",
                            "referer": self._build_flow_project_page_url(project_id),
                        }
                    )
                    debug_logger.log_info(
                        "[reCAPTCHA Extension] Applied solver-tab User-Agent to request fingerprint"
                    )
                    await _emit_poll_task_progress(
                        poll_task_progress,
                        {
                            "captcha_status": "user_agent_set",
                            "captcha_user_agent_set": True,
                            "captcha_provider": "extension",
                        },
                    )
                else:
                    self._set_request_fingerprint(None)
                if ext_req_id:
                    _flow_extension_upstream_req_id.set(ext_req_id)
                else:
                    _flow_extension_upstream_req_id.set(None)
                return token, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Extension] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None

        # 内置浏览器打码 (nodriver)
        if captcha_method == "personal":
            debug_logger.log_info(f"[reCAPTCHA] 使用 personal 模式")
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                debug_logger.log_info(f"[reCAPTCHA] 导入 BrowserCaptchaService 成功")
                service = await BrowserCaptchaService.get_instance(self.db)
                debug_logger.log_info(f"[reCAPTCHA] 获取服务实例成功，准备调用 get_token")
                solve_bundle = None
                get_token_bundle = getattr(service, "get_token_bundle", None)
                if callable(get_token_bundle):
                    solve_bundle = await get_token_bundle(
                        project_id,
                        action,
                        token_id=token_id,
                    )
                    token = str((solve_bundle or {}).get("token") or "").strip() or None
                else:
                    get_token_with_metadata = getattr(service, "get_token_with_metadata", None)
                    if callable(get_token_with_metadata):
                        result = await get_token_with_metadata(
                            project_id,
                            action,
                            token_id=token_id,
                        )
                        token = result[0] if isinstance(result, tuple) and result else result
                    else:
                        token = await service.get_token(project_id, action, token_id=token_id)
                    solve_bundle = {
                        "token": token,
                        "fingerprint": service.get_last_fingerprint() if token else None,
                    } if token else None
                meta = debug_logger.format_recaptcha_token_meta(token)
                debug_logger.log_info(f"[reCAPTCHA] get_token 返回: {meta}")
                print(f"[DEBUG-DEEP] personal token obtained: {bool(token)}, token_prefix={str(token)[:40] if token else 'None'}")
                print(f"[DEBUG-DEEP] personal fingerprint: {service.get_last_fingerprint()}")
                fingerprint = (
                    solve_bundle.get("fingerprint")
                    if isinstance(solve_bundle, dict) and isinstance(solve_bundle.get("fingerprint"), dict)
                    else None
                )
                if isinstance(solve_bundle, dict) and token:
                    session_cookies = solve_bundle.get("session_cookies")
                    proxy_url = str(solve_bundle.get("proxy_url") or "").strip()
                    next_fingerprint = dict(fingerprint or {})
                    if isinstance(session_cookies, dict) and session_cookies:
                        next_fingerprint["session_cookies"] = dict(session_cookies)
                    if proxy_url and not str(next_fingerprint.get("proxy_url") or "").strip():
                        next_fingerprint["proxy_url"] = proxy_url
                    next_fingerprint["project_id"] = project_id
                    next_fingerprint.setdefault("origin", "https://labs.google")
                    next_fingerprint.setdefault("referer", self._build_flow_project_page_url(project_id))
                    fingerprint = next_fingerprint or None
                if token and not str((fingerprint or {}).get("user_agent") or "").strip():
                    debug_logger.log_warning(
                        "[reCAPTCHA Personal] Token discarded because the producing browser fingerprint has no User-Agent"
                    )
                    self._set_request_fingerprint(None)
                    return None, None
                self._set_request_fingerprint(fingerprint if token else None)
                if token:
                    debug_logger.log_recaptcha_token_success(token)
                else:
                    debug_logger.log_recaptcha_browser_error(
                        "personal get_token returned empty",
                        {"success": False, "token": None},
                    )
                return token, None
            except RuntimeError as e:
                # 捕获 Docker 环境或依赖缺失的明确错误
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Personal] {error_msg}")
                debug_logger.log_recaptcha_execution_error(error_msg)
                print(f"[reCAPTCHA] ❌ 内置浏览器打码失败: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] 导入失败: {str(e)}")
                debug_logger.log_recaptcha_execution_error(f"ImportError: {e}")
                print("[reCAPTCHA] ❌ nodriver 未安装，请运行: uv sync")
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] 错误: {str(e)}")
                debug_logger.log_recaptcha_execution_error(f"{type(e).__name__}: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # 有头浏览器打码 (playwright)
        elif captcha_method == "browser":
            target_attempt = int(self._remote_fallback_attempt_ctx.get())
            if self._should_use_deferred_remote_fallback(
                captcha_method=captcha_method,
                retry_attempt=retry_attempt,
            ):
                debug_logger.log_info(
                    f"[reCAPTCHA] fixed local_then_remote strategy: attempt {retry_attempt + 1}/{max_retries} uses remote_browser fallback"
                )
                try:
                    solve_timeout = self._resolve_remote_browser_solve_timeout(action)
                    payload = await self._call_remote_browser_service(
                        method="POST",
                        path="/api/v1/solve",
                        json_data={
                            "project_id": project_id,
                            "action": action,
                        },
                        timeout_override=solve_timeout,
                    )
                    token = payload.get("token")
                    session_id = payload.get("session_id")
                    fingerprint = (
                        payload.get("fingerprint")
                        if isinstance(payload.get("fingerprint"), dict)
                        else None
                    )
                    if not token or not session_id:
                        raise RuntimeError(
                            f"remote_browser forced fallback 返回缺少 token/session_id: {payload}"
                        )
                    self._set_request_fingerprint(fingerprint if token else None)
                    self._set_remote_fallback_attempt(-1)
                    debug_logger.log_info(
                        "[reCAPTCHA] forced remote_browser fallback succeeded"
                    )
                    debug_logger.log_recaptcha_token_success(token)
                    return token, str(session_id)
                except Exception as force_remote_error:
                    debug_logger.log_error(
                        f"[reCAPTCHA BrowserFallback] forced remote attempt failed: {type(force_remote_error).__name__}: {force_remote_error}"
                    )
                    # Keep the force flag for subsequent retries in the same request chain.
                    self._set_request_fingerprint(None)
                    return None, None
            if target_attempt >= 0:
                debug_logger.log_info(
                    f"[reCAPTCHA] fixed local_then_remote strategy: attempt {retry_attempt + 1}/{max_retries} stays local headed (remote planned at attempt {target_attempt + 1}/{max_retries})"
                )
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                token, browser_id = await service.get_token(project_id, action, token_id=token_id)
                fingerprint = await service.get_fingerprint(browser_id) if token else None
                self._set_request_fingerprint(fingerprint if token else None)
                if token:
                    self._set_remote_fallback_attempt(-1)
                    debug_logger.log_recaptcha_token_success(token)
                else:
                    debug_logger.log_recaptcha_browser_error(
                        "headed browser pool returned empty token",
                        {"success": False, "token": None, "browser_ref": browser_id},
                    )
                return token, browser_id
            except Exception as e:
                primary_error = e
                primary_error_msg = f"{type(e).__name__}: {str(e)}"
                debug_logger.log_error(f"[reCAPTCHA Browser] 错误: {primary_error_msg}")

                if not self._can_use_browser_gateway_fallback():
                    debug_logger.log_recaptcha_execution_error(primary_error_msg)
                    if isinstance(primary_error, ImportError):
                        print("[reCAPTCHA] ❌ playwright 未安装，请运行: uv sync && uv run playwright install chromium")
                    elif isinstance(primary_error, RuntimeError):
                        print(f"[reCAPTCHA] ❌ 有头浏览器打码失败: {str(primary_error)}")
                    self._set_request_fingerprint(None)
                    return None, None

                # Fallback path: browser -> remote_browser gateway
                try:
                    self._get_remote_browser_service_config()
                except Exception as config_error:
                    fallback_error_msg = f"fallback unavailable: {type(config_error).__name__}: {config_error}"
                    combined = (
                        f"browser->remote_browser fallback failed; "
                        f"primary={primary_error_msg}; {fallback_error_msg}"
                    )
                    debug_logger.log_error(f"[reCAPTCHA BrowserFallback] {combined}")
                    debug_logger.log_recaptcha_execution_error(combined)
                    self._set_request_fingerprint(None)
                    return None, None

                debug_logger.log_info(
                    "[reCAPTCHA] browser->remote_browser fallback triggered"
                )
                try:
                    solve_timeout = self._resolve_remote_browser_solve_timeout(action)
                    payload = await self._call_remote_browser_service(
                        method="POST",
                        path="/api/v1/solve",
                        json_data={
                            "project_id": project_id,
                            "action": action,
                        },
                        timeout_override=solve_timeout,
                    )
                    token = payload.get("token")
                    session_id = payload.get("session_id")
                    fingerprint = (
                        payload.get("fingerprint")
                        if isinstance(payload.get("fingerprint"), dict)
                        else None
                    )
                    if not token or not session_id:
                        raise RuntimeError(
                            f"remote_browser fallback 返回缺少 token/session_id: {payload}"
                        )
                    self._set_request_fingerprint(fingerprint if token else None)
                    debug_logger.log_info(
                        "[reCAPTCHA] browser->remote_browser fallback succeeded"
                    )
                    debug_logger.log_recaptcha_token_success(token)
                    return token, str(session_id)
                except Exception as fallback_error:
                    fallback_error_msg = (
                        f"{type(fallback_error).__name__}: {str(fallback_error)}"
                    )
                    combined = (
                        f"browser->remote_browser fallback failed; "
                        f"primary={primary_error_msg}; fallback={fallback_error_msg}"
                    )
                    debug_logger.log_error(f"[reCAPTCHA BrowserFallback] {combined}")
                    debug_logger.log_recaptcha_execution_error(combined)
                    self._set_request_fingerprint(None)
                    return None, None

        elif captcha_method == "remote_browser":
            try:
                solve_timeout = self._resolve_remote_browser_solve_timeout(action)
                payload = await self._call_remote_browser_service(
                    method="POST",
                    path="/api/v1/solve",
                    json_data={
                        "project_id": project_id,
                        "action": action,
                    },
                    timeout_override=solve_timeout,
                )
                token = payload.get("token")
                session_id = payload.get("session_id")
                fingerprint = payload.get("fingerprint") if isinstance(payload.get("fingerprint"), dict) else None
                self._set_request_fingerprint(fingerprint if token else None)
                if not token or not session_id:
                    raise RuntimeError(f"remote_browser 返回缺少 token/session_id: {payload}")
                debug_logger.log_recaptcha_token_success(token)
                return token, str(session_id)
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA RemoteBrowser] 错误: {str(e)}")
                debug_logger.log_recaptcha_execution_error(f"{type(e).__name__}: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # API打码服务
        elif captcha_method in ["yescaptcha", "capmonster", "ezcaptcha", "capsolver"]:
            api_proxy_url = None
            if self.proxy_manager:
                try:
                    api_proxy_url = await self.proxy_manager.get_request_proxy_url()
                except Exception as e:
                    debug_logger.log_warning(f"[reCAPTCHA] Failed to get proxy for API captcha: {e}")
            # Bind solve and submit to the same proxy. The solver-provided User-Agent,
            # when present, replaces the local fallback identity before submission.
            api_captcha_ua = self._generate_user_agent(
                str(token_id or project_id or captcha_method)
            )
            self._set_request_fingerprint(
                {
                    "proxy_url": api_proxy_url or "",
                    "user_agent": api_captcha_ua,
                    "accept_language": self._get_primary_accept_language(),
                    "sec_ch_ua": self._infer_sec_ch_ua_from_user_agent(api_captcha_ua),
                    "project_id": project_id,
                    "origin": "https://labs.google",
                    "referer": self._build_flow_project_page_url(project_id),
                }
            )
            api_result = await self._get_api_captcha_token(
                captcha_method,
                project_id,
                action,
                proxy_url=api_proxy_url,
                proxy_resolved=True,
            )
            if api_result is None:
                self._set_request_fingerprint(None)
                return None, None

            captcha_user_agent = None
            if isinstance(api_result, tuple):
                token = str(api_result[0] or "").strip() if api_result else ""
                if len(api_result) > 1:
                    captcha_user_agent = str(api_result[1] or "").strip() or None
            else:
                token = str(api_result or "").strip()

            if not token:
                self._set_request_fingerprint(None)
                return None, None

            if captcha_user_agent:
                existing_fingerprint = self.get_request_fingerprint() or {}
                existing_fingerprint["user_agent"] = captcha_user_agent
                existing_fingerprint["sec_ch_ua"] = self._infer_sec_ch_ua_from_user_agent(
                    captcha_user_agent
                )
                self._set_request_fingerprint(existing_fingerprint)
                debug_logger.log_info(
                    f"[reCAPTCHA {captcha_method}] Injected provider User-Agent into request fingerprint: "
                    f"{captcha_user_agent[:80]}"
                )
                await _emit_poll_task_progress(
                    poll_task_progress,
                    {
                        "captcha_status": "user_agent_set",
                        "captcha_user_agent_set": True,
                        "captcha_provider": captcha_method,
                    },
                )
            else:
                debug_logger.log_warning(
                    f"[reCAPTCHA {captcha_method}] Provider solution omitted userAgent; using request fallback identity"
                )

            debug_logger.log_recaptcha_token_success(token)
            return token, None
        else:
            debug_logger.log_info(f"[reCAPTCHA] 未知的打码方式: {captcha_method}")
            debug_logger.log_recaptcha_browser_error(f"unknown captcha_method={captcha_method}", {"success": False})
            self._set_request_fingerprint(None)
            return None, None

    async def _get_api_captcha_token(
        self,
        method: str,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        *,
        proxy_url: Optional[str] = None,
        proxy_resolved: bool = False,
    ) -> Optional[tuple[str, Optional[str]]]:
        """通用API打码服务
        
        Args:
            method: 打码服务类型
            project_id: 项目ID
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)

        Returns:
            ``(gRecaptchaResponse, userAgent)`` when ready, otherwise ``None``.
            ``userAgent`` may be ``None`` when a provider omits it.
        """
        # 获取配置
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
            task_type = "ReCaptchaV3EnterpriseTaskProxyLess"
            min_score = None
        else:
            debug_logger.log_error(f"[reCAPTCHA] Unknown API method: {method}")
            debug_logger.log_recaptcha_browser_error(f"unknown API method: {method}", {"success": False})
            return None

        if not client_key:
            debug_logger.log_info(f"[reCAPTCHA] {method} API key not configured, skipping")
            debug_logger.log_recaptcha_browser_error(f"{method} API key not configured", {"success": False})
            return None

        website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
        page_action = action

        try:
            # Do not use curl_cffi impersonation for captcha API JSON endpoints: some ASGI
            # servers (for example FastAPI/Uvicorn) may receive an empty body and return 422.
            async with AsyncSession() as session:
                # curl_cffi: SOCKS5 uses `proxy`, HTTP/HTTPS uses `proxies`
                proxies = None
                proxy = None
                if not proxy_resolved and self.proxy_manager:
                    try:
                        proxy_url = await self.proxy_manager.get_request_proxy_url()
                    except Exception as e:
                        debug_logger.log_warning(f"[reCAPTCHA {method}] Failed to get proxy: {e}")
                if proxy_url:
                    if proxy_url.startswith("socks5://"):
                        proxy = proxy_url
                    else:
                        proxies = {"http": proxy_url, "https": proxy_url}

                create_url = f"{base_url}/createTask"
                create_data = {
                    "clientKey": client_key,
                    "task": {
                        "websiteURL": website_url,
                        "websiteKey": website_key,
                        "type": task_type,
                        "pageAction": page_action
                    }
                }
                if min_score is not None:
                    create_data["task"]["minScore"] = min_score

                if proxy:
                    result = await session.post(create_url, json=create_data, impersonate="chrome124", proxy=proxy)
                else:
                    result = await session.post(create_url, json=create_data, impersonate="chrome124", proxies=proxies)
                result_json = result.json()
                task_id = result_json.get('taskId')

                debug_logger.log_info(f"[reCAPTCHA {method}] created task_id: {task_id}")

                if not task_id:
                    error_desc = result_json.get('errorDescription', 'Unknown error')
                    debug_logger.log_error(f"[reCAPTCHA {method}] Failed to create task: {error_desc}")
                    debug_logger.log_recaptcha_browser_error(
                        f"{method} createTask failed: {error_desc[:500]}",
                        result_json,
                    )
                    return None

                get_url = f"{base_url}/getTaskResult"
                for i in range(40):
                    get_data = {
                        "clientKey": client_key,
                        "taskId": task_id
                    }
                    if proxy:
                        result = await session.post(get_url, json=get_data, impersonate="chrome124", proxy=proxy)
                    else:
                        result = await session.post(get_url, json=get_data, impersonate="chrome124", proxies=proxies)
                    result_json = result.json()

                    debug_logger.log_info(f"[reCAPTCHA {method}] polling #{i+1}: {result_json}")

                    status = result_json.get('status')
                    if status == 'ready':
                        solution = result_json.get('solution', {})
                        response = solution.get('gRecaptchaResponse')
                        if response:
                            debug_logger.log_info(f"[reCAPTCHA {method}] Token获取成功")
                            user_agent = str(solution.get('userAgent') or "").strip() or None
                            return response, user_agent

                    await asyncio.sleep(3)

                debug_logger.log_error(f"[reCAPTCHA {method}] Timeout waiting for token")
                debug_logger.log_recaptcha_browser_error(
                    f"{method} getTaskResult timeout after 40 polls",
                    {"success": False, "taskId": task_id},
                )
                return None

        except Exception as e:
            debug_logger.log_error(f"[reCAPTCHA {method}] error: {str(e)}")
            debug_logger.log_recaptcha_execution_error(f"{method}: {str(e)}")
            return None
