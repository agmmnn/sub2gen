from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sub2gen.workers.personal import PersonalCaptchaJobs, ResidentTabInfo


class CaptchaWorker:
    def __init__(self) -> None:
        self._resident_lock = asyncio.Lock()
        self.info = ResidentTabInfo(object(), "slot-1", project_id="project-1", token_id=7)
        self.info.fingerprint = {"user_agent": "Solver-UA/1"}
        self.info.session_cookies = {"SID": "cookie"}
        self._resident_tabs = {"slot-1": self.info}

    async def _get_token_direct(self, *_args: Any, return_slot_id: bool = False, **_kwargs: Any):
        return ("captcha-token", "slot-1") if return_slot_id else "captcha-token"

    def get_last_fingerprint(self):
        return None

    def _build_solve_bundle(self, **values: Any):
        return values


@pytest.mark.asyncio
async def test_captcha_jobs_return_token_metadata_and_slot_identity_bundle() -> None:
    worker = CaptchaWorker()
    jobs = PersonalCaptchaJobs(worker)

    assert await jobs.execute("project-1", token_id=7) == "captcha-token"
    assert await jobs.execute_with_metadata("project-1", token_id=7) == (
        "captcha-token",
        "slot-1",
        7,
    )
    bundle = await jobs.execute_bundle("project-1", token_id=7)
    assert bundle is not None
    assert bundle["token"] == "captcha-token"
    assert bundle["fingerprint"] == {"user_agent": "Solver-UA/1"}
    assert bundle["session_cookies"] == {"SID": "cookie"}


@pytest.mark.asyncio
async def test_captcha_jobs_refuse_mismatched_slot_identity() -> None:
    worker = CaptchaWorker()
    warnings: list[str] = []
    jobs = PersonalCaptchaJobs(worker, log_warning=warnings.append)

    bundle = await jobs.execute_bundle("project-1", token_id=8)

    assert bundle is not None
    assert bundle["fingerprint"] is None
    assert bundle["session_cookies"] is None
    assert "mismatched slot identity" in warnings[0]
