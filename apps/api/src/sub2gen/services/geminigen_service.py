"""GeminiGen web-session generation integration."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

from curl_cffi import CurlMime
from curl_cffi.requests import AsyncSession

from ..core.config import config as global_config
from ..core.database import Database
from ..core.geminigen_manifest import GEMINIGEN_MODEL_MANIFEST, geminigen_manifest_entry
from ..core.logger import debug_logger
from ..core.models import GeminiGenAccount, GeminiGenTask, RequestLog
from .file_cache import FileCache


VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".m4v"}
GEMINIGEN_OPERATION_BY_KIND = {"image": "geminigen_image", "video": "geminigen_video"}
GEMINIGEN_ORIGIN = "https://geminigen.ai"
GEMINIGEN_ANTIBOT_SECRET_KEY = "45NPBH$&"
GEMINIGEN_ANTIBOT_SECRET_SALT = "&vTQm0&u"
GEMINIGEN_GUARD_STABLE_ID = "MDYzYmU1NDQ1NDllN2IyZT"
GEMINIGEN_DOM_FINGERPRINT_HEX = "250119fee98c924f2c0b975f6586ba302bfdf81d6586ba115666822156668221"
GEMINIGEN_TIME_BUCKET_WINDOW_MS = 60_000
GEMINIGEN_CHROME_MAJOR = 147
GEMINIGEN_GUARD_STABLE_ID_LEN = 22
GEMINIGEN_GUARD_VERSION_BYTE = 1
GEMINIGEN_REFRESH_BEFORE_EXPIRY_SEC = 180
GEMINIGEN_CAPACITY_ERROR_CODE = "MAX_PROCESSING_IMAGEN_EXCEEDED"
GEMINIGEN_DAILY_LIMIT_ERROR_CODE = "DAILY_LIMIT_EXCEEDED"
GEMINIGEN_CAPACITY_COOLDOWN_INITIAL_SEC = 20.0
GEMINIGEN_CAPACITY_COOLDOWN_MAX_SEC = 90.0
GEMINIGEN_ACTIVE_STATUSES = {"queued", "processing", "submitted", "polling"}
GEMINIGEN_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
GEMINIGEN_UPSTREAM_CANCEL_MARKERS = ("cancel", "canceled", "cancelled", "stop", "stopped")
GEMINIGEN_UPSTREAM_FAILURE_MARKERS = ("fail", "error", "reject")
GEMINIGEN_PROFILE_CACHE_TTL = timedelta(hours=1)
GEMINIGEN_QUOTA_PATHS = {
    "image_gen": "/api/user/quota/image-gen",
    "grok_image": "/api/user/quota/grok-image",
    "grok_max": "/api/user/quota/grok-max",
}
GEMINIGEN_IMAGE_GEN_MAX_DAILY = 200
GEMINIGEN_GROK_IMAGE_MAX_DAILY = 50


@dataclass
class GeminiGenUpstreamError(RuntimeError):
    status_code: int
    message: str
    error_code: Optional[str] = None
    retryable_capacity: bool = False
    daily_limit_exceeded: bool = False

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)


class GeminiGenService:
    """Create, poll, and normalize GeminiGen Max web-app jobs."""

    def __init__(self, db: Database, file_cache: FileCache, proxy_manager=None):
        self.db = db
        self.file_cache = file_cache
        self.proxy_manager = proxy_manager
        self._guard_skew_ms = 0
        self._guard_skew_synced_at = 0.0
        self._guard_skew_lock = asyncio.Lock()
        self._token_refresh_locks: Dict[int, asyncio.Lock] = {}
        self._capacity_cooldowns: Dict[Tuple[int, str], float] = {}
        self._capacity_cooldown_attempts: Dict[Tuple[int, str], int] = {}
        self._finalize_locks: Dict[str, asyncio.Lock] = {}
        self._profile_refreshing_account_ids: set[int] = set()
        self._background_completion_jobs: set[str] = set()
        self._background_completion_lock = asyncio.Lock()

    async def _claim_background_completion(self, job_id: str) -> bool:
        """Allow only one in-process completion loop to own a GeminiGen job."""
        normalized = str(job_id or "").strip()
        if not normalized:
            return False
        async with self._background_completion_lock:
            if normalized in self._background_completion_jobs:
                return False
            self._background_completion_jobs.add(normalized)
            return True

    async def _release_background_completion(self, job_id: str) -> None:
        normalized = str(job_id or "").strip()
        if not normalized:
            return
        async with self._background_completion_lock:
            self._background_completion_jobs.discard(normalized)

    @staticmethod
    def is_geminigen_model(model: str) -> bool:
        return bool(geminigen_manifest_entry(model or ""))

    @staticmethod
    def model_catalog(*, video_enabled: bool = True) -> List[Dict[str, str]]:
        return [
            {
                "id": item["id"],
                "description": f"GeminiGen {item['kind']} generation - {item['endpoint_type']}",
            }
            for item in GEMINIGEN_MODEL_MANIFEST
            if video_enabled or item.get("kind") != "video"
        ]

    @staticmethod
    def _local_model_counts_by_group() -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in GEMINIGEN_MODEL_MANIFEST:
            options = item.get("options") if isinstance(item, dict) else {}
            if not isinstance(options, dict):
                continue
            model = str(options.get("model") or "").strip()
            if model:
                counts[model] = counts.get(model, 0) + 1
            elif item.get("endpoint_type") == "grok-image":
                counts["grok-image"] = counts.get("grok-image", 0) + 1
        return counts

    @staticmethod
    def _status_bucket(status: str) -> str:
        value = (status or "").strip().lower()
        if "operational" in value:
            return "operational"
        if "degraded" in value:
            return "degraded"
        if "outage" in value or "down" in value or "failed" in value:
            return "outage"
        return "unknown"

    @staticmethod
    def _normalize_success_rate(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return round(float(value), 2)
        except Exception:
            return None

    async def _first_active_account(self) -> Optional[GeminiGenAccount]:
        for account in await self.db.list_geminigen_accounts():
            if account.is_active:
                return account
        return None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _optional_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None

    @classmethod
    def parse_me_profile(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("GeminiGen /api/me payload must be an object")
        credit = payload.get("user_credit") if isinstance(payload.get("user_credit"), dict) else {}
        plan = payload.get("user_plan") if isinstance(payload.get("user_plan"), dict) else {}
        product = plan.get("product") if isinstance(plan.get("product"), dict) else {}
        active_benefits: List[Dict[str, Any]] = []
        for benefit in payload.get("user_benefits") or []:
            if not isinstance(benefit, dict) or not benefit.get("is_active"):
                continue
            benefit_product = benefit.get("product") if isinstance(benefit.get("product"), dict) else {}
            active_benefits.append(
                {
                    "id": cls._optional_int(benefit.get("id")),
                    "name": str(benefit_product.get("name") or "").strip(),
                    "expire_at": benefit.get("expire_at"),
                    "is_active": bool(benefit.get("is_active")),
                    "estimated_remaining": cls._optional_int(benefit.get("estimated_remaining")),
                }
            )
        return {
            "profile_user_id": cls._optional_int(payload.get("id")),
            "profile_uuid": str(payload.get("uuid") or "").strip() or None,
            "profile_email": str(payload.get("email") or "").strip() or None,
            "profile_full_name": str(payload.get("full_name") or "").strip() or None,
            "profile_is_active": bool(payload.get("is_active")) if payload.get("is_active") is not None else None,
            "available_credit": cls._optional_int(credit.get("available_credit")),
            "plan_credit": cls._optional_int(credit.get("plan_credit")),
            "purchased_credit": cls._optional_int(credit.get("purchased_credit")),
            "locked_credit": cls._optional_int(credit.get("locked_credit")),
            "subscription_credit": cls._optional_int(credit.get("subscription_credit")),
            "plan_name": str(product.get("name") or "").strip() or None,
            "plan_expire_at": cls._optional_datetime(plan.get("expire_at")),
            "active_benefits_json": json.dumps(active_benefits, separators=(",", ":")),
            "remaining_bulk_videos": cls._optional_int(payload.get("remaining_bulk_videos")),
            "remaining_daily_videos": cls._optional_int(payload.get("remaining_daily_videos")),
            "remaining_grok_max_daily_videos": cls._optional_int(payload.get("remaining_grok_max_daily_videos")),
            "remaining_grok_max_daily_720p_videos": cls._optional_int(payload.get("remaining_grok_max_daily_720p_videos")),
            "remaining_grok_max_daily_10s_videos": cls._optional_int(payload.get("remaining_grok_max_daily_10s_videos")),
            "remaining_grok_max_daily_15s_videos": cls._optional_int(payload.get("remaining_grok_max_daily_15s_videos")),
        }

    @classmethod
    def parse_image_gen_quota(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Image Gen quota payload must be an object")
        return {
            "is_image_gen_max": bool(payload.get("is_image_gen_max"))
            if payload.get("is_image_gen_max") is not None
            else None,
            "is_image_premium": bool(payload.get("is_image_premium"))
            if payload.get("is_image_premium") is not None
            else None,
            "image_gen_concurrent_streams": cls._optional_int(payload.get("concurrent_streams")),
            "remaining_image_gen_max_daily_images": cls._optional_int(
                payload.get("remaining_image_gen_max_daily_images")
            ),
            "remaining_image_gen_free_daily_images": cls._optional_int(
                payload.get("remaining_free_daily_images")
            ),
        }

    @classmethod
    def parse_grok_image_quota(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Grok Image quota payload must be an object")
        return {
            "is_grok_image_max": bool(payload.get("is_grok_image_max"))
            if payload.get("is_grok_image_max") is not None
            else None,
            "is_image_premium": bool(payload.get("is_image_premium"))
            if payload.get("is_image_premium") is not None
            else None,
            "grok_image_concurrent_streams": cls._optional_int(payload.get("concurrent_streams")),
            "remaining_grok_image_max_daily_images": cls._optional_int(
                payload.get("remaining_grok_image_max_daily_images")
            ),
            "remaining_grok_image_free_daily_images": cls._optional_int(
                payload.get("remaining_free_daily_images")
            ),
        }

    @classmethod
    def parse_grok_max_quota(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Grok Max quota payload must be an object")
        return {
            "is_grok_max": bool(payload.get("is_grok_max"))
            if payload.get("is_grok_max") is not None
            else None,
            "grok_max_concurrent_streams": cls._optional_int(payload.get("concurrent_streams")),
            "remaining_grok_max_daily_videos": cls._optional_int(
                payload.get("remaining_grok_max_daily_videos")
            ),
            "remaining_grok_max_daily_720p_videos": cls._optional_int(
                payload.get("remaining_grok_max_daily_720p_videos")
            ),
            "remaining_grok_max_daily_10s_videos": cls._optional_int(
                payload.get("remaining_grok_max_daily_10s_videos")
            ),
            "remaining_grok_max_daily_15s_videos": cls._optional_int(
                payload.get("remaining_grok_max_daily_15s_videos")
            ),
        }

    @staticmethod
    def _active_image_quota_remaining(updates: Dict[str, Any], bucket: str) -> Optional[int]:
        if bucket == "grok_image":
            is_max = updates.get("is_grok_image_max")
            max_remaining = updates.get("remaining_grok_image_max_daily_images")
            free_remaining = updates.get("remaining_grok_image_free_daily_images")
        else:
            is_max = updates.get("is_image_gen_max")
            max_remaining = updates.get("remaining_image_gen_max_daily_images")
            free_remaining = updates.get("remaining_image_gen_free_daily_images")
        if is_max is True:
            return max_remaining
        if is_max is False:
            return free_remaining
        return max_remaining if max_remaining is not None else free_remaining

    @staticmethod
    def is_account_profile_stale(account: GeminiGenAccount, *, now: Optional[datetime] = None) -> bool:
        synced_at = account.profile_synced_at
        if not synced_at:
            return True
        reference = now or datetime.now(timezone.utc).replace(tzinfo=None)
        if synced_at.tzinfo is not None:
            synced_at = synced_at.replace(tzinfo=None)
        if reference.tzinfo is not None:
            reference = reference.replace(tzinfo=None)
        return synced_at <= reference - GEMINIGEN_PROFILE_CACHE_TTL

    async def get_model_status(self, window: str = "1h") -> Dict[str, Any]:
        cfg = await self.db.get_geminigen_config()
        local_counts = self._local_model_counts_by_group()
        base_summary = {
            "operational": 0,
            "degraded": 0,
            "outage": 0,
            "unknown": 0,
            "matching_model_groups": 0,
        }
        accounts = await self.db.list_geminigen_accounts()
        image_in_flight = sum(int(account.image_in_flight or 0) for account in accounts)
        video_in_flight = sum(int(account.video_in_flight or 0) for account in accounts)
        active_accounts = len([account for account in accounts if account.is_active])

        if not cfg.enabled:
            return {
                "success": False,
                "status": "disabled",
                "error": "GeminiGen integration is disabled",
                "window": window,
                "generated_at": None,
                "models": [],
                "summary": base_summary,
                "geminigen": {
                    "enabled": False,
                    "active_account_count": active_accounts,
                    "image_in_flight": image_in_flight,
                    "video_in_flight": video_in_flight,
                },
            }

        account = await self._first_active_account()
        if not account:
            return {
                "success": False,
                "status": "unavailable",
                "error": "No active GeminiGen account configured",
                "window": window,
                "generated_at": None,
                "models": [],
                "summary": base_summary,
                "geminigen": {
                    "enabled": True,
                    "active_account_count": active_accounts,
                    "image_in_flight": image_in_flight,
                    "video_in_flight": video_in_flight,
                },
            }

        clean_window = (window or "1h").strip() or "1h"
        if clean_window not in {"1h", "6h", "24h", "7d"}:
            clean_window = "1h"
        path = "/api/v1/models/status"
        account = await self._ensure_fresh_account_token(account, cfg.base_url)
        proxy = await self._request_proxy()
        for attempt in range(2):
            async with AsyncSession() as session:
                response = await session.get(
                    f"{self._api_base_url(cfg.base_url)}{path}",
                    params={"window": clean_window},
                    headers=await self._headers(account, path, base_url=cfg.base_url, method="get"),
                    timeout=30,
                    proxy=proxy,
                    impersonate="chrome120",
                )
            if response.status_code < 400 or attempt or not self._token_expired_response(response):
                break
            account = await self._refresh_account_token(account, cfg.base_url)
        if response.status_code >= 400:
            raise RuntimeError(f"GeminiGen model status failed HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json() if response.text else {}
        raw_models = payload.get("models") if isinstance(payload, dict) else []
        if not isinstance(raw_models, list):
            raw_models = []

        rows: List[Dict[str, Any]] = []
        summary = dict(base_summary)
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            group_key = str(item.get("group_key") or item.get("model") or item.get("key") or "").strip()
            aliases = item.get("models") if isinstance(item.get("models"), list) else []
            matching_count = local_counts.get(group_key, 0)
            for alias in aliases:
                alias_key = str(alias or "").strip()
                if alias_key and alias_key != group_key:
                    matching_count += local_counts.get(alias_key, 0)
            status = str(item.get("status") or item.get("state") or "").strip() or "Unknown"
            bucket = self._status_bucket(status)
            if matching_count > 0:
                summary[bucket] = int(summary.get(bucket, 0)) + 1
                summary["matching_model_groups"] = int(summary.get("matching_model_groups", 0)) + 1
            rows.append(
                {
                    "model_name": str(item.get("model_name") or item.get("name") or group_key or "Unknown").strip(),
                    "group_key": group_key,
                    "type": str(item.get("type") or item.get("model_type") or "").strip(),
                    "success_rate": self._normalize_success_rate(item.get("success_rate")),
                    "status": status,
                    "status_bucket": bucket,
                    "updated_at": item.get("updated_at") or payload.get("generated_at"),
                    "generated_at": payload.get("generated_at"),
                    "matching_local_model_count": matching_count,
                }
            )

        return {
            "success": True,
            "status": "available",
            "window": clean_window,
            "generated_at": payload.get("generated_at"),
            "models": rows,
            "summary": summary,
            "geminigen": {
                "enabled": True,
                "active_account_count": active_accounts,
                "image_in_flight": image_in_flight,
                "video_in_flight": video_in_flight,
            },
        }

    @staticmethod
    def describe_credential(raw_cookie: str = "", bearer_token: str = "", guard_id: str = "") -> Dict[str, str]:
        if not (bearer_token or "").strip():
            return {"status": "missing_bearer_token", "error": "GeminiGen bearer token is required"}
        return {"status": "configured", "error": ""}

    async def _request_proxy(self) -> Optional[str]:
        if not self.proxy_manager:
            return None
        try:
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                return await self.proxy_manager.get_request_proxy_url()
            if hasattr(self.proxy_manager, "get_proxy_url"):
                return await self.proxy_manager.get_proxy_url()
        except Exception as exc:
            debug_logger.log_warning(f"GeminiGen proxy lookup failed: {exc}")
        return None

    @staticmethod
    def _api_base_url(base_url: str) -> str:
        base = (base_url or "").strip().rstrip("/") or "https://api.geminigen.ai"
        parsed = urlparse(base)
        host = (parsed.hostname or "").lower()
        if host in {"geminigen.ai", "api.geminigen.ai"}:
            return "https://api.geminigen.ai"
        if host in {"snapgen.ai", "api.snapgen.ai"}:
            return "https://api.snapgen.ai"
        return base

    @classmethod
    def _web_origin(cls, base_url: str) -> str:
        api_base = cls._api_base_url(base_url)
        parsed = urlparse(api_base)
        host = (parsed.hostname or "").lower()
        if host == "api.geminigen.ai":
            return "https://geminigen.ai"
        if host == "api.snapgen.ai":
            return "https://snapgen.ai"
        if host.startswith("api."):
            host = host[4:]
        if not host:
            return GEMINIGEN_ORIGIN
        scheme = parsed.scheme or "https"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{scheme}://{host}{port}"

    @staticmethod
    def _bearer_header(raw_token: str) -> str:
        token = (raw_token or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return f"Bearer {token}" if token else ""

    @staticmethod
    def _jwt_exp(access_token: str) -> Optional[int]:
        try:
            token = (access_token or "").strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
            exp = data.get("exp")
            return int(exp) if exp is not None else None
        except Exception:
            return None

    @classmethod
    def _needs_token_refresh(cls, account: GeminiGenAccount) -> bool:
        token = (account.bearer_token or "").strip()
        if not token:
            return True
        exp = cls._jwt_exp(token)
        if exp is None:
            return False
        return exp <= int(time.time()) + GEMINIGEN_REFRESH_BEFORE_EXPIRY_SEC

    @staticmethod
    def _token_expired_response(response: Any) -> bool:
        try:
            text = str(getattr(response, "text", "") or "")
        except Exception:
            text = ""
        return "TOKEN_EXPIRED" in text or "Token has been expired" in text

    async def _refresh_account_token(self, account: GeminiGenAccount, base_url: str) -> GeminiGenAccount:
        account_key = int(account.id or 0)
        lock = self._token_refresh_locks.setdefault(account_key, asyncio.Lock())
        async with lock:
            if account.id:
                fresh = await self.db.get_geminigen_account(int(account.id))
                if fresh:
                    account = fresh
                    if not self._needs_token_refresh(account):
                        return account
            return await self._refresh_account_token_unlocked(account, base_url)

    async def _refresh_account_token_unlocked(self, account: GeminiGenAccount, base_url: str) -> GeminiGenAccount:
        refresh_token = str(getattr(account, "refresh_token", "") or "").strip()
        if not refresh_token:
            raise RuntimeError("GeminiGen refresh token is required to refresh expired access token")
        path = "/api/refresh-token"
        proxy = await self._request_proxy()
        try:
            async with AsyncSession() as session:
                response = await session.post(
                    f"{self._api_base_url(base_url)}{path}",
                    headers=await self._headers(account, path, base_url=base_url, method="post"),
                    json={"refresh_token": refresh_token},
                    timeout=60,
                    proxy=proxy,
                    impersonate="chrome120",
                )
            if response.status_code >= 400:
                raise RuntimeError(f"GeminiGen refresh token failed HTTP {response.status_code}: {response.text[:300]}")
            payload = response.json() if response.text else {}
            access_token = str(payload.get("access_token") or "").strip()
            new_refresh_token = str(payload.get("refresh_token") or refresh_token).strip()
            if not access_token:
                raise RuntimeError("GeminiGen refresh response did not include access_token")
            if account.id:
                await self.db.update_geminigen_account(
                    int(account.id),
                    bearer_token=access_token,
                    refresh_token=new_refresh_token,
                    last_status="token_refreshed",
                    last_error="",
                )
                fresh = await self.db.get_geminigen_account(int(account.id))
                return fresh or account.model_copy(update={"bearer_token": access_token, "refresh_token": new_refresh_token})
            return account.model_copy(update={"bearer_token": access_token, "refresh_token": new_refresh_token})
        except Exception as exc:
            if account.id:
                await self.db.update_geminigen_account(int(account.id), last_status="failed", last_error=str(exc))
            raise

    async def _ensure_fresh_account_token(self, account: GeminiGenAccount, base_url: str) -> GeminiGenAccount:
        if self._needs_token_refresh(account) and str(getattr(account, "refresh_token", "") or "").strip():
            return await self._refresh_account_token(account, base_url)
        return account

    @staticmethod
    def _zg_hex(message: str) -> str:
        return hashlib.sha256(message.encode("utf-8")).hexdigest()

    @staticmethod
    def _rm_hex_pairs(hex_str: str) -> List[int]:
        return [int(hex_str[i : i + 2], 16) for i in range(0, len(hex_str), 2)]

    @staticmethod
    def _u32_be(n: int) -> List[int]:
        n = int(n) & 0xFFFFFFFF
        return [(n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255]

    @staticmethod
    def _base64url(payload: bytes) -> str:
        return base64.b64encode(payload).decode("ascii").replace("+", "-").replace("/", "_").rstrip("=")

    @staticmethod
    def _normalize_dom_fp_hex(dom_fp_hex: str) -> str:
        s = re.sub(r"[^0-9a-fA-F]", "", dom_fp_hex or "")
        if len(s) < 64:
            s = s.ljust(64, "0")
        if len(s) > 64:
            s = s[:64]
        return s.lower()

    @staticmethod
    def _valid_stable_id(stable_id: str) -> bool:
        return bool(
            isinstance(stable_id, str)
            and len(stable_id) == GEMINIGEN_GUARD_STABLE_ID_LEN
            and re.fullmatch(r"[A-Za-z0-9_-]+", stable_id)
        )

    async def _sync_guard_skew_ms(self, base_url: str = "") -> int:
        now = time.monotonic()
        if now - self._guard_skew_synced_at < 300:
            return self._guard_skew_ms
        async with self._guard_skew_lock:
            now = time.monotonic()
            if now - self._guard_skew_synced_at < 300:
                return self._guard_skew_ms
            t0 = int(time.time() * 1000)
            proxy = await self._request_proxy()
            origin = self._web_origin(base_url)
            health_url = f"{self._api_base_url(base_url)}/health"
            async with AsyncSession() as session:
                response = await session.get(
                    health_url,
                    headers={
                        "Accept": "*/*",
                        "Origin": origin,
                        "Referer": f"{origin}/",
                    },
                    timeout=30,
                    proxy=proxy,
                    impersonate="chrome120",
                )
            t1 = int(time.time() * 1000)
            server_time = response.headers.get("X-Server-Time") or response.headers.get("x-server-time")
            if server_time and str(server_time).strip():
                server_ms = int(str(server_time).strip())
            else:
                date_header = response.headers.get("Date") or response.headers.get("date")
                if not date_header:
                    self._guard_skew_ms = 0
                    self._guard_skew_synced_at = now
                    return 0
                server_ms = int(parsedate_to_datetime(date_header).timestamp() * 1000)
            self._guard_skew_ms = int(server_ms + ((t1 - t0) // 2) - t1)
            self._guard_skew_synced_at = time.monotonic()
            return self._guard_skew_ms

    async def _compute_x_guard_id(self, *, path: str, method: str, base_url: str = "") -> str:
        stable_id = GEMINIGEN_GUARD_STABLE_ID
        if not self._valid_stable_id(stable_id):
            raise RuntimeError("Invalid GeminiGen backend guard stable id")
        skew_ms = await self._sync_guard_skew_ms(base_url)
        bucket = (int(time.time() * 1000) + int(skew_ms)) // GEMINIGEN_TIME_BUCKET_WINDOW_MS
        dom_norm = self._normalize_dom_fp_hex(GEMINIGEN_DOM_FINGERPRINT_HEX)
        key_material = GEMINIGEN_ANTIBOT_SECRET_KEY
        u_prefix = self._zg_hex(f"{key_material}:{stable_id}")[:32]
        method_upper = (method or "get").upper()
        inner = self._zg_hex(f"{path}:{method_upper}:{u_prefix}:{bucket}:{key_material}")
        parts: List[int] = [GEMINIGEN_GUARD_VERSION_BYTE]
        parts.extend(self._rm_hex_pairs(u_prefix))
        parts.extend(self._u32_be(bucket))
        parts.extend(self._rm_hex_pairs(inner))
        parts.extend(self._rm_hex_pairs(dom_norm))
        return self._base64url(bytes(parts))

    async def _headers(
        self,
        account: GeminiGenAccount,
        path: str,
        *,
        base_url: str = "",
        method: str = "get",
        multipart: bool = False,
    ) -> Dict[str, str]:
        ua = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{GEMINIGEN_CHROME_MAJOR}.0.0.0 Safari/537.36"
        )
        sec_ch_ua = (
            f'"Google Chrome";v="{GEMINIGEN_CHROME_MAJOR}", "Not.A/Brand";v="8", '
            f'"Chromium";v="{GEMINIGEN_CHROME_MAJOR}"'
        )
        origin = self._web_origin(base_url)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-GB,en;q=0.9,ur-PK;q=0.8,ur;q=0.7,en-US;q=0.6",
            "Cache-Control": "no-cache",
            "Origin": origin,
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Referer": f"{origin}/",
            "Sec-CH-UA": sec_ch_ua,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": ua,
            "x-guard-id": await self._compute_x_guard_id(path=path, method=method, base_url=base_url),
        }
        bearer = GeminiGenService._bearer_header(account.bearer_token)
        if bearer:
            headers["Authorization"] = bearer
        if not multipart:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _extract_uuid(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("uuid", "id", "history_uuid", "task_id", "job_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("data", "result", "history"):
                try:
                    found = GeminiGenService._extract_uuid(payload.get(key))
                    if found:
                        return found
                except Exception:
                    pass
        if isinstance(payload, list):
            for item in payload:
                found = GeminiGenService._extract_uuid(item)
                if found:
                    return found
        raise RuntimeError("GeminiGen response did not include a history uuid")

    @staticmethod
    def _extract_status(payload: Dict[str, Any]) -> str:
        for key in ("status", "state", "generation_status"):
            value = str(payload.get(key) or "").lower()
            if value:
                return value
        return ""

    @staticmethod
    def is_geminigen_terminal_status(status: Any) -> bool:
        return str(status or "").strip().lower() in GEMINIGEN_TERMINAL_STATUSES

    @staticmethod
    def _history_cancelled(payload: Dict[str, Any], status_text: str) -> bool:
        if not isinstance(payload, dict):
            return False
        raw_status = str(payload.get("status") or "").strip().lower()
        if raw_status in {"4", "499"}:
            return True
        haystack = " ".join(
            str(value or "").lower()
            for value in (
                status_text,
                payload.get("error_code"),
                payload.get("error_message"),
                payload.get("error"),
                payload.get("message"),
            )
        )
        return any(marker in haystack for marker in GEMINIGEN_UPSTREAM_CANCEL_MARKERS)

    @staticmethod
    def _history_failed(payload: Dict[str, Any], status_text: str) -> bool:
        if not isinstance(payload, dict):
            return False
        raw_status = payload.get("status")
        try:
            if int(raw_status) == 3:
                return True
        except Exception:
            pass
        if str(payload.get("error_code") or "").strip():
            return True
        if str(payload.get("error_message") or "").strip():
            return True
        return any(x in (status_text or "") for x in GEMINIGEN_UPSTREAM_FAILURE_MARKERS)

    @staticmethod
    def _history_error_text(payload: Dict[str, Any], status_text: str) -> str:
        code = str(payload.get("error_code") or "").strip() if isinstance(payload, dict) else ""
        message = str(payload.get("error_message") or payload.get("error") or payload.get("message") or "").strip() if isinstance(payload, dict) else ""
        if code and message:
            return f"{code}: {message}"
        if message:
            return message
        if code:
            return code
        return status_text or "GeminiGen task failed"

    @staticmethod
    def _history_cancelled_text(payload: Dict[str, Any], status_text: str) -> str:
        message = str(payload.get("error_message") or payload.get("error") or payload.get("message") or "").strip() if isinstance(payload, dict) else ""
        return message or status_text or "GeminiGen task cancelled"

    @staticmethod
    def _public_status_details(task: GeminiGenTask) -> Dict[str, Optional[str]]:
        status = str(task.status or "").lower()
        job_phase: Optional[str]
        upstream_status: Optional[str] = None

        if status == "queued":
            job_phase = "queued"
        elif status == "processing":
            job_phase = "polling" if task.upstream_uuid else "submitting"
        elif status in GEMINIGEN_TERMINAL_STATUSES:
            job_phase = status
        else:
            job_phase = status or None

        raw_payload = task.response_payload
        if isinstance(raw_payload, str) and raw_payload.strip():
            try:
                payload = json.loads(raw_payload)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                upstream_status = (
                    str(payload.get("upstream_status") or payload.get("status") or payload.get("state") or payload.get("generation_status") or "").strip()
                    or None
                )
                nested = payload.get("upstream")
                if upstream_status is None and isinstance(nested, dict):
                    upstream_status = (
                        str(nested.get("status") or nested.get("state") or nested.get("generation_status") or "").strip()
                        or None
                    )

        return {"job_phase": job_phase, "upstream_status": upstream_status}

    @staticmethod
    def _history_progress(payload: Dict[str, Any], fallback: int) -> int:
        try:
            value = int(float(payload.get("status_percentage")))
            return max(int(fallback or 0), max(0, min(99, value)))
        except Exception:
            return max(int(fallback or 0), 10)

    @staticmethod
    def _walk_urls(value: Any, found: List[str]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lk = key.lower()
                if lk in {"image_url", "video_url", "file_download_url", "download_url", "url"} and isinstance(item, str):
                    if item.startswith(("http://", "https://")) and item not in found:
                        found.append(item)
                else:
                    GeminiGenService._walk_urls(item, found)
        elif isinstance(value, list):
            for item in value:
                GeminiGenService._walk_urls(item, found)

    @staticmethod
    def _walk_urls_for_keys(value: Any, keys: set[str], found: List[str]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lk = key.lower()
                if lk in keys and isinstance(item, str):
                    if item.startswith(("http://", "https://")) and item not in found:
                        found.append(item)
                else:
                    GeminiGenService._walk_urls_for_keys(item, keys, found)
        elif isinstance(value, list):
            for item in value:
                GeminiGenService._walk_urls_for_keys(item, keys, found)

    @staticmethod
    def _artifact_url_identity(url: str) -> str:
        parsed = urlparse(url or "")
        if not parsed.scheme or not parsed.netloc:
            return url
        host = (parsed.hostname or parsed.netloc).lower()
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path or "/"
        return f"{parsed.scheme.lower()}://{host}{port}{path}"

    @classmethod
    def _dedupe_artifact_urls(cls, urls: List[str]) -> List[str]:
        deduped: List[str] = []
        seen: set[str] = set()
        for url in urls:
            identity = cls._artifact_url_identity(url)
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(url)
        return deduped

    @classmethod
    def extract_artifact_urls(cls, payload: Dict[str, Any], kind: str) -> List[str]:
        final_urls: List[str] = []
        cls._walk_urls_for_keys(payload, {"file_download_url", "download_url"}, final_urls)
        if final_urls:
            return cls._dedupe_artifact_urls(final_urls)

        found: List[str] = []
        if kind == "video":
            cls._walk_urls(payload.get("generated_video"), found)
        else:
            cls._walk_urls(payload.get("generated_image"), found)
        cls._walk_urls(payload.get("result"), found)
        cls._walk_urls(payload.get("data"), found)
        return cls._dedupe_artifact_urls(found)

    def _cache_url(self, filename: str, base_url: Optional[str]) -> str:
        base = (base_url or "").strip().rstrip("/")
        builder = getattr(self.file_cache, "build_url", None)
        if callable(builder):
            result = builder(filename, base)
            if isinstance(result, str):
                return result
        return f"{base}/api/cache/blob/{quote(filename, safe='')}" if base else f"/api/cache/blob/{quote(filename, safe='')}"

    @staticmethod
    def _safe_log_json(value: Any) -> str:
        def scrub(item: Any) -> Any:
            if isinstance(item, dict):
                clean: Dict[str, Any] = {}
                for key, nested in item.items():
                    lk = str(key).lower()
                    if lk in {"raw_cookie", "cookie", "authorization", "bearer_token", "refresh_token", "guard_id", "turnstile_token"}:
                        clean[key] = "[redacted]"
                    elif lk in {"ref_images", "images"} and isinstance(nested, list):
                        clean[key] = [f"[media omitted #{idx + 1}]" for idx, _ in enumerate(nested)]
                    elif isinstance(nested, str) and nested.startswith("data:image/"):
                        clean[key] = f"[data URL omitted, length={len(nested)}]"
                    elif isinstance(nested, str) and len(nested) > 4096:
                        clean[key] = f"{nested[:800]}... [truncated, length={len(nested)}]"
                    else:
                        clean[key] = scrub(nested)
                return clean
            if isinstance(item, list):
                return [scrub(nested) for nested in item]
            if isinstance(item, bytes):
                return f"[binary media omitted, length={len(item)}]"
            if isinstance(item, str) and item.startswith("data:image/"):
                return f"[data URL omitted, length={len(item)}]"
            return item

        return json.dumps(scrub(value), ensure_ascii=False)

    async def _create_request_log(
        self,
        *,
        api_key_id: Optional[int],
        kind: str,
        public_model_id: str,
        endpoint_type: str,
        prompt: str,
        image_count: int,
        options: Dict[str, Any],
        job_id: str,
    ) -> int:
        operation = GEMINIGEN_OPERATION_BY_KIND.get(kind, "geminigen_image")
        request_body = self._safe_log_json(
            {
                "provider": "geminigen",
                "job_id": job_id,
                "model": public_model_id,
                "endpoint_type": endpoint_type,
                "prompt": prompt,
                "image_count": image_count,
                "options": options or {},
            }
        )
        return await self.db.add_request_log(
            RequestLog(
                token_id=None,
                api_key_id=api_key_id,
                operation=operation,
                request_body=request_body,
                response_body=self._safe_log_json({"status": "queued", "job_id": job_id}),
                status_code=102,
                duration=0,
                status_text="geminigen_queued",
                progress=0,
            )
        )

    async def _update_request_log(
        self,
        log_id: Optional[int],
        *,
        status_text: str,
        progress: int,
        status_code: int = 102,
        response: Optional[Dict[str, Any]] = None,
        duration: float = 0,
    ) -> None:
        if not log_id:
            return
        try:
            await self.db.update_request_log(
                int(log_id),
                response_body=self._safe_log_json(response or {"status": status_text}),
                status_code=int(status_code),
                duration=max(0.0, float(duration or 0)),
                status_text=status_text,
                progress=max(0, min(100, int(progress))),
            )
        except Exception as exc:
            debug_logger.log_warning(f"GeminiGen request log update failed: {exc}")

    @staticmethod
    def _task_duration(task: Optional[GeminiGenTask]) -> float:
        if not task or not task.created_at:
            return 0.0
        try:
            return max(0.0, (datetime.utcnow() - task.created_at.replace(tzinfo=None)).total_seconds())
        except Exception:
            return 0.0

    async def _cache_artifacts(self, urls: List[str], *, kind: str, api_key_id: Optional[int], base_url: Optional[str], enabled: bool) -> List[str]:
        if not enabled:
            return []
        cached: List[str] = []
        for raw_url in urls:
            try:
                filename = await self.file_cache.download_and_cache(
                    raw_url,
                    media_type="video" if kind == "video" else "image",
                    api_key_id=api_key_id,
                    token_id=None,
                    flow_project_id=None,
                )
                cached.append(self._cache_url(filename, base_url))
            except Exception as exc:
                debug_logger.log_warning(f"GeminiGen artifact cache failed: {exc}")
        return cached

    def _finalize_lock_for_job(self, job_id: str) -> asyncio.Lock:
        locks = getattr(self, "_finalize_locks", None)
        if locks is None:
            locks = {}
            self._finalize_locks = locks
        lock = locks.get(job_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[job_id] = lock
        return lock

    @staticmethod
    def _data_url_from_image(image: bytes) -> str:
        mime_type = "image/png"
        if image.startswith(b"\xff\xd8\xff"):
            mime_type = "image/jpeg"
        elif image.startswith(b"RIFF") and image[8:12] == b"WEBP":
            mime_type = "image/webp"
        return f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"

    @staticmethod
    def _image_file_metadata(image: bytes) -> tuple[str, str]:
        if image.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", "jpg"
        if image.startswith(b"RIFF") and image[8:12] == b"WEBP":
            return "image/webp", "webp"
        return "image/png", "png"

    def _build_form(self, *, public_model_id: str, prompt: str, images: List[bytes], options: Dict[str, Any], extra_options: Dict[str, Any], account: GeminiGenAccount) -> Dict[str, Any]:
        endpoint_type = options["endpoint_type"]
        merged = dict(options.get("options") or {})
        merged.update({k: v for k, v in (extra_options or {}).items() if v is not None})
        form: Dict[str, Any] = {"prompt": prompt, "turnstile_token": "skip"}

        if endpoint_type == "imagen":
            form.update(
                {
                    "model": merged.get("model", "nano-banana-2"),
                    "aspect_ratio": merged.get("aspect_ratio", "16:9"),
                    "resolution": str(merged.get("resolution", "4K")).upper(),
                    "output_format": merged.get("output_format", "png"),
                }
            )
            if images:
                form["ref_images"] = [self._data_url_from_image(img) for img in images]
        elif endpoint_type == "grok-image":
            form.update(
                {
                    "orientation": merged.get("orientation", "landscape"),
                    "num_result": str(max(1, min(6, int(merged.get("num_result") or 1)))),
                    "mode": merged.get("mode", "normal"),
                }
            )
            if images:
                form["ref_images"] = [self._data_url_from_image(img) for img in images]
        elif endpoint_type == "veo-video":
            form.update(
                {
                    "model": merged.get("model", "veo-3-fast"),
                    "aspect_ratio": merged.get("aspect_ratio", "16:9"),
                    "duration": str(merged.get("duration", "8")),
                    "resolution": merged.get("resolution", "720p"),
                    "service_mode": merged.get("service_mode", "unstable"),
                }
            )
            ref_mode = merged.get("reference_mode")
            if images:
                refs = images[:2] if ref_mode == "frame" else images
                if ref_mode in {"frame", "ingredient"}:
                    form["mode_image"] = ref_mode
                if ref_mode == "ingredient":
                    tags = " ".join(f"@image{index}" for index in range(1, len(refs) + 1))
                    if tags and not any(f"@image{index}" in prompt for index in range(1, len(refs) + 1)):
                        form["prompt"] = f"{tags} {prompt}".strip()
                form["_file_parts"] = [
                    {
                        "name": "ref_images",
                        "data": image,
                        "filename": f"ref_image_{index}.{extension}",
                        "content_type": content_type,
                    }
                    for index, image in enumerate(refs, start=1)
                    for content_type, extension in [self._image_file_metadata(image)]
                ]
            if "negative_prompt" in merged:
                form["negative_prompt"] = merged["negative_prompt"]
            if "enhance_prompt" in merged:
                form["enhance_prompt"] = str(bool(merged["enhance_prompt"])).lower()
        return form

    @staticmethod
    def _endpoint_path(endpoint_type: str) -> str:
        return {
            "imagen": "/api/generate_image",
            "grok-image": "/api/imagen/grok",
            "veo-video": "/api/video-gen/veo",
        }[endpoint_type]

    @staticmethod
    def _extract_upstream_error(status_code: int, body: str) -> GeminiGenUpstreamError:
        error_code: Optional[str] = None
        error_message: Optional[str] = None
        parsed: Any = None
        try:
            parsed = json.loads(body or "{}")
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            detail = parsed.get("detail")
            if isinstance(detail, dict):
                error_code = detail.get("error_code") or detail.get("code")
                error_message = detail.get("error_message") or detail.get("message") or detail.get("error")
            error_code = error_code or parsed.get("error_code") or parsed.get("code")
            error_message = error_message or parsed.get("error_message") or parsed.get("message") or parsed.get("error")
        lower_error_message = str(error_message or "").lower()
        normalized_error_code = str(error_code or "").upper()
        daily_limit_exceeded = (
            normalized_error_code == GEMINIGEN_DAILY_LIMIT_ERROR_CODE
            or (
                status_code == 422
                and "daily limit" in lower_error_message
                and ("reached" in lower_error_message or "exceeded" in lower_error_message)
            )
        )
        retryable_capacity = (
            normalized_error_code == GEMINIGEN_CAPACITY_ERROR_CODE
            or (
                status_code == 400
                and "maximum number" in lower_error_message
                and "concurrent image generation" in lower_error_message
            )
        )
        if daily_limit_exceeded:
            message = (
                f"GeminiGen account daily limit exceeded ({GEMINIGEN_DAILY_LIMIT_ERROR_CODE}); "
                "resets at 00:00 UTC"
            )
        elif retryable_capacity:
            message = f"GeminiGen upstream capacity is full ({GEMINIGEN_CAPACITY_ERROR_CODE})"
        else:
            suffix = f": {str(error_message).strip()[:240]}" if error_message else ""
            message = f"GeminiGen upstream request failed (HTTP {status_code}){suffix}"
        return GeminiGenUpstreamError(
            status_code=status_code,
            message=message,
            error_code=str(error_code) if error_code else None,
            retryable_capacity=retryable_capacity,
            daily_limit_exceeded=daily_limit_exceeded,
        )

    @staticmethod
    def _next_utc_midnight(now: Optional[datetime] = None) -> datetime:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        return (current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).replace(tzinfo=None)

    @staticmethod
    def _utc_iso(value: Optional[datetime]) -> Optional[str]:
        if not value:
            return None
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        else:
            normalized = normalized.astimezone(timezone.utc)
        return normalized.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _quota_bucket(kind: str, endpoint_type: str) -> str:
        endpoint = str(endpoint_type or "").strip().lower()
        if endpoint == "grok-image":
            return "grok_image"
        if endpoint == "veo-video" or str(kind or "").lower() == "video":
            return "video"
        return "image_gen"

    @classmethod
    def _daily_limit_reset_field(cls, kind: str, endpoint_type: str) -> str:
        return {
            "image_gen": "image_gen_daily_limit_reset_at",
            "grok_image": "grok_image_daily_limit_reset_at",
            "video": "video_daily_limit_reset_at",
        }[cls._quota_bucket(kind, endpoint_type)]

    @classmethod
    def _quota_bucket_label(cls, kind: str, endpoint_type: str) -> str:
        return {
            "image_gen": "Imagen",
            "grok_image": "Grok Image",
            "video": "video",
        }[cls._quota_bucket(kind, endpoint_type)]

    async def _daily_limit_pool_state(self, kind: str, endpoint_type: str) -> Dict[str, Any]:
        getter = getattr(self.db, "get_geminigen_daily_limit_pool_state", None)
        if not callable(getter):
            return {"account_count": 0, "daily_limited_count": 0, "earliest_reset_at": None}
        try:
            state = await getter(kind, endpoint_type=endpoint_type)
        except TypeError:
            state = await getter(kind)
        reset_at = self._optional_datetime(state.get("earliest_reset_at"))
        return {
            "account_count": int(state.get("account_count") or 0),
            "daily_limited_count": int(state.get("daily_limited_count") or 0),
            "earliest_reset_at": reset_at,
        }

    async def _fail_task_for_daily_limit_pool(
        self,
        task: GeminiGenTask,
        *,
        request_log_id: Optional[int],
        started_at: Optional[float],
        reset_at: Optional[datetime],
    ) -> GeminiGenTask:
        retry_at = reset_at or self._next_utc_midnight()
        retry_at_text = self._utc_iso(retry_at)
        bucket_label = self._quota_bucket_label(task.kind, task.endpoint_type)
        error = (
            f"All GeminiGen accounts reached their {bucket_label} daily limit; "
            f"retry after {retry_at_text}"
        )
        await self.db.update_geminigen_task(
            task.job_id,
            account_id=None,
            upstream_uuid=None,
            status="failed",
            progress=0,
            error_message=error,
            error_code=GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
            retry_at=retry_at,
            completed_at=datetime.utcnow(),
        )
        await self._update_request_log(
            request_log_id,
            status_text="failed",
            progress=0,
            status_code=429,
            response={
                "status": "failed",
                "job_id": task.job_id,
                "error_message": error,
                "error_code": GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                "retry_at": retry_at_text,
            },
            duration=time.perf_counter() - (started_at or time.perf_counter()),
        )
        return await self.db.get_geminigen_task(task.job_id) or task.model_copy(
            update={
                "account_id": None,
                "status": "failed",
                "progress": 0,
                "error_message": error,
                "error_code": GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                "retry_at": retry_at,
            }
        )

    def _capacity_key(self, account_id: Optional[int], kind: str) -> Optional[Tuple[int, str]]:
        if not account_id:
            return None
        normalized_kind = "video" if str(kind or "").lower() == "video" else "image"
        return int(account_id), normalized_kind

    def _active_capacity_cooldowns(self, kind: str) -> Dict[int, float]:
        now = time.monotonic()
        normalized_kind = "video" if str(kind or "").lower() == "video" else "image"
        active: Dict[int, float] = {}
        expired: List[Tuple[int, str]] = []
        for key, until in self._capacity_cooldowns.items():
            if until <= now:
                expired.append(key)
                continue
            account_id, cooldown_kind = key
            if cooldown_kind == normalized_kind:
                active[account_id] = until
        for key in expired:
            self._capacity_cooldowns.pop(key, None)
            self._capacity_cooldown_attempts.pop(key, None)
        return active

    def _set_capacity_cooldown(self, account_id: Optional[int], kind: str) -> float:
        key = self._capacity_key(account_id, kind)
        if not key:
            return 1.0
        attempts = self._capacity_cooldown_attempts.get(key, 0) + 1
        self._capacity_cooldown_attempts[key] = attempts
        base = min(
            GEMINIGEN_CAPACITY_COOLDOWN_MAX_SEC,
            GEMINIGEN_CAPACITY_COOLDOWN_INITIAL_SEC * (2 ** max(0, attempts - 1)),
        )
        jitter = random.uniform(0.0, min(5.0, base * 0.1))
        delay = min(GEMINIGEN_CAPACITY_COOLDOWN_MAX_SEC, base + jitter)
        self._capacity_cooldowns[key] = time.monotonic() + delay
        return delay

    def _clear_capacity_cooldown(self, account_id: Optional[int], kind: str) -> None:
        key = self._capacity_key(account_id, kind)
        if not key:
            return
        self._capacity_cooldowns.pop(key, None)
        self._capacity_cooldown_attempts.pop(key, None)

    async def _sleep_for_capacity_or_slot(self, *, kind: str, deadline: float) -> None:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            return
        active = self._active_capacity_cooldowns(kind)
        if active:
            next_ready = max(0.0, min(active.values()) - time.monotonic())
            await asyncio.sleep(min(1.0, next_ready, remaining))
        else:
            await asyncio.sleep(min(1.0, remaining))

    async def _acquire_geminigen_account(self, kind: str, endpoint_type: str) -> Optional[GeminiGenAccount]:
        excluded = list(self._active_capacity_cooldowns(kind).keys())
        try:
            account = await self.db.acquire_geminigen_account(
                kind,
                excluded_account_ids=excluded,
                endpoint_type=endpoint_type,
            )
        except TypeError:
            try:
                account = await self.db.acquire_geminigen_account(
                    kind,
                    excluded_account_ids=excluded,
                )
            except TypeError:
                account = await self.db.acquire_geminigen_account(kind)
                if account and account.id in excluded:
                    await self.db.release_geminigen_account(account.id, kind)
                    return None
        if account and hasattr(self, "_profile_refreshing_account_ids"):
            self.schedule_stale_account_profile_refresh([account])
        return account

    async def _post_generation(self, *, account: GeminiGenAccount, base_url: str, endpoint_type: str, form: Dict[str, Any]) -> Dict[str, Any]:
        path = self._endpoint_path(endpoint_type)
        url = f"{self._api_base_url(base_url)}{path}"
        proxy = await self._request_proxy()
        account = await self._ensure_fresh_account_token(account, base_url)
        for attempt in range(2):
            multipart = CurlMime()
            file_parts = form.get("_file_parts") or []
            for key, value in form.items():
                if key.startswith("_"):
                    continue
                if isinstance(value, list):
                    for item in value:
                        multipart.addpart(name=key, data=str(item).encode("utf-8"))
                else:
                    multipart.addpart(name=key, data=str(value).encode("utf-8"))
            for part in file_parts:
                multipart.addpart(
                    name=part["name"],
                    data=part["data"],
                    filename=part["filename"],
                    content_type=part["content_type"],
                )
            try:
                async with AsyncSession() as session:
                    response = await session.post(
                        url,
                        headers=await self._headers(
                            account,
                            path,
                            base_url=base_url,
                            method="post",
                            multipart=True,
                        ),
                        multipart=multipart,
                        timeout=120,
                        proxy=proxy,
                        impersonate="chrome120",
                    )
            finally:
                multipart.close()
            if response.status_code < 400 or attempt or not self._token_expired_response(response):
                break
            account = await self._refresh_account_token(account, base_url)
        if response.status_code >= 400:
            raise self._extract_upstream_error(response.status_code, response.text[:2000])
        text = response.text or "{}"
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
        return json.loads(text) if text else {}

    async def _get_history(self, *, account: GeminiGenAccount, base_url: str, upstream_uuid: str) -> Dict[str, Any]:
        path = f"/api/history/{upstream_uuid}"
        proxy = await self._request_proxy()
        account = await self._ensure_fresh_account_token(account, base_url)
        for attempt in range(2):
            async with AsyncSession() as session:
                response = await session.get(
                    f"{self._api_base_url(base_url)}{path}",
                    headers=await self._headers(account, path, base_url=base_url, method="get"),
                    timeout=60,
                    proxy=proxy,
                    impersonate="chrome120",
                )
            if response.status_code < 400 or attempt or not self._token_expired_response(response):
                break
            account = await self._refresh_account_token(account, base_url)
        if response.status_code >= 400:
            raise RuntimeError(f"GeminiGen GET {path} failed HTTP {response.status_code}: {response.text[:500]}")
        return response.json() if response.text else {}

    async def start_task(
        self,
        *,
        public_model_id: str,
        prompt: str,
        images: Optional[List[bytes]] = None,
        options: Optional[Dict[str, Any]] = None,
        api_key_id: Optional[int] = None,
    ) -> GeminiGenTask:
        queued = await self.enqueue_task(
            public_model_id=public_model_id,
            prompt=prompt,
            images=images,
            options=options,
            api_key_id=api_key_id,
        )
        started_at = time.perf_counter()
        try:
            return await self._start_queued_task(
                queued.job_id,
                images=images or [],
                options=options or {},
                request_log_id=queued.request_log_id,
                started_at=started_at,
            )
        except Exception as exc:
            await self._update_request_log(
                queued.request_log_id,
                status_text="failed",
                progress=0,
                status_code=502,
                response={"status": "failed", "job_id": queued.job_id, "error_message": str(exc)},
                duration=time.perf_counter() - started_at,
            )
            raise

    async def enqueue_task(
        self,
        *,
        public_model_id: str,
        prompt: str,
        images: Optional[List[bytes]] = None,
        options: Optional[Dict[str, Any]] = None,
        api_key_id: Optional[int] = None,
    ) -> GeminiGenTask:
        """Persist a GeminiGen job as queued without waiting for an upstream slot."""
        manifest = geminigen_manifest_entry(public_model_id)
        if not manifest:
            raise RuntimeError(f"GeminiGen model does not exist: {public_model_id}")
        cfg = await self.db.get_geminigen_config()
        if not cfg.enabled:
            raise RuntimeError("GeminiGen integration is disabled")
        kind = str(manifest["kind"])
        if kind == "video" and not bool(getattr(cfg, "video_enabled", True)):
            raise RuntimeError("GeminiGen video mode is disabled")
        job_id = f"geminigen-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        queued = GeminiGenTask(
            job_id=job_id,
            api_key_id=api_key_id,
            public_model_id=public_model_id,
            kind=kind,
            endpoint_type=str(manifest["endpoint_type"]),
            prompt=prompt,
            status="queued",
            progress=0,
            request_payload=json.dumps({"images": len(images or []), "options": options or {}}, ensure_ascii=False),
        )
        request_log = RequestLog(
            token_id=None,
            api_key_id=api_key_id,
            operation=GEMINIGEN_OPERATION_BY_KIND.get(kind, "geminigen_image"),
            request_body=self._safe_log_json(
                {
                    "provider": "geminigen",
                    "job_id": job_id,
                    "model": public_model_id,
                    "endpoint_type": str(manifest["endpoint_type"]),
                    "prompt": prompt,
                    "image_count": len(images or []),
                    "options": options or {},
                }
            ),
            response_body=self._safe_log_json({"status": "queued", "job_id": job_id}),
            status_code=102,
            duration=0,
            status_text="geminigen_queued",
            progress=0,
        )
        queued.request_log_id = await self.db.create_geminigen_task_with_request_log(
            queued,
            request_log,
        )
        return queued

    async def start_and_complete_queued_task_in_background(
        self,
        job_id: str,
        *,
        images: List[bytes],
        options: Dict[str, Any],
        api_key_id: Optional[int],
        base_url: Optional[str],
    ) -> None:
        """Wait for capacity, submit a queued task, then poll it to completion."""
        if not await self._claim_background_completion(job_id):
            return
        started_at = time.perf_counter()
        try:
            task = await self.db.get_geminigen_task(job_id)
            if not task:
                return
            await self._start_queued_task(
                job_id,
                images=images,
                options=options,
                request_log_id=task.request_log_id,
                started_at=started_at,
            )
            await self.wait_for_task(job_id, api_key_id=api_key_id, base_url=base_url)
        except Exception as exc:
            debug_logger.log_warning(f"GeminiGen queued background task failed for {job_id}: {exc}")
        finally:
            await self._release_background_completion(job_id)

    async def _start_queued_task(
        self,
        job_id: str,
        *,
        images: List[bytes],
        options: Dict[str, Any],
        request_log_id: Optional[int] = None,
        started_at: Optional[float] = None,
    ) -> GeminiGenTask:
        task = await self.db.get_geminigen_task(job_id)
        if not task:
            raise RuntimeError("GeminiGen task not found")
        cfg = await self.db.get_geminigen_config()
        timeout = cfg.timeout_video_sec if task.kind == "video" else cfg.timeout_image_sec
        deadline = time.monotonic() + float(timeout)
        saw_capacity = False
        last_capacity_error: Optional[GeminiGenUpstreamError] = None
        manifest = geminigen_manifest_entry(task.public_model_id)
        while time.monotonic() < deadline:
            account = await self._acquire_geminigen_account(task.kind, task.endpoint_type)
            if not account:
                daily_pool = await self._daily_limit_pool_state(task.kind, task.endpoint_type)
                if (
                    daily_pool["account_count"] > 0
                    and daily_pool["daily_limited_count"] >= daily_pool["account_count"]
                ):
                    return await self._fail_task_for_daily_limit_pool(
                        task,
                        request_log_id=request_log_id,
                        started_at=started_at,
                        reset_at=daily_pool["earliest_reset_at"],
                    )
                active_capacity = self._active_capacity_cooldowns(task.kind)
                reason = "upstream_capacity_cooldown" if active_capacity else "waiting_for_account_slot"
                await self._update_request_log(
                    request_log_id,
                    status_text="geminigen_queued",
                    progress=0,
                    response={"status": "queued", "job_id": job_id, "reason": reason},
                    duration=time.perf_counter() - (started_at or time.perf_counter()),
                )
                await self._sleep_for_capacity_or_slot(kind=task.kind, deadline=deadline)
                continue

            await self._update_request_log(
                request_log_id,
                status_text="geminigen_account_selected",
                progress=1,
                response={"status": "account_selected", "job_id": job_id, "account_id": account.id},
                duration=time.perf_counter() - (started_at or time.perf_counter()),
            )
            release_now = False
            try:
                form = self._build_form(
                    public_model_id=task.public_model_id,
                    prompt=task.prompt,
                    images=images,
                    options=manifest or {},
                    extra_options=options,
                    account=account,
                )
                await self.db.update_geminigen_task(
                    job_id,
                    account_id=account.id,
                    status="processing",
                    progress=1,
                    error_message=None,
                    completed_at=None,
                    started_at=datetime.utcnow(),
                    request_payload=self._safe_log_json(form),
                )
                await self._update_request_log(
                    request_log_id,
                    status_text="geminigen_submitting",
                    progress=3,
                    response={
                        "status": "submitting",
                        "job_id": job_id,
                        "account_id": account.id,
                        "endpoint_type": task.endpoint_type,
                        "form": form,
                    },
                    duration=time.perf_counter() - (started_at or time.perf_counter()),
                )
                created = await self._post_generation(
                    account=account,
                    base_url=cfg.base_url,
                    endpoint_type=task.endpoint_type,
                    form=form,
                )
                upstream_uuid = self._extract_uuid(created)
                self._clear_capacity_cooldown(account.id, task.kind)
                await self.db.update_geminigen_task(
                    job_id,
                    upstream_uuid=upstream_uuid,
                    response_payload=json.dumps(created, ensure_ascii=False),
                    progress=5,
                    error_code=None,
                    retry_at=None,
                )
                await self.db.update_geminigen_account(
                    account.id or 0,
                    last_status="submitted",
                    last_error=None,
                )
                decrement_quota = getattr(self.db, "decrement_geminigen_cached_quota", None)
                if callable(decrement_quota):
                    await decrement_quota(account.id or 0, task.endpoint_type)
                await self._update_request_log(
                    request_log_id,
                    status_text="geminigen_submitted",
                    progress=5,
                    response={"status": "submitted", "job_id": job_id, "upstream_uuid": upstream_uuid, "upstream": created},
                    duration=time.perf_counter() - (started_at or time.perf_counter()),
                )
                return await self.db.get_geminigen_task(job_id) or task
            except GeminiGenUpstreamError as exc:
                if exc.daily_limit_exceeded:
                    release_now = True
                    reset_at = self._next_utc_midnight()
                    reset_at_text = self._utc_iso(reset_at)
                    quota_bucket = self._quota_bucket(task.kind, task.endpoint_type)
                    daily_limit_field = self._daily_limit_reset_field(task.kind, task.endpoint_type)
                    await self.db.update_geminigen_task(
                        job_id,
                        account_id=None,
                        upstream_uuid=None,
                        status="queued",
                        progress=0,
                        error_message=None,
                        error_code=None,
                        retry_at=None,
                    )
                    await self.db.update_geminigen_account(
                        account.id or 0,
                        **{
                            daily_limit_field: reset_at,
                            "last_status": f"daily_limited_{quota_bucket}",
                            "last_error": GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                        },
                    )
                    await self._update_request_log(
                        request_log_id,
                        status_text="geminigen_queued",
                        progress=0,
                        response={
                            "status": "queued",
                            "job_id": job_id,
                            "reason": "account_daily_limit",
                            "account_id": account.id,
                            "kind": task.kind,
                            "endpoint_type": task.endpoint_type,
                            "quota_bucket": quota_bucket,
                            "upstream_error_code": exc.error_code or GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                            "reset_at": reset_at_text,
                        },
                        duration=time.perf_counter() - (started_at or time.perf_counter()),
                    )
                    continue
                if exc.retryable_capacity:
                    saw_capacity = True
                    last_capacity_error = exc
                    release_now = True
                    cooldown_sec = self._set_capacity_cooldown(account.id, task.kind)
                    await self.db.update_geminigen_task(
                        job_id,
                        account_id=None,
                        upstream_uuid=None,
                        status="queued",
                        progress=0,
                        error_message=None,
                        error_code=None,
                        retry_at=None,
                    )
                    await self.db.update_geminigen_account(
                        account.id or 0,
                        last_status="capacity_limited",
                        last_error=exc.error_code or str(exc),
                    )
                    await self._update_request_log(
                        request_log_id,
                        status_text="geminigen_queued",
                        progress=0,
                        response={
                            "status": "queued",
                            "job_id": job_id,
                            "reason": "upstream_capacity_full",
                            "account_id": account.id,
                            "upstream_error_code": exc.error_code,
                            "cooldown_sec": round(cooldown_sec, 2),
                        },
                        duration=time.perf_counter() - (started_at or time.perf_counter()),
                    )
                    continue
                release_now = True
                await self.db.update_geminigen_task(job_id, status="failed", error_message=str(exc), completed_at=datetime.utcnow())
                await self.db.update_geminigen_account(account.id or 0, last_status="failed", last_error=exc.error_code or str(exc))
                await self._update_request_log(
                    request_log_id,
                    status_text="failed",
                    progress=task.progress,
                    status_code=502,
                    response={"status": "failed", "job_id": job_id, "error_message": str(exc), "upstream_error_code": exc.error_code},
                    duration=time.perf_counter() - (started_at or time.perf_counter()),
                )
                raise
            except Exception as exc:
                release_now = True
                await self.db.update_geminigen_task(job_id, status="failed", error_message=str(exc), completed_at=datetime.utcnow())
                await self.db.update_geminigen_account(account.id or 0, last_status="failed", last_error=str(exc))
                await self._update_request_log(
                    request_log_id,
                    status_text="failed",
                    progress=task.progress,
                    status_code=502,
                    response={"status": "failed", "job_id": job_id, "error_message": str(exc)},
                    duration=time.perf_counter() - (started_at or time.perf_counter()),
                )
                raise
            finally:
                if release_now:
                    await self.db.release_geminigen_account(account.id, task.kind)

        if saw_capacity:
            error = "GeminiGen capacity is still full; generation did not start before timeout"
            last_detail = last_capacity_error.error_code if last_capacity_error else GEMINIGEN_CAPACITY_ERROR_CODE
            status_response = {
                "status": "failed",
                "job_id": job_id,
                "error_message": error,
                "upstream_error_code": last_detail,
            }
        else:
            error = "GeminiGen queue timed out waiting for an available account slot"
            status_response = {"status": "failed", "job_id": job_id, "error_message": error}
        await self.db.update_geminigen_task(job_id, status="failed", error_message=error, completed_at=datetime.utcnow())
        await self._update_request_log(
            request_log_id,
            status_text="failed",
            progress=0,
            status_code=504,
            response=status_response,
            duration=time.perf_counter() - (started_at or time.perf_counter()),
        )
        raise RuntimeError(error)

    async def poll_task(self, job_id: str, *, api_key_id: Optional[int] = None, base_url: Optional[str] = None) -> GeminiGenTask:
        task = await self.db.get_geminigen_task(job_id)
        if not task:
            raise KeyError("GeminiGen job not found")
        if api_key_id is not None and task.api_key_id is not None and int(api_key_id) != int(task.api_key_id):
            raise PermissionError("Not authorized to view this GeminiGen job")
        if self.is_geminigen_terminal_status(task.status):
            return task
        if task.status == "queued":
            return task
        if task.status == "processing" and not task.upstream_uuid:
            # Submission is in flight. Do not poll GeminiGen history until its
            # upstream UUID has been persisted by the submitter.
            return task
        account = await self.db.get_geminigen_account(int(task.account_id or 0))
        if not account:
            await self.db.update_geminigen_task(job_id, status="failed", error_message="GeminiGen account no longer exists", completed_at=datetime.utcnow())
            return await self.db.get_geminigen_task(job_id) or task
        cfg = await self.db.get_geminigen_config()
        try:
            payload = await self._get_history(account=account, base_url=cfg.base_url, upstream_uuid=task.upstream_uuid or "")
            status_text = self._extract_status(payload)
            cancelled = self._history_cancelled(payload, status_text)
            failed = False if cancelled else self._history_failed(payload, status_text)
            urls = self.extract_artifact_urls(payload, task.kind)
            completed = bool(urls) or any(x in status_text for x in ("complete", "success", "finished"))
            if completed:
                async with self._finalize_lock_for_job(job_id):
                    fresh_task = await self.db.get_geminigen_task(job_id)
                    if fresh_task and self.is_geminigen_terminal_status(fresh_task.status):
                        return fresh_task
                    if fresh_task:
                        task = fresh_task
                    if not urls:
                        error_text = "GeminiGen completed but did not return any artifact URLs"
                        await self.db.update_geminigen_task(
                            job_id,
                            status="failed",
                            progress=task.progress,
                            error_message=error_text,
                            response_payload=json.dumps(payload, ensure_ascii=False),
                            completed_at=datetime.utcnow(),
                        )
                        await self._update_request_log(
                            task.request_log_id,
                            status_text="failed",
                            progress=task.progress,
                            status_code=502,
                            response={
                                "status": "failed",
                                "job_id": job_id,
                                "upstream_uuid": task.upstream_uuid,
                                "error_message": error_text,
                                "raw_artifact_urls": [],
                                "cached_artifact_urls": [],
                                "result_urls": [],
                            },
                            duration=self._task_duration(task),
                        )
                        await self.db.release_geminigen_account(task.account_id, task.kind)
                        return await self.db.get_geminigen_task(job_id) or task
                    await self._update_request_log(
                        task.request_log_id,
                        status_text="caching_video" if task.kind == "video" else "caching_image",
                        progress=90,
                        response={"status": "caching", "job_id": job_id, "raw_artifact_urls": urls},
                        duration=self._task_duration(task),
                    )
                    cached = await self._cache_artifacts(
                        urls,
                        kind=task.kind,
                        api_key_id=task.api_key_id,
                        base_url=base_url,
                        enabled=bool(global_config.cache_enabled and getattr(cfg, "cache_outputs", True)),
                    )
                    result_urls = cached or urls
                    await self.db.update_geminigen_task(
                        job_id,
                        status="completed",
                        progress=100,
                        raw_artifact_urls=urls,
                        cached_artifact_urls=cached,
                        response_payload=json.dumps(payload, ensure_ascii=False),
                        completed_at=datetime.utcnow(),
                    )
                    await self._update_request_log(
                        task.request_log_id,
                        status_text="completed",
                        progress=100,
                        status_code=200,
                        response={
                            "status": "completed",
                            "job_id": job_id,
                            "upstream_uuid": task.upstream_uuid,
                            "raw_artifact_urls": urls,
                            "cached_artifact_urls": cached,
                            "result_urls": result_urls,
                        },
                        duration=self._task_duration(task),
                    )
                    await self.db.release_geminigen_account(task.account_id, task.kind)
            elif cancelled:
                error_text = self._history_cancelled_text(payload, status_text)
                await self.db.update_geminigen_task(
                    job_id,
                    status="cancelled",
                    error_message=error_text,
                    response_payload=json.dumps(payload, ensure_ascii=False),
                    completed_at=datetime.utcnow(),
                )
                await self._update_request_log(
                    task.request_log_id,
                    status_text="cancelled",
                    progress=task.progress,
                    status_code=499,
                    response={"status": "cancelled", "job_id": job_id, "upstream_uuid": task.upstream_uuid, "error_message": error_text},
                    duration=self._task_duration(task),
                )
                await self.db.release_geminigen_account(task.account_id, task.kind)
            elif failed:
                error_text = self._history_error_text(payload, status_text)
                await self.db.update_geminigen_task(job_id, status="failed", error_message=error_text, response_payload=json.dumps(payload, ensure_ascii=False), completed_at=datetime.utcnow())
                await self._update_request_log(
                    task.request_log_id,
                    status_text="failed",
                    progress=task.progress,
                    status_code=502,
                    response={"status": "failed", "job_id": job_id, "upstream_uuid": task.upstream_uuid, "error_message": error_text},
                    duration=self._task_duration(task),
                )
                await self.db.release_geminigen_account(task.account_id, task.kind)
            else:
                next_progress = self._history_progress(payload, task.progress)
                previous_status = ""
                if task.response_payload:
                    try:
                        previous_payload = json.loads(task.response_payload)
                        previous_status = self._extract_status(previous_payload)
                    except Exception:
                        previous_status = ""
                if next_progress != task.progress or previous_status != status_text:
                    await self.db.update_geminigen_task(
                        job_id,
                        status="processing",
                        progress=next_progress,
                        response_payload=json.dumps(payload, ensure_ascii=False),
                    )
                    await self._update_request_log(
                        task.request_log_id,
                        status_text="geminigen_polling",
                        progress=next_progress,
                        response={
                            "status": "polling",
                            "job_id": job_id,
                            "upstream_uuid": task.upstream_uuid,
                            "upstream_status": status_text,
                            "upstream_progress": payload.get("status_percentage") if isinstance(payload, dict) else None,
                        },
                        duration=self._task_duration(task),
                    )
                else:
                    return task
            return await self.db.get_geminigen_task(job_id) or task
        except Exception as exc:
            await self.db.update_geminigen_task(job_id, status="failed", error_message=str(exc), completed_at=datetime.utcnow())
            await self._update_request_log(
                task.request_log_id,
                status_text="failed",
                progress=task.progress,
                status_code=502,
                response={"status": "failed", "job_id": job_id, "upstream_uuid": task.upstream_uuid, "error_message": str(exc)},
                duration=self._task_duration(task),
            )
            await self.db.release_geminigen_account(task.account_id, task.kind)
            return await self.db.get_geminigen_task(job_id) or task

    async def wait_for_task(self, job_id: str, *, api_key_id: Optional[int], base_url: Optional[str]) -> GeminiGenTask:
        task = await self.db.get_geminigen_task(job_id)
        cfg = await self.db.get_geminigen_config()
        timeout = cfg.timeout_video_sec if task and task.kind == "video" else cfg.timeout_image_sec
        interval = cfg.poll_interval_video_sec if task and task.kind == "video" else cfg.poll_interval_image_sec
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            task = await self.poll_task(job_id, api_key_id=api_key_id, base_url=base_url)
            if self.is_geminigen_terminal_status(task.status):
                return task
            await asyncio.sleep(float(interval))
        await self.db.update_geminigen_task(job_id, status="failed", error_message=f"GeminiGen task did not finish within {timeout}s", completed_at=datetime.utcnow())
        if task:
            await self._update_request_log(
                task.request_log_id,
                status_text="failed",
                progress=task.progress,
                status_code=504,
                response={"status": "failed", "job_id": job_id, "error_message": f"GeminiGen task did not finish within {timeout}s"},
                duration=self._task_duration(task),
            )
            await self.db.release_geminigen_account(task.account_id, task.kind)
        return await self.db.get_geminigen_task(job_id) or task

    async def complete_task_in_background(self, job_id: str, *, api_key_id: Optional[int], base_url: Optional[str]) -> None:
        if not await self._claim_background_completion(job_id):
            return
        try:
            await self.wait_for_task(job_id, api_key_id=api_key_id, base_url=base_url)
        except Exception as exc:
            debug_logger.log_warning(f"GeminiGen background poll failed for {job_id}: {exc}")
        finally:
            await self._release_background_completion(job_id)

    async def resume_active_tasks(self, *, base_url: Optional[str] = None, limit: int = 100) -> int:
        try:
            tasks = await self.db.list_active_geminigen_tasks(limit=limit)
        except Exception as exc:
            debug_logger.log_warning(f"GeminiGen active task resume scan failed: {exc}")
            return 0
        for task in tasks:
            asyncio.create_task(
                self.complete_task_in_background(
                    task.job_id,
                    api_key_id=task.api_key_id,
                    base_url=base_url,
                )
            )
        return len(tasks)

    async def _fetch_account_json_payload(
        self,
        account: GeminiGenAccount,
        base_url: str,
        path: str,
    ) -> Dict[str, Any]:
        proxy = await self._request_proxy()
        account = await self._ensure_fresh_account_token(account, base_url)
        for attempt in range(2):
            async with AsyncSession() as session:
                response = await session.get(
                    f"{self._api_base_url(base_url)}{path}",
                    headers=await self._headers(account, path, base_url=base_url, method="get"),
                    timeout=30,
                    proxy=proxy,
                    impersonate="chrome120",
                )
            if response.status_code < 400 or attempt or not self._token_expired_response(response):
                break
            account = await self._refresh_account_token(account, base_url)
        if response.status_code >= 400:
            raise RuntimeError(f"GET {path} failed HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"GeminiGen {path} did not return JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"GeminiGen {path} did not return an object")
        return payload

    async def _fetch_me_profile_payload(self, account: GeminiGenAccount, base_url: str) -> Dict[str, Any]:
        payload = await self._fetch_account_json_payload(account, base_url, "/api/me")
        if not isinstance(payload, dict) or not payload.get("email"):
            raise RuntimeError("GeminiGen /api/me did not return an authenticated user")
        return payload

    async def sync_account_profile(self, account_id: int) -> Dict[str, Any]:
        account = await self.db.get_geminigen_account(account_id)
        if not account:
            raise ValueError("GeminiGen account not found")
        cfg = await self.db.get_geminigen_config()
        synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            payload = await self._fetch_me_profile_payload(account, cfg.base_url)
            profile_updates = self.parse_me_profile(payload)
            fresh_account = await self.db.get_geminigen_account(account_id) or account
            quota_items = list(GEMINIGEN_QUOTA_PATHS.items())
            quota_results = await asyncio.gather(
                *[
                    self._fetch_account_json_payload(fresh_account, cfg.base_url, path)
                    for _, path in quota_items
                ],
                return_exceptions=True,
            )
            quota_updates: Dict[str, Any] = {}
            quota_errors: List[str] = []
            quota_successes = 0
            parsers = {
                "image_gen": self.parse_image_gen_quota,
                "grok_image": self.parse_grok_image_quota,
                "grok_max": self.parse_grok_max_quota,
            }
            for (bucket, _), result in zip(quota_items, quota_results):
                if isinstance(result, BaseException):
                    quota_errors.append(f"{bucket}: {result}")
                    continue
                try:
                    parsed_quota = parsers[bucket](result)
                except Exception as exc:
                    quota_errors.append(f"{bucket}: {exc}")
                    continue
                quota_updates.update(parsed_quota)
                quota_updates[f"{bucket}_quota_synced_at"] = synced_at
                quota_successes += 1
                if bucket in {"image_gen", "grok_image"}:
                    remaining = self._active_image_quota_remaining(parsed_quota, bucket)
                    if remaining is not None:
                        reset_field = (
                            "image_gen_daily_limit_reset_at"
                            if bucket == "image_gen"
                            else "grok_image_daily_limit_reset_at"
                        )
                        quota_updates[reset_field] = self._next_utc_midnight() if remaining <= 0 else None

            quota_status = (
                "healthy"
                if quota_successes == len(quota_items)
                else "partial"
                if quota_successes > 0
                else "failed"
            )
            quota_error = "; ".join(quota_errors)
            quota_updates.update(
                {
                    "quota_synced_at": synced_at,
                    "quota_sync_status": quota_status,
                    "quota_sync_error": quota_error,
                }
            )

            reset_values = {
                "image_gen": quota_updates.get(
                    "image_gen_daily_limit_reset_at",
                    account.image_gen_daily_limit_reset_at,
                ),
                "grok_image": quota_updates.get(
                    "grok_image_daily_limit_reset_at",
                    account.grok_image_daily_limit_reset_at,
                ),
                "video": account.video_daily_limit_reset_at,
            }
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            limited_buckets = [
                bucket for bucket, reset_at in reset_values.items() if reset_at and reset_at > now_utc
            ]
            status = (
                f"daily_limited_{limited_buckets[0]}"
                if len(limited_buckets) == 1
                else "daily_limited_multiple"
                if limited_buckets
                else "healthy"
            )
            error = GEMINIGEN_DAILY_LIMIT_ERROR_CODE if limited_buckets else ""
            account_updates = {**profile_updates, **quota_updates}
            await self.db.update_geminigen_account(
                account_id,
                **account_updates,
                last_status=status,
                last_error=error,
                profile_synced_at=synced_at,
                profile_sync_status="healthy",
                profile_sync_error="",
            )
        except Exception as exc:
            status = "failed"
            error = str(exc)
            await self.db.update_geminigen_account(
                account_id,
                last_status=status,
                last_error=error,
                profile_synced_at=synced_at,
                profile_sync_status=status,
                profile_sync_error=error,
            )
            quota_status = "not_synced"
            quota_error = ""
        return {
            "success": status in {"healthy", "daily_limited_image_gen", "daily_limited_grok_image", "daily_limited_video", "daily_limited_multiple"},
            "status": status,
            "error": error,
            "quota_status": quota_status,
            "quota_error": quota_error,
        }

    async def _refresh_account_profile_background(self, account_id: int) -> None:
        try:
            await self.sync_account_profile(account_id)
        except Exception as exc:
            debug_logger.log_warning(f"GeminiGen profile refresh failed for account {account_id}: {exc}")
        finally:
            self._profile_refreshing_account_ids.discard(int(account_id))

    def schedule_stale_account_profile_refresh(self, accounts: List[GeminiGenAccount]) -> int:
        scheduled = 0
        for account in accounts:
            if not account.id or not account.is_active or not self.is_account_profile_stale(account):
                continue
            account_id = int(account.id)
            if account_id in self._profile_refreshing_account_ids:
                continue
            self._profile_refreshing_account_ids.add(account_id)
            asyncio.create_task(self._refresh_account_profile_background(account_id))
            scheduled += 1
        return scheduled

    async def test_account(self, account_id: int) -> Dict[str, Any]:
        return await self.sync_account_profile(account_id)

    @staticmethod
    def _result_urls_for_task(task: GeminiGenTask) -> List[str]:
        return task.cached_artifact_urls or task.raw_artifact_urls or []

    @staticmethod
    def task_to_public_dict(task: GeminiGenTask) -> Dict[str, Any]:
        details = GeminiGenService._public_status_details(task)
        result_urls = GeminiGenService._result_urls_for_task(task)
        return {
            "job_id": task.job_id,
            "upstream_uuid": task.upstream_uuid,
            "status": task.status,
            "job_phase": details["job_phase"],
            "upstream_status": details["upstream_status"],
            "progress": task.progress,
            "model": task.public_model_id,
            "raw_artifact_urls": task.raw_artifact_urls or [],
            "cached_artifact_urls": task.cached_artifact_urls or [],
            "result_urls": result_urls,
            "error_message": task.error_message,
            "error_code": task.error_code,
            "retry_at": GeminiGenService._utc_iso(task.retry_at),
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        }

    @staticmethod
    def task_to_openai_payload(task: GeminiGenTask) -> Dict[str, Any]:
        if task.status in {"failed", "cancelled"}:
            cancelled = str(task.status or "").lower() == "cancelled"
            daily_limited = task.error_code == GEMINIGEN_DAILY_LIMIT_ERROR_CODE
            if cancelled:
                error_payload = {
                    "message": task.error_message or f"GeminiGen task {task.status}",
                    "type": "cancelled",
                    "code": "geminigen_generation_cancelled",
                    "status_code": 499,
                }
            elif daily_limited:
                error_payload = {
                    "message": task.error_message or f"GeminiGen task {task.status}",
                    "type": "rate_limit_error",
                    "code": GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                    "status_code": 429,
                    "retry_at": GeminiGenService._utc_iso(task.retry_at),
                }
            else:
                error_payload = {
                    "message": task.error_message or f"GeminiGen task {task.status}",
                    "type": "server_error",
                    "code": "geminigen_generation_failed",
                    "status_code": 502,
                }
            return {
                "error": error_payload,
                "job_id": task.job_id,
                "raw_artifact_urls": task.raw_artifact_urls or [],
                "cached_artifact_urls": task.cached_artifact_urls or [],
                "result_urls": GeminiGenService._result_urls_for_task(task),
            }
        urls = GeminiGenService._result_urls_for_task(task)
        parts: List[str] = []
        for url in urls:
            suffix = Path(urlparse(url).path).suffix.lower()
            if task.kind == "video" or suffix in VIDEO_SUFFIXES:
                parts.append(f"<video src='{url}' controls></video>")
            else:
                parts.append(f"![Generated Image]({url})")
        return {
            "id": f"chatcmpl-geminigen-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": task.public_model_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "\n".join(parts)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "job_id": task.job_id,
            "raw_artifact_urls": task.raw_artifact_urls or [],
            "cached_artifact_urls": task.cached_artifact_urls or [],
            "result_urls": urls,
        }
