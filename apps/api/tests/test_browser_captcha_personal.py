import itertools
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

from sub2gen.services.browser_captcha_personal import (
    BrowserCaptchaService,
    PERSONAL_POOL_MAX_TOTAL_RESIDENT_TABS,
    _PersonalBrowserPoolService,
    _is_nodriver_connection_closed,
    _is_runtime_disconnect_error,
    _patch_nodriver_connection_instance,
)


class _FakeWorker:
    def __init__(
        self,
        name: str,
        *,
        initialized: bool = False,
        live: bool = False,
        restart_pending: bool = False,
        cooldown_seconds: float = 0.0,
        warmup_error: Exception | None = None,
    ):
        self.name = name
        self.db = None
        self._initialized = initialized
        self.browser = (
            types.SimpleNamespace(stopped=False, _sub2gen_runtime_disconnected=False)
            if live
            else None
        )
        self._fresh_profile_restart_pending = restart_pending
        self._fresh_profile_restart_task = None
        self._max_resident_tabs = 5
        self._resident_tabs = {}
        self._project_resident_affinity = {}
        self._token_resident_affinity = {}
        self._cooldown_seconds = cooldown_seconds
        self._warmup_error = warmup_error
        self.warmup_calls: list[tuple[list[str], int]] = []
        self.pool_settings: list[tuple[int, int]] = []
        self.reload_config = AsyncMock()

    def get_resident_count(self) -> int:
        return 0

    def _get_browser_launch_cooldown_remaining_seconds(self) -> float:
        return self._cooldown_seconds

    def apply_pool_worker_settings(
        self,
        *,
        browser_instance_id: int,
        max_resident_tabs_override: int,
    ) -> None:
        self.pool_settings.append((browser_instance_id, max_resident_tabs_override))
        self._max_resident_tabs = max_resident_tabs_override

    async def warmup_resident_tabs(
        self,
        project_ids: list[str],
        *,
        limit: int,
    ) -> list[str]:
        self.warmup_calls.append((list(project_ids), limit))
        if self._warmup_error is not None:
            raise self._warmup_error
        return [f"{self.name}:{project_id}" for project_id in project_ids[:limit]]


class _FakeWebSocket:
    def __init__(self, owner, *, close_code=None):
        self.owner = owner
        self.close_code = close_code
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        payload = json.loads(message)
        transaction = self.owner.mapper[payload["id"]]
        transaction(result={"ok": True})


class _ConnectionWithoutClosed:
    def __init__(self):
        self.mapper = {}
        self.handlers = {}
        self.websocket = None
        self.connect_count = 0
        self.register_count = 0
        self.__count__ = itertools.count(0)

    async def send(self, _cdp_obj, _is_update=False):
        raise AssertionError("original send should be patched")

    async def connect(self):
        self.connect_count += 1
        self.websocket = _FakeWebSocket(self)

    async def _register_handlers(self):
        self.register_count += 1


class _ConnectionWithoutWebSocket(_ConnectionWithoutClosed):
    async def connect(self):
        self.connect_count += 1


class _ConnectionWithBrokenClosedProperty(_ConnectionWithoutClosed):
    @property
    def closed(self):
        raise RuntimeError("connection state unavailable")


def _fake_cdp_command():
    result = yield {"method": "Runtime.evaluate", "params": {}}
    return result


class NodriverConnectionCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_patch_handles_connection_without_closed_attribute(self):
        connection = _ConnectionWithoutClosed()

        _patch_nodriver_connection_instance(connection)
        result = await connection.send(_fake_cdp_command())

        self.assertEqual(result, {"ok": True})
        self.assertEqual(connection.connect_count, 1)
        self.assertEqual(connection.register_count, 1)
        self.assertTrue(getattr(connection, "_sub2gen_send_patched", False))

    async def test_missing_websocket_after_connect_cleans_mapper(self):
        connection = _ConnectionWithoutWebSocket()

        _patch_nodriver_connection_instance(connection)
        with self.assertRaisesRegex(ConnectionError, "websocket unavailable"):
            await connection.send(_fake_cdp_command())

        self.assertEqual(connection.connect_count, 1)
        self.assertEqual(connection.mapper, {})

    def test_connection_state_falls_back_to_websocket(self):
        connection = _ConnectionWithoutClosed()
        self.assertTrue(_is_nodriver_connection_closed(connection))

        connection.websocket = _FakeWebSocket(connection)
        self.assertFalse(_is_nodriver_connection_closed(connection))

        connection.websocket.close_code = 1000
        self.assertTrue(_is_nodriver_connection_closed(connection))
        self.assertTrue(
            _is_nodriver_connection_closed(_ConnectionWithBrokenClosedProperty())
        )

    def test_invalid_connection_object_is_not_patched(self):
        connection = types.SimpleNamespace(send=lambda *_args, **_kwargs: None)

        _patch_nodriver_connection_instance(connection)

        self.assertFalse(getattr(connection, "_sub2gen_send_patched", False))

    def test_compatibility_errors_are_runtime_disconnects(self):
        self.assertTrue(
            _is_runtime_disconnect_error(
                AttributeError("connection has no attribute 'closed'")
            )
        )
        self.assertTrue(
            _is_runtime_disconnect_error(
                AttributeError("'NoneType' object has no attribute 'send'")
            )
        )
        self.assertTrue(
            _is_runtime_disconnect_error(
                ConnectionError("nodriver websocket unavailable after connect")
            )
        )


class BrowserCaptchaPersonalEnvironmentTests(unittest.TestCase):
    def test_runtime_surface_profile_contains_extended_browser_environment(self):
        service = BrowserCaptchaService(browser_instance_id=1, max_resident_tabs_override=5)

        profile = service._get_runtime_surface_profile()

        self.assertIn("webgpu", profile)
        self.assertIn("mediaQueries", profile)
        self.assertIn("storage", profile)
        self.assertIn("behavior", profile)
        self.assertIn("visualViewport", profile["window"])
        self.assertIn("supportedExtensions", profile["graphics"])
        self.assertIn("WEBGL_debug_renderer_info", profile["graphics"]["supportedExtensions"])

        source = service._build_tab_fingerprint_spoof_source(
            types.SimpleNamespace(target_id="unit-tab")
        )
        for marker in (
            "ensureWebGpuEnvironment",
            "ensureMatchMediaEnvironment",
            "ensureVisualViewportEnvironment",
            "navigator.storage",
            "getSupportedConstraints",
            "userActivation",
        ):
            self.assertIn(marker, source)


class BrowserCaptchaPersonalIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_browser_user_agent_priority(self):
        service = BrowserCaptchaService(browser_instance_id=1, max_resident_tabs_override=5)
        service._last_fingerprint = {"user_agent": " fingerprint-ua "}
        service._runtime_surface_profile = {"userAgent": "profile-ua"}
        service._get_live_browser_runtime_identity = AsyncMock(
            return_value=("live-ua", "Chrome/124")
        )

        self.assertEqual(await service.get_current_user_agent(), "fingerprint-ua")
        service._get_live_browser_runtime_identity.assert_not_awaited()

        service._last_fingerprint = None
        self.assertEqual(await service.get_current_user_agent(), "profile-ua")
        service._get_live_browser_runtime_identity.assert_not_awaited()

        service._runtime_surface_profile = {}
        self.assertEqual(await service.get_current_user_agent(), "live-ua")
        service._get_live_browser_runtime_identity.assert_awaited_once()

    async def test_pool_prefers_last_successful_worker_and_recovers_from_failure(self):
        first = types.SimpleNamespace(
            get_last_fingerprint=lambda: None,
            get_current_user_agent=AsyncMock(return_value="first-worker-ua"),
        )
        preferred = types.SimpleNamespace(
            get_last_fingerprint=lambda: None,
            get_current_user_agent=AsyncMock(side_effect=RuntimeError("worker unavailable")),
        )
        pool = _PersonalBrowserPoolService()
        pool._workers = [first, preferred]
        pool._last_successful_worker_index = 1

        self.assertEqual(await pool.get_current_user_agent(), "first-worker-ua")
        preferred.get_current_user_agent.assert_awaited_once()
        first.get_current_user_agent.assert_awaited_once()


