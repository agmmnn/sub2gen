import inspect
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sub2gen.core.models import Token
from sub2gen.services.browser_captcha_personal import (
    BrowserCaptchaService,
    ResidentTabInfo,
    clear_cached_session_cookies,
    set_cached_session_cookies,
)
from sub2gen.services.flow_client import FlowClient
from sub2gen.services.token_manager import TokenManager


def make_token(**overrides):
    values = {
        "id": 7,
        "st": "session-token",
        "at": "access-token",
        "at_expires": datetime.now(timezone.utc) + timedelta(hours=4),
        "email": "account@example.com",
        "is_active": True,
    }
    values.update(overrides)
    return Token(**values)


class FakeTokenDb:
    def __init__(self, token):
        self.token = token
        self.update_calls = []

    async def get_token(self, token_id):
        return self.token if self.token and self.token.id == token_id else None

    async def update_token(self, token_id, **changes):
        self.update_calls.append((token_id, changes))
        if self.token and self.token.id == token_id:
            self.token = self.token.model_copy(update=changes)
        return True

    async def reset_error_count(self, _token_id):
        return None


class PersonalSlotIdentityTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        clear_cached_session_cookies()

    async def test_solve_bundle_uses_exact_slot_identity_not_global_cookies(self):
        service = BrowserCaptchaService()
        slot = ResidentTabInfo(object(), "slot-1", token_id=7)
        slot.fingerprint = {"user_agent": "Slot UA", "proxy_url": "http://slot-proxy"}
        slot.session_cookies = {"SID": "slot-cookie"}
        service._resident_tabs["slot-1"] = slot
        service._get_token_direct = AsyncMock(return_value=("captcha-token", "slot-1"))
        set_cached_session_cookies({"SID": "wrong-global-cookie"})

        bundle = await service.get_token_bundle("project-1", token_id=7)

        self.assertEqual(bundle["fingerprint"]["user_agent"], "Slot UA")
        self.assertEqual(bundle["proxy_url"], "http://slot-proxy")
        self.assertEqual(bundle["session_cookies"], {"SID": "slot-cookie"})

    async def test_mismatched_slot_never_leaks_another_token_identity(self):
        service = BrowserCaptchaService()
        slot = ResidentTabInfo(object(), "slot-1", token_id=99)
        slot.fingerprint = {"user_agent": "Wrong account UA"}
        slot.session_cookies = {"SID": "wrong-account-cookie"}
        service._resident_tabs["slot-1"] = slot
        service._get_token_direct = AsyncMock(return_value=("captcha-token", "slot-1"))
        service._last_fingerprint = {"user_agent": "Global wrong UA"}

        bundle = await service.get_token_bundle("project-1", token_id=7)

        self.assertIsNone(bundle["fingerprint"])
        self.assertIsNone(bundle["session_cookies"])

    def test_global_cookie_fallback_is_limited_to_non_slot_bundles(self):
        service = BrowserCaptchaService()
        set_cached_session_cookies({"SID": "global-cookie"})

        slot_bundle = service._build_solve_bundle(
            token="captcha-token",
            project_id="project-1",
            action="IMAGE_GENERATION",
            token_id=7,
            slot_id="slot-1",
        )
        legacy_bundle = service._build_solve_bundle(
            token="captcha-token",
            project_id="project-1",
            action="IMAGE_GENERATION",
            token_id=7,
            slot_id=None,
        )

        self.assertIsNone(slot_bundle["session_cookies"])
        self.assertEqual(legacy_bundle["session_cookies"], {"SID": "global-cookie"})

    async def test_cookie_extraction_supports_expanded_google_and_labs_domains(self):
        service = BrowserCaptchaService()
        slot = ResidentTabInfo(object(), "slot-1", token_id=7, browser_context_id="context-1")
        service._get_browser_cookies = AsyncMock(
            return_value=[
                {"name": "AEC", "value": "aec-value", "domain": ".google.com"},
                {"name": "SIDCC", "value": "sidcc-value", "domain": "labs.google"},
                {"name": "SID", "value": "wrong-domain", "domain": "example.com"},
            ]
        )

        result = await service._cache_session_cookies_for_computed(slot)

        self.assertEqual(result, {"AEC": "aec-value", "SIDCC": "sidcc-value"})
        self.assertEqual(slot.session_cookies, result)
        self.assertGreater(slot.session_cookies_fetched_at, 0)

    async def test_cookie_rebinding_invalidates_slot_cookie_snapshot_and_warms_context(self):
        service = BrowserCaptchaService()
        slot = ResidentTabInfo(object(), "slot-1", token_id=7)
        slot.cookie_signature = "old-signature"
        slot.session_cookies = {"SID": "old-cookie"}
        service._load_token_cookie = AsyncMock(return_value="SID=new-cookie")
        service._apply_token_cookie_binding = AsyncMock(return_value=True)
        service._wait_for_document_ready = AsyncMock(return_value=True)
        service._warmup_google_context_cookies = AsyncMock(return_value=True)
        service._wait_for_recaptcha = AsyncMock(return_value=True)

        self.assertTrue(
            await service._ensure_resident_token_binding(slot, 7, label="test-rebind")
        )

        service._apply_token_cookie_binding.assert_awaited_once()
        service._warmup_google_context_cookies.assert_awaited_once_with(
            slot,
            label="test-rebind:google_warmup",
        )

    async def test_missing_token_cookie_invalidates_slot_cookie_snapshot(self):
        service = BrowserCaptchaService()
        slot = ResidentTabInfo(object(), "slot-1", token_id=7)
        slot.cookie_signature = "old-signature"
        slot.session_cookies = {"SID": "old-cookie"}
        slot.session_cookies_fetched_at = 123.0
        service._load_token_cookie = AsyncMock(return_value=None)

        self.assertFalse(
            await service._apply_token_cookie_binding(slot, 7, label="missing-cookie")
        )
        self.assertIsNone(slot.session_cookies)
        self.assertEqual(slot.session_cookies_fetched_at, 0.0)

    def test_resident_and_legacy_initialization_delegate_google_warmup(self):
        resident_source = inspect.getsource(BrowserCaptchaService._create_resident_tab)
        legacy_source = inspect.getsource(BrowserCaptchaService._get_token_legacy)
        shutdown_source = inspect.getsource(BrowserCaptchaService._shutdown_browser_runtime_locked)

        self.assertIn("_warmup_google_context_cookies", resident_source)
        self.assertIn("_warmup_google_context_cookies", legacy_source)
        self.assertIn("clear_cached_session_cookies", shutdown_source)


