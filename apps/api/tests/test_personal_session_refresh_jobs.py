from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from sub2gen.workers.personal import PersonalSessionRefreshJobs, ResidentTabInfo


async def no_sleep(_seconds: float) -> None:
    return None


class RefreshWorker:
    def __init__(self) -> None:
        self._resident_lock = asyncio.Lock()
        self._session_refresh_timeout_seconds = 5.0
        self._resident_error_streaks = {"slot-1": 2}
        self.info = ResidentTabInfo(object(), "slot-1", project_id="project-1", token_id=7)
        self.remembered: list[tuple[str, Any]] = []
        self.health: list[bool] = []

    def _mark_runtime_active(self) -> None:
        return None

    async def initialize(self) -> None:
        return None

    def _resolve_resident_slot_for_project_locked(self, _project_id: str, *, token_id: int | None):
        return self.info.slot_id, self.info

    async def _ensure_resident_token_binding(self, _info: ResidentTabInfo, _token_id: int | None, *, label: str):
        return True

    async def _tab_reload(self, _tab: Any, *, label: str) -> None:
        return None

    async def _run_with_timeout(self, awaitable: Any, *, timeout_seconds: float, label: str):
        return await awaitable

    async def _tab_evaluate(self, _tab: Any, expression: str, **_kwargs: Any):
        return "complete" if expression == "document.readyState" else ""

    async def _wait_for_recaptcha(self, _tab: Any) -> bool:
        return True

    async def _get_browser_cookies(self, **_kwargs: Any):
        return [SimpleNamespace(name="__Secure-next-auth.session-token", value="refreshed-token")]

    def _remember_project_affinity(self, project_id: str, slot_id: str, _info: ResidentTabInfo) -> None:
        self.remembered.append(("project", project_id, slot_id))

    def _remember_token_affinity(self, token_id: int | None, slot_id: str, _info: ResidentTabInfo) -> None:
        self.remembered.append(("token", token_id, slot_id))

    def _mark_browser_health(self, healthy: bool) -> None:
        self.health.append(healthy)


@pytest.mark.asyncio
async def test_refresh_executor_reloads_tab_extracts_cookie_and_updates_identity() -> None:
    worker = RefreshWorker()
    executor = PersonalSessionRefreshJobs(worker, sleep=no_sleep)

    result = await executor.execute("project-1", token_id=7)

    assert result == "refreshed-token"
    assert worker.info.recaptcha_ready is True
    assert worker._resident_error_streaks == {}
    assert worker.remembered == [
        ("project", "project-1", "slot-1"),
        ("token", 7, "slot-1"),
    ]
    assert worker.health == [True]


def test_document_cookie_parser_matches_only_session_token() -> None:
    assert (
        PersonalSessionRefreshJobs._session_token_from_cookie_text(
            "SID=one; __Secure-next-auth.session-token=two; AEC=three"
        )
        == "two"
    )
    assert PersonalSessionRefreshJobs._session_token_from_cookie_text("SID=one") is None
