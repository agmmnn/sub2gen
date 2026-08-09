"""Persistent headed Chrome profiles for Flow account login and refresh."""

from __future__ import annotations

import asyncio
import os
import signal
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.logger import debug_logger
from ..core.models import Token
from ..core.config import get_runtime_data_dir
from .browser_cookie_utils import extract_session_token_from_cookie_payload
from .browser_metrics_cleanup import cleanup_browser_metrics, profile_process_ids


BROWSER_PROFILE_ROOT = get_runtime_data_dir() / "browser_profiles"
LOGIN_URL = "https://accounts.google.com/"
FLOW_URL = "https://labs.google/fx/tools/flow"
CHROMIUM_SINGLETON_ARTIFACTS = ("SingletonLock", "SingletonCookie", "SingletonSocket")
SUPPORTS_PROC_PROFILE_INSPECTION = os.name != "nt" and Path("/proc").exists()


@dataclass
class ProfileRuntime:
    context: Any
    page: Any
    lock: asyncio.Lock
    pinned: bool = False
    profile_path: Optional[Path] = None


class BrowserProfileResourceExhaustedError(RuntimeError):
    """The container cannot allocate another browser process or worker thread."""


class BrowserProfileService:
    """Manage one persistent headed browser profile per Flow token/account."""

    _instance: Optional["BrowserProfileService"] = None
    _instance_lock = asyncio.Lock()

    def __init__(self, db=None, flow_client=None):
        self.db = db
        self.flow_client = flow_client
        self._playwright = None
        self._runtimes: Dict[int, ProfileRuntime] = {}
        self._runtime_lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls, db=None, flow_client=None) -> "BrowserProfileService":
        async with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(db=db, flow_client=flow_client)
            else:
                if db is not None:
                    cls._instance.db = db
                if flow_client is not None:
                    cls._instance.flow_client = flow_client
            return cls._instance

    @classmethod
    def get_existing_instance(cls) -> Optional["BrowserProfileService"]:
        return cls._instance

    @staticmethod
    def build_placeholder_st() -> str:
        return f"__browser_profile_pending_{uuid.uuid4().hex}"

    @staticmethod
    def build_placeholder_email(token_id: Optional[int] = None) -> str:
        suffix = str(token_id) if token_id else uuid.uuid4().hex[:8]
        return f"browser-profile-{suffix}@pending.local"

    def profile_path_for_token(self, token_id: int) -> Path:
        return (BROWSER_PROFILE_ROOT / f"token-{int(token_id)}").resolve()

    async def _ensure_playwright(self):
        if self._playwright is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
        return self._playwright

    def _launch_options(self) -> Dict[str, Any]:
        executable_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
        headless = os.environ.get("PERSONAL_BROWSER_HEADLESS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        options: Dict[str, Any] = {
            "headless": headless,
            "viewport": {"width": 1365, "height": 900},
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if executable_path and Path(executable_path).exists():
            options["executable_path"] = executable_path
        return options

    @staticmethod
    def is_resource_exhaustion_error(exc: BaseException) -> bool:
        message = str(exc or "").strip().lower()
        return any(
            marker in message
            for marker in (
                "can't start new thread",
                "cannot start new thread",
                "cannot fork",
                "resource temporarily unavailable",
                "spawn eagain",
                "spawn /",
            )
        ) and (
            "thread" in message
            or "fork" in message
            or "eagain" in message
            or "resource temporarily unavailable" in message
        )

    @staticmethod
    def _runtime_is_alive(runtime: ProfileRuntime) -> bool:
        try:
            return not runtime.page.is_closed()
        except Exception:
            return False

    @classmethod
    def _is_target_closed_error(cls, exc: Exception, runtime: ProfileRuntime) -> bool:
        if not cls._runtime_is_alive(runtime):
            return True
        error_name = type(exc).__name__.lower()
        error_message = str(exc).lower()
        return (
            "targetclosed" in error_name
            or "target page, context or browser has been closed" in error_message
            or "browser has been closed" in error_message
        )

    @staticmethod
    def _is_profile_lock_error(exc: BaseException) -> bool:
        message = str(exc or "").lower()
        return any(
            marker in message
            for marker in (
                "profile appears to be in use by another chromium process",
                "has locked the profile so that it doesn't get corrupted",
                "process_singleton_posix.cc",
                "failed to create a processsingleton",
            )
        )

    @staticmethod
    def _profile_process_ids(profile_path: Optional[Path]) -> list[int]:
        return profile_process_ids(profile_path) if profile_path is not None else []

    @classmethod
    def _remove_stale_singleton_artifacts(cls, profile_path: Path) -> int:
        """Remove Chromium locks only when no process is using this exact profile."""
        if not SUPPORTS_PROC_PROFILE_INSPECTION or cls._profile_process_ids(profile_path):
            return 0
        removed = 0
        for name in CHROMIUM_SINGLETON_ARTIFACTS:
            artifact = profile_path / name
            try:
                if artifact.is_symlink() or artifact.is_file():
                    artifact.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                debug_logger.log_warning(
                    f"[BrowserProfile] unable to remove stale {name}: {type(exc).__name__}"
                )
        return removed

    @classmethod
    async def _terminate_profile_processes(cls, profile_path: Optional[Path]) -> int:
        process_ids = cls._profile_process_ids(profile_path)
        for pid in process_ids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        if process_ids:
            await asyncio.sleep(0.2)
        survivors = cls._profile_process_ids(profile_path)
        for pid in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        return len(process_ids)

    @classmethod
    async def _dispose_runtime(cls, runtime: ProfileRuntime) -> None:
        try:
            await asyncio.wait_for(runtime.context.close(), timeout=10)
        except Exception:
            pass
        await cls._terminate_profile_processes(runtime.profile_path)
        if runtime.profile_path is not None:
            cleanup_browser_metrics(profiles=[runtime.profile_path])

    async def is_runtime_open(self, token_id: int) -> bool:
        stale_runtime: Optional[ProfileRuntime] = None
        async with self._runtime_lock:
            runtime = self._runtimes.get(int(token_id))
            if runtime is not None and not self._runtime_is_alive(runtime):
                stale_runtime = self._runtimes.pop(int(token_id), None)
                runtime = None
        if stale_runtime is not None:
            await self._dispose_runtime(stale_runtime)
        return runtime is not None

    async def _get_runtime(
        self,
        token_id: int,
        *,
        open_url: Optional[str] = None,
        pin: bool = False,
    ) -> ProfileRuntime:
        token_id = int(token_id)
        async with self._runtime_lock:
            for attempt in range(2):
                runtime = self._runtimes.get(token_id)
                if runtime is not None and not self._runtime_is_alive(runtime):
                    self._runtimes.pop(token_id, None)
                    await self._dispose_runtime(runtime)
                    runtime = None

                if runtime is None:
                    playwright = await self._ensure_playwright()
                    profile_path = self.profile_path_for_token(token_id)
                    profile_path.mkdir(parents=True, exist_ok=True)
                    metrics_stats = cleanup_browser_metrics(profiles=[profile_path])
                    if metrics_stats.reclaimed_bytes:
                        debug_logger.log_info(
                            f"[BrowserProfile] reclaimed {metrics_stats.reclaimed_bytes} BrowserMetrics bytes "
                            f"for token {token_id}"
                        )
                    removed = self._remove_stale_singleton_artifacts(profile_path)
                    if removed:
                        debug_logger.log_info(
                            f"[BrowserProfile] removed {removed} stale Chromium profile locks "
                            f"for token {token_id}"
                        )
                    for launch_attempt in range(2):
                        try:
                            context = await playwright.chromium.launch_persistent_context(
                                str(profile_path),
                                **self._launch_options(),
                            )
                            break
                        except Exception as exc:
                            if self.is_resource_exhaustion_error(exc):
                                raise BrowserProfileResourceExhaustedError(
                                    "Browser profile runtime capacity is exhausted"
                                ) from exc
                            if (
                                launch_attempt == 0
                                and self._is_profile_lock_error(exc)
                                and self._remove_stale_singleton_artifacts(profile_path)
                            ):
                                debug_logger.log_warning(
                                    f"[BrowserProfile] reclaimed a stale Chromium profile lock "
                                    f"for token {token_id}; retrying once"
                                )
                                continue
                            if self._is_profile_lock_error(exc):
                                raise RuntimeError(
                                    "Browser profile is already in use by another live Chromium process"
                                ) from exc
                            raise
                    page = context.pages[0] if context.pages else await context.new_page()
                    runtime = ProfileRuntime(
                        context=context,
                        page=page,
                        lock=asyncio.Lock(),
                        pinned=bool(pin),
                        profile_path=profile_path,
                    )
                    self._runtimes[token_id] = runtime
                elif pin:
                    runtime.pinned = True

                if open_url:
                    try:
                        await runtime.page.goto(open_url, wait_until="domcontentloaded", timeout=45000)
                    except Exception as exc:
                        if attempt == 0 and self._is_target_closed_error(exc, runtime):
                            if self._runtimes.get(token_id) is runtime:
                                self._runtimes.pop(token_id, None)
                            await self._dispose_runtime(runtime)
                            continue
                        raise
                return runtime

        raise RuntimeError(f"Unable to open browser profile runtime for token {token_id}")

    async def close_runtime(self, token_id: int) -> bool:
        async with self._runtime_lock:
            runtime = self._runtimes.pop(int(token_id), None)
        if runtime is not None:
            await self._dispose_runtime(runtime)
            return True
        return False

    async def close_runtime_if_unpinned(self, token_id: int) -> bool:
        """Close a scheduler-owned runtime without disturbing an admin-opened profile."""
        async with self._runtime_lock:
            runtime = self._runtimes.get(int(token_id))
            if runtime is None or runtime.pinned:
                return False
            self._runtimes.pop(int(token_id), None)
        await self._dispose_runtime(runtime)
        return True

    async def close_unpinned_runtimes(self) -> int:
        """Release every transient browser runtime after container resource pressure."""
        async with self._runtime_lock:
            transient = [runtime for runtime in self._runtimes.values() if not runtime.pinned]
            self._runtimes = {
                token_id: runtime
                for token_id, runtime in self._runtimes.items()
                if runtime.pinned
            }
        for runtime in transient:
            await self._dispose_runtime(runtime)
        return len(transient)

    async def close_all(self) -> int:
        """Close all profile contexts and the shared Playwright driver."""
        async with self._runtime_lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
            playwright = self._playwright
            self._playwright = None
        for runtime in runtimes:
            await self._dispose_runtime(runtime)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        return len(runtimes)

    def _token_status_payload(self, token: Token) -> Dict[str, Any]:
        return {
            "token_id": token.id,
            "auth_mode": token.auth_mode,
            "profile_path": token.browser_profile_path,
            "profile_status": token.browser_profile_status,
            "login_state": token.browser_profile_login_state,
            "cookie_status": token.browser_profile_cookie_status,
            "st_status": token.browser_profile_st_status,
            "at_status": token.browser_profile_at_status,
            "email": token.browser_profile_email or token.email,
            "name": token.browser_profile_name or token.name,
            "last_opened_at": token.browser_profile_last_opened_at.isoformat() if token.browser_profile_last_opened_at else None,
            "last_sync_at": token.browser_profile_last_sync_at.isoformat() if token.browser_profile_last_sync_at else None,
            "last_refresh_at": token.browser_profile_last_refresh_at.isoformat() if token.browser_profile_last_refresh_at else None,
            "last_error": token.browser_profile_last_error,
            "runtime_open": int(token.id or 0) in self._runtimes,
        }

    async def status(self, token_id: int) -> Dict[str, Any]:
        token = await self.db.get_token(int(token_id))
        if not token:
            raise ValueError("Token not found")
        payload = self._token_status_payload(token)
        payload["runtime_open"] = await self.is_runtime_open(token_id)
        return payload

    async def open_profile(self, token_id: int) -> Dict[str, Any]:
        token = await self.db.get_token(int(token_id))
        if not token:
            raise ValueError("Token not found")
        profile_path = str(self.profile_path_for_token(token_id))
        now = datetime.now(timezone.utc)
        try:
            await self._get_runtime(token_id, open_url=LOGIN_URL, pin=True)
            authenticated = (
                token.browser_profile_login_state == "logged_in"
                and token.browser_profile_cookie_status == "ok"
                and token.browser_profile_st_status == "ok"
            )
            await self.db.update_token(
                token_id,
                auth_mode="browser_profile",
                browser_profile_path=profile_path,
                browser_profile_status="connected" if authenticated else "opened",
                browser_profile_login_state="logged_in" if authenticated else "login_needed",
                browser_profile_last_opened_at=now,
                browser_profile_last_error=None,
            )
        except Exception as exc:
            await self.db.update_token(
                token_id,
                auth_mode="browser_profile",
                browser_profile_path=profile_path,
                browser_profile_status="error",
                browser_profile_last_error=str(exc)[:500],
            )
            raise
        updated = await self.db.get_token(token_id)
        return self._token_status_payload(updated)

    async def close_profile(self, token_id: int) -> Dict[str, Any]:
        token = await self.db.get_token(int(token_id))
        if not token:
            raise ValueError("Token not found")
        await self.close_runtime(token_id)
        updated = await self.db.get_token(int(token_id))
        payload = self._token_status_payload(updated)
        payload["runtime_open"] = False
        return payload

    async def _collect_cookies(self, runtime: ProfileRuntime) -> list[dict[str, Any]]:
        cookies = await runtime.context.cookies(
            [
                "https://labs.google/",
                "https://accounts.google.com/",
                "https://www.google.com/",
            ]
        )
        return [dict(cookie) for cookie in cookies]

    async def sync_profile(
        self,
        token_id: int,
        *,
        retain_runtime: bool = True,
    ) -> Dict[str, Any]:
        token = await self.db.get_token(int(token_id))
        if not token:
            raise ValueError("Token not found")
        profile_path = str(self.profile_path_for_token(token_id))
        now = datetime.now(timezone.utc)
        try:
            runtime = await self._get_runtime(token_id, open_url=FLOW_URL)
            async with runtime.lock:
                cookies = await self._collect_cookies(runtime)
                session_token = extract_session_token_from_cookie_payload(cookies)
                if not session_token:
                    await self.db.update_token(
                        token_id,
                        auth_mode="browser_profile",
                        browser_profile_path=profile_path,
                        browser_profile_status="login_needed",
                        browser_profile_login_state="login_needed",
                        browser_profile_cookie_status="missing_session_cookie",
                        browser_profile_st_status="missing",
                        browser_profile_last_sync_at=now,
                        browser_profile_last_error="Session cookie not found in browser profile",
                    )
                    updated = await self.db.get_token(token_id)
                    return self._token_status_payload(updated)

                if self.flow_client is None:
                    raise RuntimeError("Flow client is unavailable")
                result = await self.flow_client.st_to_at(session_token)
                access_token = result.get("access_token")
                expires = result.get("expires")
                at_expires = None
                if expires:
                    try:
                        at_expires = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                    except Exception:
                        at_expires = None
                user_info = result.get("user", {}) if isinstance(result, dict) else {}
                email = str(user_info.get("email") or token.email or "").strip()
                name = str(user_info.get("name") or token.name or "").strip()
                if not email or email.endswith("@pending.local"):
                    email = self.build_placeholder_email(token_id)
                await self.db.update_token(
                    token_id,
                    st=session_token,
                    at=access_token,
                    at_expires=at_expires,
                    email=email,
                    name=name or email.split("@")[0],
                    auth_mode="browser_profile",
                    browser_profile_path=profile_path,
                    browser_profile_status="connected",
                    browser_profile_email=email,
                    browser_profile_name=name or email.split("@")[0],
                    browser_profile_login_state="logged_in",
                    browser_profile_cookie_status="ok",
                    browser_profile_st_status="ok",
                    browser_profile_at_status="ok" if access_token else "missing",
                    browser_profile_last_sync_at=now,
                    browser_profile_last_refresh_at=now,
                    browser_profile_last_error=None,
                )
        except Exception as exc:
            safe_error = (
                "browser runtime capacity exhausted"
                if self.is_resource_exhaustion_error(exc)
                else str(exc)[:500]
            )
            debug_logger.log_warning(f"[BrowserProfile] sync failed for token {token_id}: {safe_error}")
            try:
                await self.db.update_token(
                    token_id,
                    auth_mode="browser_profile",
                    browser_profile_path=profile_path,
                    browser_profile_status="error",
                    browser_profile_last_sync_at=now,
                    browser_profile_last_error=safe_error,
                )
            except Exception as update_exc:
                debug_logger.log_warning(
                    f"[BrowserProfile] failed to persist sync error for token {token_id}: "
                    f"{type(update_exc).__name__}"
                )
            raise
        finally:
            if not retain_runtime:
                await self.close_runtime_if_unpinned(token_id)
        updated = await self.db.get_token(token_id)
        return self._token_status_payload(updated)

    async def refresh_profile(
        self,
        token_id: int,
        *,
        retain_runtime: bool = True,
    ) -> Dict[str, Any]:
        return await self.sync_profile(token_id, retain_runtime=retain_runtime)

    async def reset_profile(self, token_id: int) -> Dict[str, Any]:
        token = await self.db.get_token(int(token_id))
        if not token:
            raise ValueError("Token not found")
        await self.close_runtime(token_id)
        profile_path = self.profile_path_for_token(token_id)
        if profile_path.exists():
            shutil.rmtree(profile_path)
        await self.db.update_token(
            token_id,
            st=self.build_placeholder_st(),
            at=None,
            at_expires=None,
            is_active=False,
            auth_mode="browser_profile",
            browser_profile_path=str(profile_path),
            browser_profile_status="not_created",
            browser_profile_login_state="unknown",
            browser_profile_cookie_status="unknown",
            browser_profile_st_status="unknown",
            browser_profile_at_status="unknown",
            browser_profile_last_error=None,
        )
        updated = await self.db.get_token(token_id)
        return self._token_status_payload(updated)
