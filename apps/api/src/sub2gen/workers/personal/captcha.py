"""CAPTCHA result orchestration for personal browser workers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class PersonalCaptchaJobs:
    def __init__(self, worker: Any, *, log_warning: Callable[[str], None] | None = None) -> None:
        self.worker = worker
        self.log_warning = log_warning or (lambda _message: None)

    async def execute(
        self,
        project_id: str,
        *,
        action: str = "IMAGE_GENERATION",
        token_id: int | None = None,
        return_slot_id: bool = False,
    ) -> str | None | tuple[str | None, str | None]:
        return await self.worker._get_token_direct(
            project_id,
            action=action,
            token_id=token_id,
            return_slot_id=return_slot_id,
        )

    async def execute_with_metadata(
        self,
        project_id: str,
        *,
        action: str = "IMAGE_GENERATION",
        token_id: int | None = None,
    ) -> tuple[str | None, str | None, int | None]:
        token, slot_id = await self.worker._get_token_direct(
            project_id,
            action=action,
            token_id=token_id,
            return_slot_id=True,
        )
        if not token:
            return None, None, None
        return token, slot_id, token_id

    async def execute_bundle(
        self,
        project_id: str,
        *,
        action: str = "IMAGE_GENERATION",
        token_id: int | None = None,
    ) -> dict[str, Any] | None:
        worker = self.worker
        token, slot_id = await worker._get_token_direct(
            project_id,
            action=action,
            token_id=token_id,
            return_slot_id=True,
        )
        if not token:
            return None
        info = None
        if slot_id:
            async with worker._resident_lock:
                info = worker._resident_tabs.get(slot_id)
        if info and token_id is not None and info.token_id != int(token_id):
            self.log_warning(
                "[BrowserCaptcha] refusing mismatched slot identity in solve bundle "
                f"(slot={slot_id}, expected_token_id={token_id}, resident_token_id={info.token_id})"
            )
            info = None
        if info and not info.session_cookies:
            try:
                await worker._cache_session_cookies_for_computed(info)
            except Exception as error:
                self.log_warning(
                    "[BrowserCaptcha] solve-bundle cookie extraction failed "
                    f"(slot={slot_id}, project={project_id}, token_id={token_id}): {error}"
                )
        fingerprint = (
            dict(info.fingerprint)
            if info and isinstance(info.fingerprint, dict) and info.fingerprint
            else (worker.get_last_fingerprint() if not slot_id else None)
        )
        session_cookies = (
            dict(info.session_cookies)
            if info and isinstance(info.session_cookies, dict) and info.session_cookies
            else None
        )
        return worker._build_solve_bundle(
            token=token,
            project_id=project_id,
            action=action,
            token_id=token_id,
            slot_id=slot_id,
            fingerprint=fingerprint,
            session_cookies=session_cookies,
        )
