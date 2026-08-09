"""API key manager with account assignment and endpoint rate limits."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

from ..persistence.repositories import ApiKeyRepository
from ..services.redis_runtime import RedisRuntime, RedisUnavailableError


MANAGED_API_KEY_PREFIX = "s2g_live_"


def adobe_flags_from_scopes(scopes: Set[str]) -> Tuple[bool, bool, bool]:
    """Adobe route access is driven only by scope tokens (not legacy api_keys columns at auth time)."""
    return (
        "adobe:cloning" in scopes,
        "adobe:metadata" in scopes,
        "adobe:tracker" in scopes,
    )


@dataclass
class AuthContext:
    """Resolved auth context for one request."""

    key_id: Optional[int]
    key_label: str
    is_legacy: bool
    allowed_accounts: Set[int]
    scopes: Set[str]
    adobe_cloning_enabled: bool = True
    adobe_metadata_enabled: bool = True
    adobe_tracker_enabled: bool = True


class ApiKeyManager:
    """Validates API keys, account bindings, and simple fixed-window rate limits."""

    def __init__(
        self,
        db,
        legacy_api_key_provider,
        redis_runtime: Optional[RedisRuntime] = None,
        *,
        repository: ApiKeyRepository | None = None,
    ):
        self.db = db
        self.repository = repository or ApiKeyRepository(db)
        self.legacy_api_key_provider = legacy_api_key_provider
        self.redis_runtime = redis_runtime
        self._rate_limit_lock = asyncio.Lock()
        self._window_counters: Dict[Tuple[int, str, int], int] = {}

    @staticmethod
    def _digest(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @staticmethod
    def generate_key(prefix: str = MANAGED_API_KEY_PREFIX.removesuffix("_")) -> Tuple[str, str]:
        secret = secrets.token_urlsafe(36)
        full_key = f"{prefix}_{secret}"
        return full_key, ApiKeyManager._digest(full_key)

    async def create_api_key(
        self,
        client_name: str,
        label: str,
        scopes: str,
        account_ids: list[int],
        endpoint_limits: dict[str, dict[str, int]],
        expires_at: Optional[str] = None,
    ) -> dict:
        scopes_norm = (scopes or "").strip()
        if not scopes_norm:
            raise ValueError("scopes must be non-empty")
        full_key, key_hash = self.generate_key()
        key_prefix = full_key[:18]
        key_id = await self.repository.create_client_api_key(
            client_name=client_name.strip(),
            label=label.strip() or "default",
            key_prefix=key_prefix,
            key_plaintext=full_key,
            key_hash=key_hash,
            scopes=scopes_norm,
            account_ids=account_ids,
            endpoint_limits=endpoint_limits,
            expires_at=expires_at,
        )
        return {"id": key_id, "api_key": full_key, "key_prefix": key_prefix}

    async def authenticate(
        self,
        provided_api_key: Optional[str],
        *,
        endpoint: str,
        require_assignment: bool = False,
        enforce_rate_limits: bool = True,
        touch_usage: bool = True,
        require_redis: bool = False,
    ) -> AuthContext:
        if not provided_api_key:
            raise PermissionError("Missing API key")

        runtime = self.redis_runtime
        if require_redis and runtime is not None:
            runtime.ensure_ready()

        # Try managed keys first. In required mode Redis is authoritative for a
        # short cache window; shadow mode still reads SQLite and mirrors values.
        key_hash = self._digest(provided_api_key)
        cached = None
        is_managed_key = provided_api_key.startswith(MANAGED_API_KEY_PREFIX)
        if is_managed_key and runtime is not None and runtime.ready and runtime.required:
            cached = await runtime.get_auth_cache(key_hash)
        row = cached.get("row") if isinstance(cached, dict) else None
        cached_accounts = cached.get("account_ids") if isinstance(cached, dict) else None
        if is_managed_key and not row:
            row = await self.repository.get_client_api_key_by_hash(key_hash)
        if row:
            if not bool(row.get("is_active", True)):
                raise PermissionError("API key is disabled")

            if row.get("expires_at"):
                # SQLite compares timestamps lexicographically in ISO-ish format.
                now_unix = int(time.time())
                expires_ts = int(row.get("expires_unix") or 0)
                if expires_ts and now_unix >= expires_ts:
                    raise PermissionError("API key expired")

            key_id = int(row["id"])
            if isinstance(cached_accounts, list):
                allowed_accounts = {int(value) for value in cached_accounts}
            else:
                allowed_accounts = set(await self.repository.get_api_key_account_ids(key_id))
            scopes = {x.strip() for x in (row.get("scopes") or "").split(",") if x.strip()}
            if not scopes:
                scopes = {"*"}

            if require_assignment and not allowed_accounts:
                raise PermissionError("No accounts assigned to this API key")

            if runtime is not None and runtime.ready and cached is None:
                await runtime.set_auth_cache(
                    key_hash,
                    {"row": row, "account_ids": sorted(allowed_accounts)},
                )

            if enforce_rate_limits:
                await self._enforce_rate_limits(
                    key_id=key_id,
                    endpoint=endpoint,
                    require_redis=require_redis,
                )
            if touch_usage:
                if runtime is not None and runtime.ready:
                    try:
                        await runtime.queue_usage_touch(key_id)
                    except RedisUnavailableError:
                        if require_redis:
                            raise
                        await self.repository.touch_api_key_usage(key_id)
                    else:
                        if not runtime.required:
                            await self.repository.touch_api_key_usage(key_id)
                else:
                    await self.repository.touch_api_key_usage(key_id)

            acl, ame, atr = adobe_flags_from_scopes(scopes)

            return AuthContext(
                key_id=key_id,
                key_label=str(row.get("label") or row.get("key_prefix") or "managed"),
                is_legacy=False,
                allowed_accounts=allowed_accounts,
                scopes=scopes,
                adobe_cloning_enabled=acl,
                adobe_metadata_enabled=ame,
                adobe_tracker_enabled=atr,
            )

        # Legacy fallback
        legacy = (self.legacy_api_key_provider() or "").strip()
        if legacy and hmac.compare_digest(provided_api_key, legacy):
            if require_redis and runtime is not None:
                runtime.ensure_ready()
            return AuthContext(
                key_id=None,
                key_label="legacy-global",
                is_legacy=True,
                allowed_accounts=set(),
                scopes={"*"},
                adobe_cloning_enabled=True,
                adobe_metadata_enabled=True,
                adobe_tracker_enabled=True,
            )

        raise PermissionError("Invalid API key")

    async def _enforce_rate_limits(self, key_id: int, endpoint: str, *, require_redis: bool = False):
        runtime = self.redis_runtime
        limits = None
        if runtime is not None and runtime.ready and runtime.required:
            limits = await runtime.get_rate_config(key_id, endpoint)
        if limits is None:
            limits = await self.repository.get_api_key_rate_limits(key_id, endpoint)
            if runtime is not None and runtime.ready:
                await runtime.set_rate_config(key_id, endpoint, limits)
        if not limits:
            return

        now = int(time.time())
        minute_window = now // 60
        hour_window = now // 3600

        rpm = int(limits.get("rpm") or 0)
        rph = int(limits.get("rph") or 0)

        if runtime is not None and runtime.ready and runtime.required:
            try:
                await runtime.enforce_rate_limits(
                    key_id=key_id,
                    endpoint=endpoint,
                    rpm=rpm,
                    rph=rph,
                    now=now,
                )
                return
            except RedisUnavailableError:
                if require_redis:
                    raise
        if require_redis and runtime is not None:
            raise RedisUnavailableError("redis_unavailable")

        async with self._rate_limit_lock:
            if rpm > 0:
                minute_key = (key_id, endpoint, minute_window)
                minute_count = self._window_counters.get(minute_key, 0) + 1
                if minute_count > rpm:
                    raise RuntimeError(f"Rate limit exceeded: {rpm} requests/min for {endpoint}")
                self._window_counters[minute_key] = minute_count

            if rph > 0:
                hour_key = (key_id, endpoint, hour_window)
                hour_count = self._window_counters.get(hour_key, 0) + 1
                if hour_count > rph:
                    raise RuntimeError(f"Rate limit exceeded: {rph} requests/hour for {endpoint}")
                self._window_counters[hour_key] = hour_count

        if runtime is not None and runtime.ready and runtime.mode == "shadow":
            try:
                await runtime.enforce_rate_limits(
                    key_id=key_id,
                    endpoint=endpoint,
                    rpm=rpm,
                    rph=rph,
                    now=now,
                )
            except (RuntimeError, RedisUnavailableError):
                # Shadow decisions are observed through Redis metrics/events but
                # never change the current in-process authoritative result.
                pass

    async def invalidate(self, key_id: int) -> None:
        if self.redis_runtime is not None:
            await self.redis_runtime.invalidate_api_key(key_id)
