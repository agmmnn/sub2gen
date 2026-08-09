"""Token manager for sub2gen with AT auto-refresh"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, List
from ..core.database import Database
from ..core.config import config
from ..core.models import Token, Project
from ..core.logger import debug_logger
from ..core.monitoring import record_token_refresh
from ..persistence.repositories import AccountRepository, ProjectRepository
from .flow_client import FlowClient
from .proxy_manager import ProxyManager


class TokenManager:
    """Token lifecycle manager with AT auto-refresh"""

    def __init__(
        self,
        db: Database,
        flow_client: FlowClient,
        *,
        accounts: AccountRepository | None = None,
        projects: ProjectRepository | None = None,
    ):
        self.db = db
        self.accounts = accounts or AccountRepository(db)
        self.projects = projects or ProjectRepository(db)
        self.flow_client = flow_client
        self._refresh_lock_guard = asyncio.Lock()
        self._project_lock_guard = asyncio.Lock()
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        self._project_locks: dict[int, asyncio.Lock] = {}
        self._refresh_futures: dict[int, asyncio.Task] = {}
        self._last_st_refresh_reason: dict[int, str] = {}
        self._at_validation_cache: dict[int, float] = {}
        self._at_validation_retry_after: dict[int, float] = {}
        self._protocol_refresher_task: Optional[asyncio.Task] = None

    def _set_st_refresh_reason(self, token_id: int, reason: str) -> None:
        self._last_st_refresh_reason[token_id] = str(reason or "").strip() or "unknown"

    def consume_st_refresh_reason(self, token_id: int) -> str:
        """Return and clear the latest ST refresh reason for a token."""
        return self._last_st_refresh_reason.pop(token_id, "")

    def _clear_at_validation_cache(self, token_id: int) -> None:
        token_key = int(token_id)
        self._at_validation_cache.pop(token_key, None)
        self._at_validation_retry_after.pop(token_key, None)

    def _mark_at_valid(self, token_id: int, ttl_seconds: int = 300) -> None:
        token_key = int(token_id)
        self._at_validation_cache[token_key] = time.monotonic() + max(30, int(ttl_seconds or 300))
        self._at_validation_retry_after.pop(token_key, None)

    def _defer_at_validation(self, token_id: int, delay_seconds: int = 30) -> None:
        token_key = int(token_id)
        self._at_validation_retry_after[token_key] = time.monotonic() + max(1, int(delay_seconds or 30))

    def _has_recent_at_validation(self, token_id: int) -> bool:
        token_key = int(token_id)
        expires_at = float(self._at_validation_cache.get(token_key, 0.0) or 0.0)
        if expires_at <= time.monotonic():
            self._at_validation_cache.pop(token_key, None)
            return False
        return True

    def _is_at_validation_deferred(self, token_id: int) -> bool:
        token_key = int(token_id)
        retry_at = float(self._at_validation_retry_after.get(token_key, 0.0) or 0.0)
        if retry_at <= time.monotonic():
            self._at_validation_retry_after.pop(token_key, None)
            return False
        return True

    @staticmethod
    def _is_at_authentication_error(exc: BaseException) -> bool:
        message = str(exc or "").strip().lower()
        return any(
            marker in message
            for marker in (
                "401",
                "unauthenticated",
                "invalid authentication credentials",
                "invalid access token",
                "access token expired",
            )
        )

    def _is_timeout_st_refresh_reason(self, reason: str) -> bool:
        normalized = str(reason or "").strip().lower()
        return normalized in {
            "extension_timeout",
            "local_timeout",
            "local_timeout_after_extension",
        }

    @staticmethod
    def _is_resource_exhaustion_error(exc: BaseException) -> bool:
        message = str(exc or "").strip().lower()
        return any(
            marker in message
            for marker in (
                "can't start new thread",
                "cannot start new thread",
                "cannot fork",
                "resource temporarily unavailable",
                "eagain",
            )
        )

    async def _get_token_lock(
        self,
        lock_map: dict[int, asyncio.Lock],
        guard: asyncio.Lock,
        token_id: int,
    ) -> asyncio.Lock:
        """按 token 维度获取锁，避免不同 token 之间串行阻塞。"""
        async with guard:
            lock = lock_map.get(token_id)
            if lock is None:
                lock = asyncio.Lock()
                lock_map[token_id] = lock
            return lock

    def _get_project_pool_size(self) -> int:
        """读取当前生效的单 Token 项目池大小配置。"""
        try:
            return max(1, min(50, int(config.personal_project_pool_size or 4)))
        except Exception:
            return 4

    def _sort_projects(self, projects: List[Project]) -> List[Project]:
        """Sort projects in a stable order for round-robin selection."""
        return sorted(projects, key=lambda project: (project.id or 0, project.project_id))

    def _normalize_project_name_base(self, project_name: Optional[str] = None) -> str:
        """Normalize a project base name for pooled creation."""
        raw_name = (project_name or "").strip()
        if raw_name:
            parts = raw_name.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].startswith("P") and parts[1][1:].isdigit():
                return parts[0]
            return raw_name
        return datetime.now().strftime("%b %d - %H:%M")

    def _build_project_name(self, pool_index: int, base_name: Optional[str] = None) -> str:
        """Build a project name for the pool."""
        normalized_base = self._normalize_project_name_base(base_name)
        return f"{normalized_base} P{pool_index}"

    @staticmethod
    def _normalize_protocol_mode(value: Optional[str]) -> str:
        return "protocol" if str(value or "").strip().lower() == "protocol" else "session"

    @staticmethod
    def _normalize_refresh_interval(value: Optional[int]) -> int:
        try:
            return max(1, min(10080, int(value if value is not None else 120)))
        except Exception:
            return 120

    @staticmethod
    def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @staticmethod
    def _parse_at_expires(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def _normalize_token_proxy_url(self, proxy_url: Optional[str]) -> str:
        raw = str(proxy_url or "").strip()
        if not raw:
            return ""
        proxy_manager = getattr(self.flow_client, "proxy_manager", None)
        normalize = getattr(proxy_manager, "normalize_proxy_url", None)
        if callable(normalize):
            return str(normalize(raw) or "")
        return raw

    async def _flow_call_for_token(
        self,
        token: Optional[Token],
        call: Callable[[], Awaitable[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        previous_fingerprint = self.flow_client.get_request_fingerprint()
        proxy_url = str(getattr(token, "proxy_url", "") or "").strip()
        if proxy_url:
            fingerprint = dict(previous_fingerprint or {})
            fingerprint["proxy_url"] = proxy_url
            self.flow_client._set_request_fingerprint(fingerprint)
        try:
            return await call()
        finally:
            self.flow_client._set_request_fingerprint(previous_fingerprint)

    async def _st_to_at_for_token(self, token: Optional[Token], st: str) -> Dict[str, Any]:
        return await self._flow_call_for_token(token, lambda: self.flow_client.st_to_at(st))

    async def _get_credits_for_token(self, token: Optional[Token], at: str) -> Dict[str, Any]:
        return await self._flow_call_for_token(token, lambda: self.flow_client.get_credits(at))

    async def get_personal_warmup_project_ids(
        self,
        tokens: Optional[List[Token]] = None,
        limit: Optional[int] = None,
    ) -> List[str]:
        """返回 personal 模式启动时建议预热的项目 ID 列表。"""
        token_list = tokens if tokens is not None else await self.get_all_tokens()
        pool_size = self._get_project_pool_size()
        warmup_ids: List[str] = []
        seen_projects: set[str] = set()

        try:
            warmup_limit = None if limit is None else max(1, int(limit))
        except Exception:
            warmup_limit = None

        for token in token_list:
            if not token or not token.is_active:
                continue

            candidate_ids: List[str] = []
            current_project_id = str(token.current_project_id or "").strip()
            if current_project_id:
                candidate_ids.append(current_project_id)

            projects = [project for project in await self.projects.get_projects_by_token(token.id) if project.is_active]
            for project in self._sort_projects(projects):
                project_id = str(project.project_id or "").strip()
                if project_id and project_id not in candidate_ids:
                    candidate_ids.append(project_id)

            for project_id in candidate_ids[:pool_size]:
                if project_id in seen_projects:
                    continue
                seen_projects.add(project_id)
                warmup_ids.append(project_id)
                if warmup_limit is not None and len(warmup_ids) >= warmup_limit:
                    return warmup_ids

        return warmup_ids

    async def _create_project_for_token(
        self,
        token: Token,
        pool_index: int,
        base_name: Optional[str] = None,
        api_key_id: Optional[int] = None,
    ) -> Project:
        """Create a new pooled project for a token and persist it."""
        project_name = self._build_project_name(pool_index, base_name)
        project_id = await self.flow_client.create_project(token.st, project_name)
        debug_logger.log_info(f"[PROJECT] Created pooled project for token {token.id}: {project_name} ({project_id})")
        project = Project(
            project_id=project_id,
            token_id=token.id,
            api_key_id=api_key_id,
            project_name=project_name,
        )
        project.id = await self.projects.add_project(project)
        return project

    def _select_next_project(self, token: Token, projects: List[Project]) -> Project:
        """Select the next project from the pool in round-robin order."""
        ordered_projects = self._sort_projects(projects)
        if not ordered_projects:
            raise ValueError("No available projects for token")

        if len(ordered_projects) == 1:
            return ordered_projects[0]

        if token.current_project_id:
            for index, project in enumerate(ordered_projects):
                if project.project_id == token.current_project_id:
                    return ordered_projects[(index + 1) % len(ordered_projects)]

        return ordered_projects[0]

    # ========== Token CRUD ==========

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        return await self.accounts.get_all_tokens()

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        return await self.accounts.get_active_tokens()

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        return await self.accounts.get_token(token_id)

    async def delete_token(self, token_id: int):
        """Delete token"""
        token = await self.accounts.get_token(token_id)
        project_ids: List[str] = []
        if token:
            current_project_id = str(token.current_project_id or "").strip()
            if current_project_id:
                project_ids.append(current_project_id)

        for project in await self.projects.get_projects_by_token(token_id):
            project_id = str(project.project_id or "").strip()
            if project_id and project_id not in project_ids:
                project_ids.append(project_id)

        await self.accounts.delete_token(token_id)
        self._clear_at_validation_cache(token_id)

        refresh_task = self._refresh_futures.pop(token_id, None)
        if refresh_task and not refresh_task.done():
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._refresh_locks.pop(token_id, None)
        self._project_locks.pop(token_id, None)

        if config.captcha_method == "personal" and project_ids:
            try:
                from .browser_captcha_personal import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                for project_id in project_ids:
                    await service.stop_resident_mode(project_id)
            except Exception as e:
                debug_logger.log_warning(f"[DELETE_TOKEN] 清理 personal 浏览器状态失败: {e}")

    async def enable_token(self, token_id: int):
        """Enable a token and reset error count"""
        self._clear_at_validation_cache(token_id)
        # Enable the token
        await self.accounts.update_token(token_id, is_active=True, ban_reason=None, banned_at=None)
        # Reset error count when enabling (only reset total error_count, keep today_error_count)
        await self.accounts.reset_error_count(token_id)

    async def disable_token(self, token_id: int):
        """Disable a token"""
        self._clear_at_validation_cache(token_id)
        await self.accounts.update_token(token_id, is_active=False)

    # ========== Token添加 (支持Project创建) ==========

    async def add_token(
        self,
        st: str,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        remark: Optional[str] = None,
        image_enabled: bool = True,
        video_enabled: bool = True,
        image_concurrency: int = -1,
        video_concurrency: int = -1,
        captcha_proxy_url: Optional[str] = None,
        extension_route_key: Optional[str] = None,
        protocol_mode: str = "session",
        google_cookies: Optional[str] = None,
        login_account: Optional[str] = None,
        login_password: Optional[str] = None,
        proxy_url: Optional[str] = None,
        auto_refresh_enabled: bool = True,
        refresh_interval_minutes: int = 120,
    ) -> Token:
        """Add a new token and prepare its pooled projects."""
        existing_token = await self.accounts.get_token_by_st(st)
        if existing_token:
            raise ValueError(f"Token ??????: {existing_token.email}?")

        debug_logger.log_info(f"[ADD_TOKEN] Converting ST to AT...")
        normalized_proxy_url = self._normalize_token_proxy_url(proxy_url)
        pending_token = Token(st=st, email="", proxy_url=normalized_proxy_url)
        try:
            result = await self._st_to_at_for_token(pending_token, st)
            at = result["access_token"]
            expires = result.get("expires")
            user_info = result.get("user", {})
            email = user_info.get("email", "")
            name = user_info.get("name", email.split("@")[0] if email else "")
            at_expires = None
            if expires:
                try:
                    at_expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                except Exception:
                    pass
        except Exception as e:
            raise ValueError(f"ST?AT??: {str(e)}")

        try:
            credits_result = await self._get_credits_for_token(pending_token, at)
            credits = credits_result.get("credits", 0)
            user_paygate_tier = credits_result.get("userPaygateTier")
        except Exception:
            credits = 0
            user_paygate_tier = None

        token = Token(
            st=st,
            at=at,
            at_expires=at_expires,
            email=email,
            name=name,
            remark=remark,
            is_active=True,
            credits=credits,
            user_paygate_tier=user_paygate_tier,
            current_project_id=None,
            current_project_name=None,
            image_enabled=image_enabled,
            video_enabled=video_enabled,
            image_concurrency=image_concurrency,
            video_concurrency=video_concurrency,
            captcha_proxy_url=captcha_proxy_url,
            extension_route_key=extension_route_key,
            protocol_mode=self._normalize_protocol_mode(protocol_mode),
            google_cookies=str(google_cookies or "").strip(),
            login_account=str(login_account or "").strip(),
            login_password=str(login_password or ""),
            proxy_url=normalized_proxy_url,
            auto_refresh_enabled=bool(auto_refresh_enabled),
            refresh_interval_minutes=self._normalize_refresh_interval(refresh_interval_minutes),
        )

        token_id = await self.accounts.add_token(token)
        token.id = token_id

        debug_logger.log_info(
            f"[ADD_TOKEN] Token added successfully (ID: {token_id}, Email: {email}, auth_mode=session_token)"
        )
        return token

    async def update_token(
        self,
        token_id: int,
        st: Optional[str] = None,
        at: Optional[str] = None,
        at_expires: Optional[datetime] = None,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        remark: Optional[str] = None,
        image_enabled: Optional[bool] = None,
        video_enabled: Optional[bool] = None,
        image_concurrency: Optional[int] = None,
        video_concurrency: Optional[int] = None,
        captcha_proxy_url: Optional[str] = None,
        extension_route_key: Optional[str] = None,
        use_extension_for_generation: Optional[bool] = None,
        protocol_mode: Optional[str] = None,
        google_cookies: Optional[str] = None,
        login_account: Optional[str] = None,
        login_password: Optional[str] = None,
        proxy_url: Optional[str] = None,
        auto_refresh_enabled: Optional[bool] = None,
        refresh_interval_minutes: Optional[int] = None,
    ):
        """Update token (支持修改project_id和project_name)

        当用户编辑保存token时，如果token未过期，自动清空429禁用状态
        """
        update_fields = {}
        credential_updated = any(value is not None for value in (st, at, at_expires))

        if st is not None:
            update_fields["st"] = st
        if at is not None:
            update_fields["at"] = at
        if at_expires is not None:
            update_fields["at_expires"] = at_expires
        if project_id is not None:
            update_fields["current_project_id"] = project_id
        if project_name is not None:
            update_fields["current_project_name"] = project_name
        if remark is not None:
            update_fields["remark"] = remark
        if image_enabled is not None:
            update_fields["image_enabled"] = image_enabled
        if video_enabled is not None:
            update_fields["video_enabled"] = video_enabled
        if image_concurrency is not None:
            update_fields["image_concurrency"] = image_concurrency
        if video_concurrency is not None:
            update_fields["video_concurrency"] = video_concurrency
        if captcha_proxy_url is not None:
            update_fields["captcha_proxy_url"] = captcha_proxy_url
        if extension_route_key is not None:
            update_fields["extension_route_key"] = extension_route_key
        if use_extension_for_generation is not None:
            update_fields["use_extension_for_generation"] = 1 if use_extension_for_generation else 0
        if protocol_mode is not None:
            update_fields["protocol_mode"] = self._normalize_protocol_mode(protocol_mode)
        if google_cookies is not None:
            update_fields["google_cookies"] = str(google_cookies).strip()
        if login_account is not None:
            update_fields["login_account"] = str(login_account).strip()
        if login_password is not None:
            update_fields["login_password"] = str(login_password)
        if proxy_url is not None:
            update_fields["proxy_url"] = self._normalize_token_proxy_url(proxy_url)
        if auto_refresh_enabled is not None:
            update_fields["auto_refresh_enabled"] = bool(auto_refresh_enabled)
        if refresh_interval_minutes is not None:
            update_fields["refresh_interval_minutes"] = self._normalize_refresh_interval(refresh_interval_minutes)

        # 检查token是否因429被禁用，如果是且未过期，则清空429状态
        token = await self.accounts.get_token(token_id)
        if credential_updated and token:
            if not token.is_active:
                debug_logger.log_info(f"[UPDATE_TOKEN] Token {token_id} credentials updated; re-enabling token")
            update_fields["is_active"] = True
            update_fields["ban_reason"] = None
            update_fields["banned_at"] = None
        if token and token.ban_reason == "429_rate_limit":
            # 检查token是否过期
            is_expired = False
            if token.at_expires:
                now = datetime.now(timezone.utc)
                if token.at_expires.tzinfo is None:
                    at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
                else:
                    at_expires_aware = token.at_expires
                is_expired = at_expires_aware <= now

            # 如果未过期，清空429禁用状态
            if not is_expired:
                debug_logger.log_info(f"[UPDATE_TOKEN] Token {token_id} 编辑保存，清空429禁用状态")
                update_fields["ban_reason"] = None
                update_fields["banned_at"] = None

        if update_fields:
            if credential_updated:
                self._clear_at_validation_cache(token_id)
            await self.accounts.update_token(token_id, **update_fields)

    # ========== AT自动刷新逻辑 (核心) ==========

    def _should_refresh_at(self, token: Token) -> bool:
        """根据当前 token 快照判断是否需要刷新 AT。"""
        if not token.at:
            debug_logger.log_info(f"[AT_CHECK] Token {token.id}: AT不存在,需要刷新")
            return True

        if not token.at_expires:
            debug_logger.log_info(f"[AT_CHECK] Token {token.id}: AT过期时间未知,尝试刷新")
            return True

        now = datetime.now(timezone.utc)
        if token.at_expires.tzinfo is None:
            at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
        else:
            at_expires_aware = token.at_expires

        time_until_expiry = at_expires_aware - now
        if time_until_expiry.total_seconds() < 3600:
            debug_logger.log_info(
                f"[AT_CHECK] Token {token.id}: AT即将过期 (剩余 {time_until_expiry.total_seconds():.0f} 秒),需要刷新"
            )
            return True

        return False

    def needs_at_refresh(self, token: Optional[Token]) -> bool:
        """供调度层快速判断当前 token 是否大概率会触发 AT 刷新。"""
        if not token:
            return True
        return self._should_refresh_at(token)

    async def ensure_valid_token(self, token: Optional[Token]) -> Optional[Token]:
        """确保 token 的 AT 可用，并在必要时返回刷新后的最新对象。"""
        if not token:
            return None

        if not self._should_refresh_at(token):
            if self._has_recent_at_validation(token.id) or self._is_at_validation_deferred(token.id):
                return token
            try:
                credits_result = await self._get_credits_for_token(token, token.at)
                await self.accounts.update_token(
                    token.id,
                    credits=credits_result.get("credits", 0),
                    user_paygate_tier=credits_result.get("userPaygateTier"),
                )
                self._mark_at_valid(token.id)
                return await self.accounts.get_token(token.id) or token
            except Exception as exc:
                if not self._is_at_authentication_error(exc):
                    self._defer_at_validation(token.id, delay_seconds=30)
                    debug_logger.log_warning(
                        f"[AT_CHECK] Token {token.id}: transient AT validation failure; "
                        f"keeping token active and retrying later - {type(exc).__name__}"
                    )
                    return token
                debug_logger.log_warning(
                    f"[AT_CHECK] Token {token.id}: upstream rejected locally unexpired AT; refreshing AT/ST"
                )

        if not await self._refresh_at(token.id):
            return None

        return await self.accounts.get_token(token.id)

    async def is_at_valid(self, token_id: int, token: Optional[Token] = None) -> bool:
        """检查AT是否有效,如果无效或即将过期则自动刷新

        Returns:
            True if AT is valid or refreshed successfully
            False if AT cannot be refreshed
        """
        token_obj = token if token and token.id == token_id else await self.accounts.get_token(token_id)
        if not token_obj:
            return False

        valid_token = await self.ensure_valid_token(token_obj)
        return valid_token is not None

    async def _refresh_at_inner(self, token_id: int) -> bool:
        """Perform exactly one real AT refresh attempt."""
        from ..core.config import config

        refresh_lock = await self._get_token_lock(
            self._refresh_locks,
            self._refresh_lock_guard,
            token_id,
        )
        async with refresh_lock:
            self._set_st_refresh_reason(token_id, "not_attempted")
            token = await self.accounts.get_token(token_id)
            if not token:
                self._set_st_refresh_reason(token_id, "token_not_found")
                return False

            effective_st = token.st
            if config.session_refresh_browser_first and config.session_refresh_enabled:
                debug_logger.log_info(
                    f"[AT_REFRESH] Token {token_id}: browser-first policy active, refreshing ST before AT refresh..."
                )
                new_st = await self._try_refresh_st(token_id, token)
                if not new_st and config.session_refresh_fail_if_st_refresh_fails:
                    st_reason = self._last_st_refresh_reason.get(token_id, "not_attempted")
                    debug_logger.log_error(
                        f"[AT_REFRESH] Token {token_id}: browser ST refresh failed, aborting AT refresh per strict policy "
                        f"(reason={st_reason})"
                    )
                    if self._is_timeout_st_refresh_reason(st_reason):
                        await self.disable_token(token_id)
                        debug_logger.log_warning(
                            f"[AT_REFRESH] Token {token_id}: token disabled due to timeout-class ST refresh failure"
                        )
                    else:
                        debug_logger.log_warning(
                            f"[AT_REFRESH] Token {token_id}: token kept active (non-timeout ST refresh failure)"
                        )
                    if self._last_st_refresh_reason.get(token_id, "not_attempted") == "not_attempted":
                        self._set_st_refresh_reason(token_id, "failed_without_reason")
                    return False
                if new_st:
                    effective_st = new_st
            elif config.session_refresh_enabled:
                self._set_st_refresh_reason(token_id, "policy_skipped")
            else:
                self._set_st_refresh_reason(token_id, "disabled")

            debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: starting AT refresh...")
            current_after_st = await self.accounts.get_token(token_id)
            previous_expires = current_after_st.at_expires if current_after_st else token.at_expires
            result = await self._do_refresh_at(token_id, effective_st, previous_at_expires=previous_expires)
            if result:
                return True

            st_reason = self._last_st_refresh_reason.get(token_id, "not_attempted")
            if self._is_timeout_st_refresh_reason(st_reason):
                debug_logger.log_error(
                    f"[AT_REFRESH] Token {token_id}: all refresh attempts failed; disabling token (reason={st_reason})"
                )
                await self.disable_token(token_id)
            else:
                debug_logger.log_warning(
                    f"[AT_REFRESH] Token {token_id}: all refresh attempts failed; token kept active (reason={st_reason})"
                )
            return False

    async def _refresh_at(self, token_id: int) -> bool:
        """Coalesce concurrent AT refresh calls for the same token."""
        existing_task = self._refresh_futures.get(token_id)
        if existing_task:
            return await existing_task

        async def runner() -> bool:
            try:
                result = await self._refresh_at_inner(token_id)
                if not result:
                    self._clear_at_validation_cache(token_id)
                return result
            finally:
                current = self._refresh_futures.get(token_id)
                if current is task:
                    self._refresh_futures.pop(token_id, None)

        task = asyncio.create_task(runner())
        self._refresh_futures[token_id] = task
        return await task

    async def refresh_st_only(self, token_id: int) -> bool:
        """Refresh only the session-token cookie (no AT mint).

        Pulls the latest __Secure-next-auth.session-token from the bound extension
        worker (or local headed browser as fallback) and persists it on the token.
        Does NOT call st_to_at and does NOT touch token.at / token.at_expires.
        Reuses the per-token refresh_lock so this cannot race with _refresh_at_inner.
        Returns True iff a NEW (different) session token was persisted.
        """
        refresh_lock = await self._get_token_lock(
            self._refresh_locks,
            self._refresh_lock_guard,
            token_id,
        )
        async with refresh_lock:
            self._set_st_refresh_reason(token_id, "not_attempted")
            token = await self.accounts.get_token(token_id)
            if not token:
                self._set_st_refresh_reason(token_id, "token_not_found")
                return False
            new_st = await self._try_refresh_st(token_id, token)
            return bool(new_st)

    async def _do_refresh_at(
        self,
        token_id: int,
        st: str,
        previous_at_expires: Optional[datetime] = None,
    ) -> bool:
        """执行 AT 刷新的核心逻辑

        Args:
            token_id: Token ID
            st: Session Token

        Returns:
            True if refresh successful AND AT is valid, False otherwise
        """
        try:
            debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: 开始刷新AT...")

            # 使用ST转AT
            result = await self.flow_client.st_to_at(st)
            new_at = result["access_token"]
            expires = result.get("expires")

            # 解析过期时间
            new_at_expires = None
            if expires:
                try:
                    new_at_expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                except:
                    pass

            expiry_advanced = False
            if previous_at_expires and new_at_expires:
                prev_aware = (
                    previous_at_expires.replace(tzinfo=timezone.utc)
                    if previous_at_expires.tzinfo is None
                    else previous_at_expires
                )
                new_aware = (
                    new_at_expires.replace(tzinfo=timezone.utc) if new_at_expires.tzinfo is None else new_at_expires
                )
                expiry_advanced = new_aware > prev_aware

            # 更新数据库
            await self.accounts.update_token(token_id, at=new_at, at_expires=new_at_expires)

            debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: AT刷新成功")
            debug_logger.log_info(f"  - 新过期时间: {new_at_expires}")
            if previous_at_expires and new_at_expires and not expiry_advanced:
                debug_logger.log_warning(
                    f"[AT_REFRESH] Token {token_id}: AT refreshed but expires unchanged "
                    f"(prev={previous_at_expires}, new={new_at_expires})"
                )

            # 验证 AT 有效性：通过 get_credits 测试
            try:
                current_token = await self.accounts.get_token(token_id)
                credits_result = await self._get_credits_for_token(current_token, new_at)
                await self.accounts.update_token(
                    token_id,
                    credits=credits_result.get("credits", 0),
                    user_paygate_tier=credits_result.get("userPaygateTier"),
                )
                self._mark_at_valid(token_id)
                debug_logger.log_info(
                    f"[AT_REFRESH] Token {token_id}: AT 验证成功（余额: {credits_result.get('credits', 0)}）"
                )
                record_token_refresh("at", "success")
                return True
            except Exception as verify_err:
                # AT 验证失败（可能返回 401），说明 ST 已过期
                error_msg = str(verify_err)
                if "401" in error_msg or "UNAUTHENTICATED" in error_msg:
                    debug_logger.log_warning(f"[AT_REFRESH] Token {token_id}: AT 验证失败 (401)，ST 可能已过期")
                    record_token_refresh("at", "failure")
                    return False
                else:
                    # 其他错误（如网络问题），仍视为成功
                    debug_logger.log_warning(f"[AT_REFRESH] Token {token_id}: AT 验证时发生非认证错误: {error_msg}")
                    self._defer_at_validation(token_id, delay_seconds=30)
                    record_token_refresh("at", "success")
                    return True

        except Exception as e:
            debug_logger.log_error(f"[AT_REFRESH] Token {token_id}: AT刷新失败 - {str(e)}")
            record_token_refresh("at", "failure")
            return False

    async def _try_protocol_refresh_st(self, token_id: int, token: Token) -> Optional[str]:
        """Refresh ST through the allowlisted Google protocol flow when enabled."""
        if self._normalize_protocol_mode(getattr(token, "protocol_mode", "session")) != "protocol":
            return None
        if not str(getattr(token, "google_cookies", "") or "").strip():
            self._set_st_refresh_reason(token_id, "protocol_cookies_missing")
            return None

        from .protocol_login import protocol_loginer

        result = await protocol_loginer.login(
            token.google_cookies,
            proxy=str(getattr(token, "proxy_url", "") or "").strip() or None,
            email=str(getattr(token, "login_account", "") or token.email or "").strip() or None,
        )
        refreshed_at = datetime.now(timezone.utc)
        if result.get("success") and result.get("session_token"):
            new_st = str(result["session_token"]).strip()
            await self.accounts.update_token(
                token_id,
                st=new_st,
                last_st_refresh_at=refreshed_at,
                last_st_refresh_result="success",
            )
            self._set_st_refresh_reason(token_id, "success_protocol")
            record_token_refresh("st", "success")
            debug_logger.log_info(f"[ST_REFRESH] Token {token_id}: protocol ST refresh succeeded")
            return new_st

        safe_error = str(result.get("error") or "Protocol refresh failed")[:240]
        await self.accounts.update_token(
            token_id,
            last_st_refresh_at=refreshed_at,
            last_st_refresh_result=safe_error,
        )
        self._set_st_refresh_reason(token_id, "protocol_failed")
        record_token_refresh("st", "failure")
        debug_logger.log_warning(f"[ST_REFRESH] Token {token_id}: protocol ST refresh failed: {safe_error}")
        return None

    async def _try_refresh_st(self, token_id: int, token) -> Optional[str]:
        """尝试通过浏览器刷新 Session Token

        使用常驻 tab 获取新的 __Secure-next-auth.session-token

        Args:
            token_id: Token ID
            token: Token 对象

        Returns:
            新的 ST 字符串，如果失败返回 None
        """
        try:
            from ..core.config import config

            captcha_mode = str(config.captcha_method or "").strip()

            if self._normalize_protocol_mode(getattr(token, "protocol_mode", "session")) == "protocol":
                protocol_st = await self._try_protocol_refresh_st(token_id, token)
                if protocol_st:
                    return protocol_st

            if getattr(token, "auth_mode", "session_token") == "browser_profile":
                try:
                    from .browser_profile_service import (
                        BrowserProfileResourceExhaustedError,
                        BrowserProfileService,
                    )

                    profile_service = await BrowserProfileService.get_instance(
                        db=self.db,
                        flow_client=self.flow_client,
                    )
                    profile = await profile_service.refresh_profile(
                        token_id,
                        retain_runtime=False,
                    )
                    updated = await self.accounts.get_token(token_id)
                    if (
                        updated
                        and updated.st
                        and profile.get("profile_status") == "connected"
                        and profile.get("st_status") == "ok"
                    ):
                        self._set_st_refresh_reason(token_id, "success_browser_profile")
                        record_token_refresh("st", "success")
                        return updated.st
                    self._set_st_refresh_reason(token_id, "browser_profile_login_needed")
                    record_token_refresh("st", "failure")
                    return None
                except Exception as profile_err:
                    resource_exhausted = isinstance(
                        profile_err, BrowserProfileResourceExhaustedError
                    ) or BrowserProfileService.is_resource_exhaustion_error(profile_err)
                    debug_logger.log_warning(
                        f"[ST_REFRESH] Token {token_id}: browser profile ST refresh failed: "
                        f"{'runtime capacity exhausted' if resource_exhausted else profile_err}"
                    )
                    self._set_st_refresh_reason(
                        token_id,
                        "browser_profile_resource_exhausted" if resource_exhausted else "browser_profile_error",
                    )
                    record_token_refresh("st", "failure")
                    return None

            if not token.current_project_id:
                debug_logger.log_warning(f"[ST_REFRESH] Token {token_id} 没有 project_id，无法刷新 ST")
                self._set_st_refresh_reason(token_id, "project_id_missing")
                return None

            debug_logger.log_info(f"[ST_REFRESH] Token {token_id}: 尝试通过浏览器刷新 ST (mode={captcha_mode})...")

            refresh_timeout_seconds = 45.0

            async def _persist_if_new(candidate_st: Optional[str], source_label: str) -> Optional[str]:
                if not candidate_st:
                    self._set_st_refresh_reason(token_id, f"{source_label}_empty")
                    record_token_refresh("st", "failure")
                    return None
                if candidate_st == token.st:
                    debug_logger.log_warning(
                        f"[ST_REFRESH] Token {token_id}: 从 {source_label} 获取到的 ST 与原 ST 相同，"
                        "可能上游会话未续期或登录已失效"
                    )
                    self._set_st_refresh_reason(token_id, "same_st")
                    record_token_refresh("st", "failure")
                    return None
                await self.accounts.update_token(token_id, st=candidate_st)
                debug_logger.log_info(
                    f"[ST_REFRESH] Token {token_id}: ST 已自动更新 (source={source_label}, mode={captcha_mode})"
                )
                self._set_st_refresh_reason(token_id, f"success_{source_label}")
                record_token_refresh("st", "success")
                return candidate_st

            dedicated_extension_enabled = bool(getattr(config, "dedicated_extension_enabled", False))
            extension_mode_active = captcha_mode == "extension"
            dedicated_extension_effective = dedicated_extension_enabled or extension_mode_active
            extension_attempted = False
            ext_routing_failure_code: Optional[str] = None
            if dedicated_extension_effective:
                try:
                    from .browser_captcha_extension import ExtensionCaptchaService

                    extension_timeout = float(
                        getattr(config, "dedicated_extension_st_refresh_timeout_seconds", 45) or 45
                    )
                    extension_service = await ExtensionCaptchaService.get_instance(self.db)
                    extension_attempted = True
                    ext_res = await asyncio.wait_for(
                        extension_service.refresh_session_token(
                            token_id=token_id,
                            timeout=max(10, int(extension_timeout)),
                        ),
                        timeout=max(10.0, extension_timeout + 5.0),
                    )
                    if ext_res.session_token is None:
                        code = ext_res.failure_code or "extension_no_worker_or_empty"
                        self._set_st_refresh_reason(token_id, code)
                        if code in (
                            "extension_worker_offline",
                            "extension_session_refresh_disabled",
                        ):
                            ext_routing_failure_code = code
                    else:
                        persisted_extension = await _persist_if_new(ext_res.session_token, "dedicated_extension")
                        if persisted_extension:
                            return persisted_extension
                except asyncio.TimeoutError:
                    debug_logger.log_warning(f"[ST_REFRESH] Token {token_id}: dedicated extension ST refresh timeout")
                    self._set_st_refresh_reason(token_id, "extension_timeout")
                except Exception as ext_err:
                    debug_logger.log_warning(
                        f"[ST_REFRESH] Token {token_id}: dedicated extension ST refresh failed: {ext_err}"
                    )
                    self._set_st_refresh_reason(token_id, "extension_error")
            else:
                self._set_st_refresh_reason(token_id, "extension_disabled")

            # 1) Always try local headed-browser refresh first.
            from .browser_captcha import BrowserCaptchaService

            service = await BrowserCaptchaService.get_instance(self.db)
            refresh_kwargs = {"token_id": token_id}

            try:
                local_st = await asyncio.wait_for(
                    service.refresh_session_token(token.current_project_id, **refresh_kwargs),
                    timeout=refresh_timeout_seconds,
                )
            except asyncio.TimeoutError:
                debug_logger.log_error(
                    f"[ST_REFRESH] Token {token_id}: 本地浏览器刷新 ST 超时 ({refresh_timeout_seconds:.0f}s)"
                )
                if extension_attempted:
                    if ext_routing_failure_code not in (
                        "extension_worker_offline",
                        "extension_session_refresh_disabled",
                    ):
                        self._set_st_refresh_reason(token_id, "local_timeout_after_extension")
                else:
                    self._set_st_refresh_reason(token_id, "local_timeout")
                record_token_refresh("st", "failure")
                local_st = None
            except Exception as local_err:
                debug_logger.log_warning(f"[ST_REFRESH] Token {token_id}: 本地浏览器刷新 ST 失败: {local_err}")
                if extension_attempted:
                    if ext_routing_failure_code not in (
                        "extension_worker_offline",
                        "extension_session_refresh_disabled",
                    ):
                        self._set_st_refresh_reason(token_id, "local_error_after_extension")
                else:
                    self._set_st_refresh_reason(token_id, "local_error")
                record_token_refresh("st", "failure")
                local_st = None

            _preserve_ext_routing_reason = ext_routing_failure_code in (
                "extension_worker_offline",
                "extension_session_refresh_disabled",
            )
            if local_st is not None or not _preserve_ext_routing_reason:
                persisted_local = await _persist_if_new(local_st, "local_headed")
            else:
                persisted_local = None
            if persisted_local:
                return persisted_local

            debug_logger.log_warning(f"[ST_REFRESH] Token {token_id}: 本地有头浏览器刷新 ST 失败 (mode={captcha_mode})")
            if extension_attempted:
                if ext_routing_failure_code in (
                    "extension_worker_offline",
                    "extension_session_refresh_disabled",
                ):
                    pass
                else:
                    self._set_st_refresh_reason(token_id, "extension_and_local_failed")
            elif dedicated_extension_effective:
                self._set_st_refresh_reason(token_id, "extension_enabled_but_no_success")
            else:
                self._set_st_refresh_reason(token_id, "local_failed")
            record_token_refresh("st", "failure")
            return None

        except Exception as e:
            debug_logger.log_error(f"[ST_REFRESH] Token {token_id}: 刷新 ST 失败 - {str(e)}")
            self._set_st_refresh_reason(token_id, "st_refresh_exception")
            record_token_refresh("st", "failure")
            return None

    async def _refresh_protocol_token(self, token: Token) -> None:
        token_id = int(token.id)
        refresh_lock = await self._get_token_lock(
            self._refresh_locks,
            self._refresh_lock_guard,
            token_id,
        )
        async with refresh_lock:
            latest = await self.accounts.get_token(token_id)
            if (
                not latest
                or not latest.is_active
                or not latest.auto_refresh_enabled
                or self._normalize_protocol_mode(latest.protocol_mode) != "protocol"
                or not latest.google_cookies.strip()
            ):
                return

            new_st = await self._try_protocol_refresh_st(token_id, latest)
            if not new_st:
                return

            try:
                session = await self._st_to_at_for_token(latest, new_st)
                new_at = str(session.get("access_token") or "").strip()
                if not new_at:
                    raise RuntimeError("ST-to-AT response did not include an access token")

                updates: Dict[str, Any] = {
                    "st": new_st,
                    "at": new_at,
                    "at_expires": self._parse_at_expires(session.get("expires")),
                    "last_st_refresh_at": datetime.now(timezone.utc),
                    "last_st_refresh_result": "success",
                }
                user = session.get("user") if isinstance(session.get("user"), dict) else {}
                if user.get("email"):
                    updates["email"] = user["email"]
                if user.get("name"):
                    updates["name"] = user["name"]

                try:
                    credits = await self._get_credits_for_token(latest, new_at)
                    updates["credits"] = credits.get("credits", 0)
                    updates["user_paygate_tier"] = credits.get("userPaygateTier")
                except Exception as exc:
                    debug_logger.log_warning(
                        f"[PROTOCOL_REFRESH] Token {token_id}: credit refresh failed: {type(exc).__name__}"
                    )

                await self.accounts.update_token(token_id, **updates)
                record_token_refresh("at", "success")
            except Exception as exc:
                await self.accounts.update_token(
                    token_id,
                    st=new_st,
                    last_st_refresh_at=datetime.now(timezone.utc),
                    last_st_refresh_result=f"at_refresh_failed:{type(exc).__name__}",
                )
                record_token_refresh("at", "failure")
                debug_logger.log_error(
                    f"[PROTOCOL_REFRESH] Token {token_id}: ST-to-AT refresh failed: {type(exc).__name__}"
                )

    async def run_protocol_refresh_once(self) -> None:
        """Refresh every due, active protocol-mode token once."""
        refresh_config = await self.db.get_token_refresh_config()
        if not refresh_config.enabled:
            return

        now = datetime.now(timezone.utc)
        for token in await self.accounts.get_active_tokens():
            if (
                not token.auto_refresh_enabled
                or self._normalize_protocol_mode(token.protocol_mode) != "protocol"
                or not token.google_cookies.strip()
            ):
                continue
            interval = self._normalize_refresh_interval(
                token.refresh_interval_minutes or refresh_config.refresh_interval_minutes
            )
            last_refresh = self._as_utc(token.last_st_refresh_at)
            if last_refresh and now - last_refresh < timedelta(minutes=interval):
                continue
            try:
                await self._refresh_protocol_token(token)
            except Exception as exc:
                if self._is_resource_exhaustion_error(exc):
                    raise
                debug_logger.log_error(
                    f"[PROTOCOL_REFRESH] Token {token.id}: background refresh failed: {type(exc).__name__}"
                )

    async def _protocol_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                from .redis_runtime import redis_runtime

                if redis_runtime.maintenance_active:
                    continue
                await self.run_protocol_refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._is_resource_exhaustion_error(exc):
                    debug_logger.log_error(
                        "[PROTOCOL_REFRESH] Container runtime capacity exhausted; "
                        "pausing background refresh for five minutes"
                    )
                    await asyncio.sleep(300)
                    continue
                debug_logger.log_error(f"[PROTOCOL_REFRESH] Background loop failed: {type(exc).__name__}")

    def start_protocol_refresher(self) -> None:
        if self._protocol_refresher_task and not self._protocol_refresher_task.done():
            return
        self._protocol_refresher_task = asyncio.create_task(self._protocol_refresh_loop())

    async def stop_protocol_refresher(self) -> None:
        task = self._protocol_refresher_task
        self._protocol_refresher_task = None
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def ensure_project_exists(
        self,
        token_id: int,
        api_key_id: Optional[int] = None,
        preferred_project_id: Optional[str] = None,
    ) -> str:
        """Ensure a token has a pooled set of projects and return one in round-robin order."""
        project_lock = await self._get_token_lock(
            self._project_locks,
            self._project_lock_guard,
            token_id,
        )
        async with project_lock:
            token = await self.accounts.get_token(token_id)
            if not token:
                raise ValueError("Token not found")

            pref = (preferred_project_id or "").strip()
            if pref:
                projects_for_pref = await self.projects.get_projects_by_token(token_id, api_key_id=api_key_id)
                match = next(
                    (p for p in projects_for_pref if p.is_active and p.project_id == pref),
                    None,
                )
                if not match:
                    raise ValueError(f"Project not found or inactive for this token: {pref}")
                await self.accounts.update_token(
                    token_id,
                    current_project_id=match.project_id,
                    current_project_name=match.project_name,
                )
                return match.project_id

            projects = [
                project
                for project in await self.projects.get_projects_by_token(token_id, api_key_id=api_key_id)
                if project.is_active
            ]
            projects = self._sort_projects(projects)

            try:
                project_pool_size = self._get_project_pool_size()
                while len(projects) < project_pool_size:
                    new_project = await self._create_project_for_token(
                        token,
                        len(projects) + 1,
                        api_key_id=api_key_id,
                    )
                    projects.append(new_project)
                    projects = self._sort_projects(projects)

                selectable_projects = projects[:project_pool_size]
                selected_project = self._select_next_project(token, selectable_projects)
                await self.accounts.update_token(
                    token_id,
                    current_project_id=selected_project.project_id,
                    current_project_name=selected_project.project_name,
                )
                return selected_project.project_id
            except Exception as e:
                raise ValueError(f"Failed to prepare project pool: {str(e)}")

    async def create_project_for_token(
        self,
        token_id: int,
        title: Optional[str] = None,
        set_as_current: bool = True,
        api_key_id: Optional[int] = None,
    ) -> Project:
        """Create a new VideoFX project for an existing token and persist it in the projects table."""
        project_lock = await self._get_token_lock(
            self._project_locks,
            self._project_lock_guard,
            token_id,
        )
        async with project_lock:
            token = await self.accounts.get_token(token_id)
            if not token:
                raise ValueError("Token not found")
            st = (token.st or "").strip()
            if not st:
                raise ValueError("Token has no session token")

            all_projects = await self.projects.get_projects_by_token(token_id, api_key_id=api_key_id)
            projects = self._sort_projects([p for p in all_projects if p.is_active])
            next_index = len(all_projects) + 1

            raw = (title or "").strip()
            if raw:
                project_title = raw[:120]
            else:
                base = self._normalize_project_name_base(token.current_project_name or token.remark)
                project_title = self._build_project_name(next_index, base)

            project_id = await self.flow_client.create_project(st, project_title)
            project = Project(
                project_id=project_id,
                token_id=token_id,
                api_key_id=api_key_id,
                project_name=project_title,
                tool_name="PINHOLE",
            )
            # Enforce one active project per (api_key_id, token_id) scope.
            await self.projects.deactivate_projects_for_token_scope(token_id, api_key_id=api_key_id)
            project.id = await self.projects.add_project(project)
            if set_as_current:
                await self.accounts.update_token(
                    token_id,
                    current_project_id=project_id,
                    current_project_name=project_title,
                )
            return project

    async def record_usage(self, token_id: int, is_video: bool = False):
        """Record token usage"""
        await self.accounts.update_token(token_id, use_count=1, last_used_at=datetime.now())

        if is_video:
            await self.accounts.increment_token_stats(token_id, "video")
        else:
            await self.accounts.increment_token_stats(token_id, "image")

    async def record_error(self, token_id: int):
        """Record token error and auto-disable if threshold reached"""
        await self.accounts.increment_token_stats(token_id, "error")

        # Check if should auto-disable token (based on consecutive errors)
        stats = await self.accounts.get_token_stats(token_id)
        admin_config = await self.db.get_admin_config()

        if (
            admin_config.error_ban_enabled
            and stats
            and stats.consecutive_error_count >= admin_config.error_ban_threshold
        ):
            debug_logger.log_warning(
                f"[TOKEN_BAN] Token {token_id} consecutive error count ({stats.consecutive_error_count}) "
                f"reached threshold ({admin_config.error_ban_threshold}), auto-disabling"
            )
            await self.disable_token(token_id)

    async def record_success(self, token_id: int):
        """Record successful request (reset consecutive error count)

        This method resets error_count to 0, which is used for auto-disable threshold checking.
        Note: today_error_count and historical statistics are NOT reset.
        """
        await self.accounts.reset_error_count(token_id)

    async def ban_token_for_429(self, token_id: int):
        """因429错误立即禁用token

        Args:
            token_id: Token ID
        """
        debug_logger.log_warning(f"[429_BAN] 禁用Token {token_id} (原因: 429 Rate Limit)")
        await self.accounts.update_token(
            token_id, is_active=False, ban_reason="429_rate_limit", banned_at=datetime.now(timezone.utc)
        )

    async def auto_unban_429_tokens(self):
        """自动解禁因429被禁用的token

        规则:
        - 距离禁用时间12小时后自动解禁
        - 仅解禁未过期的token
        - 仅解禁因429被禁用的token
        """
        all_tokens = await self.accounts.get_all_tokens()
        now = datetime.now(timezone.utc)

        for token in all_tokens:
            # 跳过非429禁用的token
            if token.ban_reason != "429_rate_limit":
                continue

            # 跳过未禁用的token
            if token.is_active:
                continue

            # 跳过没有禁用时间的token
            if not token.banned_at:
                continue

            # 检查token是否已过期
            if token.at_expires:
                # 确保时区一致
                if token.at_expires.tzinfo is None:
                    at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
                else:
                    at_expires_aware = token.at_expires

                # 如果已过期，跳过
                if at_expires_aware <= now:
                    debug_logger.log_info(f"[AUTO_UNBAN] Token {token.id} 已过期，跳过解禁")
                    continue

            # 确保banned_at时区一致
            if token.banned_at.tzinfo is None:
                banned_at_aware = token.banned_at.replace(tzinfo=timezone.utc)
            else:
                banned_at_aware = token.banned_at

            # 检查是否已过12小时
            time_since_ban = now - banned_at_aware
            if time_since_ban.total_seconds() >= 12 * 3600:  # 12小时
                debug_logger.log_info(
                    f"[AUTO_UNBAN] 解禁Token {token.id} (禁用时间: {banned_at_aware}, "
                    f"已过 {time_since_ban.total_seconds() / 3600:.1f} 小时)"
                )
                await self.accounts.update_token(token.id, is_active=True, ban_reason=None, banned_at=None)
                # 重置错误计数
                await self.accounts.reset_error_count(token.id)

    # ========== 余额刷新 ==========

    async def refresh_credits(self, token_id: int) -> int:
        """刷新Token余额

        Returns:
            credits
        """
        token = await self.accounts.get_token(token_id)
        if not token:
            return 0

        # 确保AT有效
        token = await self.ensure_valid_token(token)
        if not token:
            return 0

        try:
            result = await self.flow_client.get_credits(token.at)
            credits = result.get("credits", 0)
            user_paygate_tier = result.get("userPaygateTier")

            # 更新数据库
            await self.accounts.update_token(
                token_id,
                credits=credits,
                user_paygate_tier=user_paygate_tier,
            )

            return credits
        except Exception as e:
            debug_logger.log_error(f"Failed to refresh credits for token {token_id}: {str(e)}")
            return 0
