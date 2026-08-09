import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from sub2gen.api import admin
from sub2gen.api.admin import AddTokenRequest, BrowserProfileTokenRequest, ImportTokenItem
from sub2gen.core.browser_runtime_status import (
    finish_runtime_prepare,
    get_runtime_status,
    reset_runtime_prepare,
    start_runtime_prepare,
)
from sub2gen.core.config import Config, DEFAULT_YESCAPTCHA_TASK_TYPE
from sub2gen.core.database import Database
from sub2gen.core.model_resolver import VIDEO_BASE_MODELS, resolve_model_name
from sub2gen.core.models import Token, TokenRefreshConfig
from sub2gen.services import browser_captcha_personal
from sub2gen.services.browser_captcha import TokenBrowser
from sub2gen.services.flow_client import FlowClient
from sub2gen.services.generation_handler import MODEL_CONFIG
from sub2gen.services.protocol_login import (
    _validate_redirect,
    normalize_proxy_url,
    parse_google_cookies,
)
from sub2gen.services.token_manager import TokenManager


class Upstream052ConfigTests(unittest.TestCase):
    def test_yescaptcha_default_reflects_latest_integrated_upstream(self):
        self.assertEqual(DEFAULT_YESCAPTCHA_TASK_TYPE, "RecaptchaV3TaskProxylessM1S9")

    def test_browser_retry_settings_are_bounded(self):
        cfg = Config()
        cfg._config.setdefault("captcha", {})["browser_captcha_max_retries"] = 99
        cfg._config["captcha"]["browser_captcha_generation_retries"] = "bad"
        self.assertEqual(cfg.browser_captcha_max_retries, 20)
        self.assertEqual(cfg.browser_captcha_generation_retries, 6)

    def test_omni_aliases_and_model_configs_exist(self):
        self.assertEqual(resolve_model_name("omni", model_config=MODEL_CONFIG), "omni")
        self.assertEqual(VIDEO_BASE_MODELS["omni"]["portrait"], "omni_portrait")
        self.assertEqual(MODEL_CONFIG["omni"]["model_key"], "abra_t2v_8s")
        self.assertEqual(MODEL_CONFIG["omni"]["reference_model_key"], "abra_r2v_8s")
        self.assertEqual(MODEL_CONFIG["omni"]["max_images"], 3)

    def test_protocol_fields_belong_to_normal_token_requests(self):
        request = AddTokenRequest(
            st="session-token",
            protocol_mode="protocol",
            google_cookies="SID=secret",
            login_account="person@example.com",
            proxy_url="127.0.0.1:8080",
            refresh_interval_minutes=45,
        )
        self.assertEqual(request.protocol_mode, "protocol")
        self.assertEqual(request.refresh_interval_minutes, 45)
        self.assertNotIn("protocol_mode", BrowserProfileTokenRequest.model_fields)
        self.assertIn("login_password", ImportTokenItem.model_fields)


class BrowserRuntimeStatusTests(unittest.TestCase):
    def test_status_transitions_are_isolated_and_copy_safe(self):
        reset_runtime_prepare("browser")
        started = start_runtime_prepare("browser", "starting")
        started["message"] = "mutated"
        live = get_runtime_status("browser")
        self.assertEqual(live["state"], "running")
        self.assertEqual(live["message"], "starting")
        finished = finish_runtime_prepare("browser", "ready")
        self.assertEqual(finished["state"], "ready")
        self.assertFalse(finished["active"])
        self.assertIsNotNone(finished["last_completed_at"])


class BrowserEnvironmentPatchTests(unittest.TestCase):
    def test_browser_mode_patch_covers_required_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            browser = TokenBrowser(3, tmp)
            source = browser._build_browser_environment_patch_source()
        for marker in (
            "__sub2genBrowserEnvironmentV2",
            "hardwareConcurrency",
            "deviceMemory",
            "WebGLRenderingContext",
            "navigator.gpu",
            "navigator.storage",
            "mediaDevices",
            "performance.memory",
            "document.fonts",
            "window.chrome",
        ):
            self.assertIn(marker, source)

    def test_personal_nodriver_bridge_uses_browser_send_and_compat_transaction(self):
        patch_source = inspect.getsource(browser_captcha_personal._patch_nodriver_connection_instance)
        service_source = inspect.getsource(browser_captcha_personal.BrowserCaptchaService)
        self.assertIn("class _CompatTransaction", patch_source)
        self.assertNotIn(".connection.send(", service_source)

    def test_personal_capabilities_cookie_cache_and_configurable_headless(self):
        browser_captcha_personal.set_cached_session_cookies({"SID": "secret"})
        self.assertEqual(browser_captcha_personal.get_cached_session_cookies(), {"SID": "secret"})
        with patch.dict("os.environ", {"PERSONAL_BROWSER_HEADLESS": "true"}):
            service = browser_captcha_personal.BrowserCaptchaService()
            self.assertTrue(service.headless)
        tab = MagicMock()
        tab.target_id = "test-target"
        profile = service._build_tab_fingerprint_spoof_config(tab)
        self.assertIn("bluetoothAvailable", profile["capability"])
        source = service._build_tab_fingerprint_spoof_source(tab)
        for marker in ("navigator.bluetooth", "navigator.usb", "navigator.serial", "navigator.hid", "mediaCapabilities", "speechSynthesis", "wakeLock", "openDatabase"):
            self.assertIn(marker, source)


class CreationAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_agent_helpers_parse_sse_and_entity_ids(self):
        client = FlowClient(MagicMock())
        events = client._parse_sse_json_events(
            'data: {"agentMessage":{"agentEvents":[{"toolResult":{"toolName":"generate_video_with_references","toolResult":{"media_id":"media-1"}}}]}}\n\n'
        )
        self.assertEqual(client._extract_generate_video_with_references_result(events)["media_id"], "media-1")
        client._labs_trpc_post_with_st = AsyncMock(return_value={"result": {"data": {"json": {"entityId": "entity-1"}}}})
        self.assertEqual(await client.create_flow_entity("st", "project-1"), "entity-1")
        self.assertTrue(callable(client.generate_omni_reference_video))

    async def test_video_warmup_submits_page_view_telemetry(self):
        client = FlowClient(MagicMock())
        client._get_token_st_by_id = AsyncMock(return_value="session-token")
        client._labs_trpc_get_with_st = AsyncMock(return_value={})
        client._labs_trpc_post_with_st = AsyncMock(return_value={})
        client._aisandbox_request = AsyncMock(return_value={})

        await client._warmup_flow_video_frontend_context(
            at="access-token",
            project_id="project-1",
            token_id=1,
            session_id="client-session",
            user_paygate_tier="PAYGATE_TIER_ONE",
            prompt="prompt",
            model_key="abra_r2v_10s",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
        )

        args = client._labs_trpc_post_with_st.await_args.args
        self.assertEqual(args[0], "general.submitBatchLog")
        event = args[1]["json"]["appEvents"][0]
        self.assertEqual(event["event"], "PAGE_VIEW")
        self.assertEqual(event["eventMetadata"]["sessionId"], "client-session")


class PluginSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_check_tokens_uses_connection_token_and_filters_email(self):
        fake_db = MagicMock()
        fake_db.get_plugin_config = AsyncMock(return_value=MagicMock(connection_token="connection-secret"))
        fake_db.get_all_tokens_with_stats = AsyncMock(return_value=[{
            "id": 1,
            "st": "st",
            "email": "person@example.com",
            "is_active": True,
            "protocol_mode": "protocol",
            "refresh_interval_minutes": 120,
        }])
        fake_manager = MagicMock()
        fake_manager.needs_at_refresh.return_value = False
        container = SimpleNamespace(db=fake_db, token_manager=fake_manager)
        result = await admin.plugin_check_tokens(
            {"emails": ["PERSON@example.com"]},
            authorization="Bearer connection-secret",
            container=container,
        )
        self.assertEqual(result["tokens"][0]["email"], "person@example.com")
        with self.assertRaises(HTTPException):
            await admin._verify_plugin_connection_token("Bearer wrong", fake_db)


class ProtocolLoginUtilityTests(unittest.TestCase):
    def test_cookie_exports_and_cookie_headers_are_parsed(self):
        self.assertEqual(
            parse_google_cookies('[{"name":"SID","value":"one"}]')["SID"],
            "one",
        )
        self.assertEqual(parse_google_cookies("SID=one; HSID=two")["HSID"], "two")

    def test_proxy_notations_are_normalized(self):
        self.assertEqual(normalize_proxy_url("127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(
            normalize_proxy_url("host:1080:user:pass"),
            "http://user:pass@host:1080",
        )
        self.assertIsNone(normalize_proxy_url("ftp://host:21"))

    def test_oauth_redirects_are_allowlisted(self):
        self.assertEqual(
            _validate_redirect("https://accounts.google.com/o/oauth2/auth"),
            "https://accounts.google.com/o/oauth2/auth",
        )
        with self.assertRaises(RuntimeError):
            _validate_redirect("https://example.com/steal")
        with self.assertRaises(RuntimeError):
            _validate_redirect("https://labs.google/not-a-callback", callback_only=True)


class ProtocolRefreshDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_persists_protocol_fields_and_global_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "sub2gen.db"))
            await db.init_db()
            token = Token(
                st="session-token",
                email="person@example.com",
                protocol_mode="protocol",
                google_cookies="SID=secret",
                login_account="person@example.com",
                login_password="password-secret",
                proxy_url="http://proxy-user:proxy-pass@127.0.0.1:8080",
                refresh_interval_minutes=45,
            )
            token_id = await db.add_token(token)
            stored = await db.get_token(token_id)
            self.assertEqual(stored.protocol_mode, "protocol")
            self.assertEqual(stored.google_cookies, "SID=secret")
            self.assertEqual(stored.login_password, "password-secret")
            self.assertEqual(stored.refresh_interval_minutes, 45)

            updated = await db.update_token_refresh_config(
                enabled=False,
                refresh_interval_minutes=10_081,
            )
            self.assertFalse(updated.enabled)
            self.assertEqual(updated.refresh_interval_minutes, 10_080)


class ProtocolRefreshManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_token_uses_configured_proxy_for_initial_exchange(self):
        db = MagicMock()
        db.get_token_by_st = AsyncMock(return_value=None)
        db.add_token = AsyncMock(return_value=11)

        flow_client = MagicMock()
        flow_client.proxy_manager.normalize_proxy_url.return_value = "http://proxy:8080"
        flow_client.get_request_fingerprint.return_value = None
        flow_client.st_to_at = AsyncMock(return_value={
            "access_token": "access-token",
            "expires": "2026-08-01T00:00:00Z",
            "user": {"email": "person@example.com"},
        })
        flow_client.get_credits = AsyncMock(return_value={"credits": 12})

        token = await TokenManager(db, flow_client).add_token(
            "session-token",
            proxy_url="proxy:8080",
        )

        self.assertEqual(token.id, 11)
        self.assertEqual(token.proxy_url, "http://proxy:8080")
        flow_client.st_to_at.assert_awaited_once_with("session-token")
        flow_client.get_credits.assert_awaited_once_with("access-token")
        applied_fingerprints = [call.args[0] for call in flow_client._set_request_fingerprint.call_args_list]
        self.assertIn({"proxy_url": "http://proxy:8080"}, applied_fingerprints)

    async def test_due_protocol_token_refreshes_st_at_and_credits(self):
        token = Token(
            id=7,
            st="old-st",
            at="old-at",
            email="person@example.com",
            is_active=True,
            protocol_mode="protocol",
            google_cookies="SID=secret",
            auto_refresh_enabled=True,
            refresh_interval_minutes=1,
            last_st_refresh_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        db = MagicMock()
        db.get_token_refresh_config = AsyncMock(return_value=TokenRefreshConfig())
        db.get_active_tokens = AsyncMock(return_value=[token])
        db.get_token = AsyncMock(return_value=token)
        db.update_token = AsyncMock()

        flow_client = MagicMock()
        flow_client.get_request_fingerprint.return_value = None
        flow_client._set_request_fingerprint = MagicMock()
        flow_client.st_to_at = AsyncMock(return_value={
            "access_token": "new-at",
            "expires": "2026-08-01T00:00:00Z",
            "user": {"email": "person@example.com"},
        })
        flow_client.get_credits = AsyncMock(return_value={
            "credits": 321,
            "userPaygateTier": "PAYGATE_TIER_ONE",
        })

        manager = TokenManager(db, flow_client)
        with patch(
            "sub2gen.services.protocol_login.protocol_loginer.login",
            new=AsyncMock(return_value={"success": True, "session_token": "new-st"}),
        ):
            await manager.run_protocol_refresh_once()

        updates = [call.kwargs for call in db.update_token.await_args_list]
        self.assertTrue(any(update.get("st") == "new-st" for update in updates))
        self.assertTrue(any(update.get("at") == "new-at" for update in updates))
        self.assertTrue(any(update.get("credits") == 321 for update in updates))

    async def test_not_due_protocol_token_is_skipped(self):
        token = Token(
            id=8,
            st="old-st",
            email="person@example.com",
            protocol_mode="protocol",
            google_cookies="SID=secret",
            last_st_refresh_at=datetime.now(timezone.utc),
            refresh_interval_minutes=120,
        )
        db = MagicMock()
        db.get_token_refresh_config = AsyncMock(return_value=TokenRefreshConfig())
        db.get_active_tokens = AsyncMock(return_value=[token])
        db.update_token = AsyncMock()
        flow_client = MagicMock()
        manager = TokenManager(db, flow_client)
        await manager.run_protocol_refresh_once()
        db.update_token.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