class FlowIdentityPortTests(unittest.TestCase):
    def test_googleapis_is_cookie_allowlisted_without_broad_host_matching(self):
        client = FlowClient(proxy_manager=None)

        self.assertTrue(
            client._should_attach_runtime_session_cookies(
                "https://aisandbox-pa.googleapis.com/v1/test"
            )
        )
        self.assertFalse(
            client._should_attach_runtime_session_cookies(
                "https://googleapis.com.attacker.example/v1/test"
            )
        )

    def test_obsolete_project_initial_data_warmup_is_removed(self):
        source = inspect.getsource(FlowClient._warmup_flow_video_frontend_context)

        self.assertNotIn("flow.projectInitialData", source)
        self.assertIn("general.fetchUserPreferences", source)
        self.assertIn("videoFx.getFlowAppConfig", source)


class TokenValidationPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_validation_is_cached_for_five_minutes(self):
        token = make_token()
        db = FakeTokenDb(token)
        manager = TokenManager(db, flow_client=SimpleNamespace())
        manager._get_credits_for_token = AsyncMock(
            return_value={"credits": 42, "userPaygateTier": "PAYGATE_TIER_TWO"}
        )

        first = await manager.ensure_valid_token(token)
        second = await manager.ensure_valid_token(first)

        self.assertEqual(manager._get_credits_for_token.await_count, 1)
        self.assertEqual(second.credits, 42)
        self.assertTrue(manager._has_recent_at_validation(token.id))

    async def test_authentication_failure_triggers_refresh(self):
        token = make_token()
        db = FakeTokenDb(token)
        manager = TokenManager(db, flow_client=SimpleNamespace())
        manager._get_credits_for_token = AsyncMock(
            side_effect=RuntimeError("HTTP 401 UNAUTHENTICATED")
        )
        manager._refresh_at = AsyncMock(return_value=True)

        result = await manager.ensure_valid_token(token)

        self.assertIsNotNone(result)
        manager._refresh_at.assert_awaited_once_with(token.id)

    async def test_transient_validation_failure_keeps_token_and_backs_off(self):
        token = make_token()
        db = FakeTokenDb(token)
        manager = TokenManager(db, flow_client=SimpleNamespace())
        manager._get_credits_for_token = AsyncMock(
            side_effect=RuntimeError("temporary upstream timeout")
        )
        manager._refresh_at = AsyncMock(return_value=True)

        first = await manager.ensure_valid_token(token)
        second = await manager.ensure_valid_token(first)

        self.assertIs(first, token)
        self.assertIs(second, token)
        self.assertEqual(manager._get_credits_for_token.await_count, 1)
        manager._refresh_at.assert_not_awaited()
        self.assertTrue(manager._is_at_validation_deferred(token.id))

    async def test_credential_update_reenables_token_and_clears_validation_state(self):
        token = make_token(is_active=False, ban_reason="manual", banned_at=datetime.now(timezone.utc))
        db = FakeTokenDb(token)
        manager = TokenManager(db, flow_client=SimpleNamespace())
        manager._mark_at_valid(token.id)

        await manager.update_token(token.id, at="replacement-at")

        changes = db.update_calls[-1][1]
        self.assertEqual(changes["at"], "replacement-at")
        self.assertTrue(changes["is_active"])
        self.assertIsNone(changes["ban_reason"])
        self.assertFalse(manager._has_recent_at_validation(token.id))

    async def test_failed_refresh_and_disable_clear_validation_state(self):
        token = make_token()
        db = FakeTokenDb(token)
        manager = TokenManager(db, flow_client=SimpleNamespace())
        manager._mark_at_valid(token.id)
        manager._refresh_at_inner = AsyncMock(return_value=False)

        self.assertFalse(await manager._refresh_at(token.id))
        self.assertFalse(manager._has_recent_at_validation(token.id))

        manager._mark_at_valid(token.id)
        await manager.disable_token(token.id)
        self.assertFalse(manager._has_recent_at_validation(token.id))


if __name__ == "__main__":
    unittest.main()