class BrowserCaptchaPersonalPoolTests(unittest.IsolatedAsyncioTestCase):
    def test_pool_tab_limits_use_browser_count_times_per_worker_tabs(self):
        pool = _PersonalBrowserPoolService()

        self.assertEqual(pool._build_worker_tab_limits(5, 10), [5] * 10)

        capped_limits = pool._build_worker_tab_limits(5, 20)
        self.assertEqual(len(capped_limits), 20)
        self.assertEqual(sum(capped_limits), PERSONAL_POOL_MAX_TOTAL_RESIDENT_TABS)
        self.assertLessEqual(max(capped_limits), 5)

        warmup_limits = pool._build_worker_tab_limits(
            5,
            10,
            total_limit=5,
            allow_zero=True,
        )
        self.assertEqual(len(warmup_limits), 10)
        self.assertEqual(sum(warmup_limits), 5)
        self.assertEqual(sum(1 for item in warmup_limits if item > 0), 5)

    def test_effective_capacity_normalizes_boundaries(self):
        pool = _PersonalBrowserPoolService()

        self.assertEqual(
            pool._resolve_effective_pool_tab_capacity(browser_count=0, per_worker_tabs=0),
            1,
        )
        self.assertEqual(
            pool._resolve_effective_pool_tab_capacity(browser_count=4, per_worker_tabs=5),
            20,
        )
        self.assertEqual(
            pool._resolve_effective_pool_tab_capacity(browser_count=20, per_worker_tabs=50),
            PERSONAL_POOL_MAX_TOTAL_RESIDENT_TABS,
        )

    def test_pool_dispatch_prefers_cold_idle_worker_over_busy_live_worker(self):
        pool = _PersonalBrowserPoolService()
        live_worker = _FakeWorker("live", initialized=True, live=True)
        cold_worker = _FakeWorker("cold")
        pool._workers = [live_worker, cold_worker]
        pool._worker_dispatch_reservations = {0: 1}

        self.assertLess(
            pool._worker_dispatch_score(1, cold_worker),
            pool._worker_dispatch_score(0, live_worker),
        )

    async def test_acquire_worker_avoids_restart_and_launch_cooldown(self):
        pool = _PersonalBrowserPoolService()
        restarting_worker = _FakeWorker("restarting", restart_pending=True)
        cooldown_worker = _FakeWorker("cooldown", cooldown_seconds=30.0)
        available_worker = _FakeWorker("available")
        pool._workers = [restarting_worker, cooldown_worker, available_worker]

        worker_index, worker = await pool._acquire_worker(
            ensure_workers=False,
            allow_affinity=False,
        )
        try:
            self.assertEqual(worker_index, 2)
            self.assertIs(worker, available_worker)
        finally:
            await pool._release_worker_reservation(worker_index)

    async def test_pool_warmup_distributes_projects_and_keeps_partial_successes(self):
        pool = _PersonalBrowserPoolService()
        first = _FakeWorker("first")
        failing = _FakeWorker("failing", warmup_error=RuntimeError("warmup failed"))
        third = _FakeWorker("third")
        pool._workers = [first, failing, third]
        pool._ensure_workers = AsyncMock()
        pool._resolve_worker_resident_tabs = lambda limit=None: 2
        pool._resolve_effective_pool_tab_capacity = lambda **kwargs: 6

        warmed_slots = await pool.warmup_resident_tabs(
            ["project-1", "project-2", "project-3", "project-4", "project-5"],
            limit=5,
        )

        pool._ensure_workers.assert_awaited_once()
        self.assertEqual([limit for _, limit in first.warmup_calls], [2])
        self.assertEqual([limit for _, limit in failing.warmup_calls], [2])
        self.assertEqual([limit for _, limit in third.warmup_calls], [1])
        self.assertEqual(
            warmed_slots,
            ["first:project-1", "first:project-4", "third:project-3"],
        )

    async def test_reload_applies_per_worker_tab_limit_to_existing_workers(self):
        pool = _PersonalBrowserPoolService()
        workers = [_FakeWorker(f"worker-{index}") for index in range(1, 4)]
        pool._workers = workers
        pool._resolve_worker_resident_tabs = lambda limit=None: 4
        pool._ensure_idle_worker_reaper = AsyncMock()
        pool._is_token_pool_enabled = lambda: False

        with patch.object(
            BrowserCaptchaService,
            "_resolve_configured_browser_count",
            return_value=3,
        ):
            await pool._ensure_workers(reload_existing=True)

        self.assertEqual(pool._worker_tab_limits, [4, 4, 4])
        self.assertEqual(
            [worker.pool_settings for worker in workers],
            [[(1, 4)], [(2, 4)], [(3, 4)]],
        )
        for worker in workers:
            worker.reload_config.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
