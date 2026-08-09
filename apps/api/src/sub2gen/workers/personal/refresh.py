"""Session-token refresh orchestration for personal browser workers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .models import ResidentTabInfo


class PersonalSessionRefreshJobs:
    """Run the refresh workflow while browser operations remain on the worker."""

    def __init__(
        self,
        worker: Any,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        log_info: Callable[[str], None] | None = None,
        log_warning: Callable[[str], None] | None = None,
        log_error: Callable[[str], None] | None = None,
    ) -> None:
        self.worker = worker
        self.sleep = sleep
        self.log_info = log_info or (lambda _message: None)
        self.log_warning = log_warning or (lambda _message: None)
        self.log_error = log_error or (lambda _message: None)

    async def execute(self, project_id: str, token_id: int | None = None) -> str | None:
        worker = self.worker
        for attempt in range(2):
            worker._mark_runtime_active()
            await worker.initialize()
            started_at = time.time()
            self.log_info(
                f"[BrowserCaptcha] 开始刷新 Session Token (project: {project_id}, "
                f"token_id={token_id}, attempt={attempt + 1})..."
            )

            async with worker._resident_lock:
                slot_id, info = worker._resolve_resident_slot_for_project_locked(project_id, token_id=token_id)
            if info is None or not slot_id:
                slot_id, info = await worker._ensure_resident_tab(
                    project_id,
                    token_id=token_id,
                    return_slot_key=True,
                )
            if info is None or not slot_id:
                if attempt == 0 and not await worker._probe_browser_runtime():
                    await worker._recover_browser_runtime(project_id, reason="refresh_session_prepare")
                    continue
                self.log_warning(f"[BrowserCaptcha] 无法为 project_id={project_id} 获取共享常驻标签页")
                return None
            if not info.tab:
                self.log_error("[BrowserCaptcha] 无法获取常驻标签页")
                return None

            if not await worker._ensure_resident_token_binding(
                info,
                token_id,
                label=f"refresh_session:{slot_id}",
            ):
                self.log_warning(
                    "[BrowserCaptcha] 刷新 Session Token 前 cookie 绑定未就绪，尝试重建 "
                    f"(slot={slot_id}, project={project_id}, token_id={token_id})"
                )
                slot_id, info = await worker._rebuild_resident_tab(
                    project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    return_slot_key=True,
                )
                if info is None or not slot_id or not info.tab:
                    if attempt == 0 and not await worker._probe_browser_runtime():
                        await worker._recover_browser_runtime(
                            project_id,
                            reason="refresh_session_rebuild_cookie_binding",
                        )
                        continue
                    return None

            try:
                session_token = await self._refresh_tab(slot_id, info)
                if session_token:
                    self._mark_success(project_id, token_id, slot_id, info)
                    elapsed_ms = (time.time() - started_at) * 1000
                    self.log_info(f"[BrowserCaptcha] ✅ Session Token 获取成功（耗时 {elapsed_ms:.0f}ms）")
                    return session_token
                self.log_error("[BrowserCaptcha] ❌ 未找到 __Secure-next-auth.session-token cookie")
                return None
            except Exception as error:
                self.log_error(f"[BrowserCaptcha] 刷新 Session Token 异常: {error}")
                if attempt == 0 and worker._is_browser_runtime_error(error):
                    if await worker._recover_browser_runtime(project_id, reason=f"refresh_session:{slot_id}"):
                        continue
                slot_id, info = await worker._rebuild_resident_tab(
                    project_id,
                    token_id=token_id,
                    slot_id=slot_id,
                    return_slot_key=True,
                )
                if info is not None and slot_id:
                    try:
                        session_token = await self._read_cookie_api(
                            slot_id,
                            info,
                            label="refresh_session_get_cookies_after_rebuild",
                        )
                        if session_token:
                            self._mark_success(project_id, token_id, slot_id, info)
                            self.log_info("[BrowserCaptcha] ✅ 重建后 Session Token 获取成功")
                            return session_token
                    except Exception as rebuild_error:
                        if attempt == 0 and worker._is_browser_runtime_error(rebuild_error):
                            if await worker._recover_browser_runtime(
                                project_id,
                                reason=f"refresh_session_rebuild:{slot_id}",
                            ):
                                continue
                return None
        return None

    async def _refresh_tab(self, slot_id: str, info: ResidentTabInfo) -> str | None:
        worker = self.worker
        async with info.solve_lock:
            self.log_info("[BrowserCaptcha] 刷新常驻标签页以获取最新 cookies...")
            info.recaptcha_ready = False
            await worker._run_with_timeout(
                worker._tab_reload(info.tab, label=f"refresh_session_reload:{slot_id}"),
                timeout_seconds=worker._session_refresh_timeout_seconds,
                label=f"refresh_session_reload_total:{slot_id}",
            )
            for _ in range(30):
                await self.sleep(1)
                try:
                    ready_state = await worker._tab_evaluate(
                        info.tab,
                        "document.readyState",
                        label=f"refresh_session_ready_state:{slot_id}",
                        timeout_seconds=2.0,
                    )
                    if ready_state == "complete":
                        break
                except Exception:
                    pass
            info.recaptcha_ready = await worker._wait_for_recaptcha(info.tab)
            if not info.recaptcha_ready:
                self.log_warning(f"[BrowserCaptcha] 刷新 Session Token 后 reCAPTCHA 未恢复就绪 (slot={slot_id})")
            await self.sleep(2)
            try:
                return await self._read_cookie_api(slot_id, info, lock=False)
            except Exception as error:
                self.log_warning(f"[BrowserCaptcha] 通过 cookies API 获取失败: {error}，尝试从 document.cookie 获取...")
                try:
                    cookie_text = await worker._tab_evaluate(
                        info.tab,
                        "document.cookie",
                        label=f"refresh_session_document_cookie:{slot_id}",
                    )
                    return self._session_token_from_cookie_text(cookie_text)
                except Exception as document_error:
                    self.log_error(f"[BrowserCaptcha] document.cookie 获取失败: {document_error}")
                    return None

    async def _read_cookie_api(
        self,
        slot_id: str,
        info: ResidentTabInfo,
        *,
        label: str = "refresh_session_get_cookies",
        lock: bool = True,
    ) -> str | None:
        async def read() -> str | None:
            cookies = await self.worker._get_browser_cookies(
                label=f"{label}:{slot_id}",
                browser_context_id=info.browser_context_id,
            )
            for cookie in cookies:
                if cookie.name == "__Secure-next-auth.session-token":
                    return cookie.value
            return None

        if not lock:
            return await read()
        async with info.solve_lock:
            return await read()

    def _mark_success(
        self,
        project_id: str,
        token_id: int | None,
        slot_id: str,
        info: ResidentTabInfo,
    ) -> None:
        info.last_used_at = time.time()
        self.worker._remember_project_affinity(project_id, slot_id, info)
        self.worker._remember_token_affinity(token_id, slot_id, info)
        self.worker._resident_error_streaks.pop(slot_id, None)
        self.worker._mark_browser_health(True)

    @staticmethod
    def _session_token_from_cookie_text(cookie_text: Any) -> str | None:
        if not cookie_text:
            return None
        for part in str(cookie_text).split(";"):
            normalized = part.strip()
            if normalized.startswith("__Secure-next-auth.session-token="):
                return normalized.split("=", 1)[1]
        return None
