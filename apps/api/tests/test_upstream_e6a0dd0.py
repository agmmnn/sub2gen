import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sub2gen.core.config import Config, DEFAULT_YESCAPTCHA_TASK_TYPE
from sub2gen.services.browser_captcha_personal import (
    BrowserCaptchaService,
    TokenPoolLease,
    _PersonalBrowserPoolService,
    set_cached_session_cookies,
)
from sub2gen.services.flow_client import FlowClient
from sub2gen.services.generation_handler import GenerationHandler


class UpstreamE6ConfigTests(unittest.TestCase):
    def test_yescaptcha_default_is_m1s9(self):
        self.assertEqual(DEFAULT_YESCAPTCHA_TASK_TYPE, "RecaptchaV3TaskProxylessM1S9")

    def test_personal_headless_environment_override(self):
        with patch.dict("os.environ", {"PERSONAL_BROWSER_HEADLESS": "true"}):
            self.assertTrue(Config().personal_headless)
            self.assertTrue(BrowserCaptchaService().headless)


class PersonalSolveBundleTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        set_cached_session_cookies({})

    async def test_single_worker_bundle_preserves_browser_context(self):
        service = BrowserCaptchaService()
        service._proxy_url = "http://127.0.0.1:8080"
        set_cached_session_cookies({"SID": "cookie-secret"})
        bundle = service._build_solve_bundle(
            token="captcha-token",
            project_id="project-1",
            action="VIDEO_GENERATION",
            token_id=7,
            slot_id="worker-0:slot-1",
            fingerprint={"user_agent": "Browser UA"},
            session_cookies={"SID": "cookie-secret"},
        )
        self.assertEqual(bundle["proxy_url"], "http://127.0.0.1:8080")
        self.assertEqual(bundle["fingerprint"]["proxy_url"], "http://127.0.0.1:8080")
        self.assertEqual(bundle["session_cookies"], {"SID": "cookie-secret"})
        self.assertGreater(bundle["expires_at"], bundle["issued_at"])

    async def test_pool_returns_the_exact_bundle_stored_with_lease(self):
        pool = _PersonalBrowserPoolService()
        bundle = {
            "token": "captcha-token",
            "fingerprint": {"user_agent": "Browser UA"},
            "session_cookies": {"SID": "cookie-secret"},
        }
        now = time.time()
        lease = TokenPoolLease(
            bucket_key="bucket",
            token="captcha-token",
            project_id="project-1",
            action="IMAGE_GENERATION",
            token_id=7,
            slot_id="worker-0:slot-1",
            worker_index=0,
            solve_bundle=bundle,
            created_at=now,
            expires_at=now + 120,
        )
        pool._is_token_pool_enabled = lambda: True
        pool._get_token_pool_bucket_target_size = lambda **_kwargs: 1

        async def wait_for_token(**_kwargs):
            return lease

        pool._wait_for_token_pool_token = wait_for_token
        pool._remember_affinity = lambda **_kwargs: None
        result = await pool.get_token_bundle("project-1", token_id=7)
        self.assertEqual(result, bundle)
        self.assertIsNot(result, bundle)


class FlowRequestIdentityTests(unittest.TestCase):
    def test_cookie_relay_is_host_limited_and_preserves_explicit_cookie(self):
        client = FlowClient(proxy_manager=None)
        self.assertTrue(client._should_attach_runtime_session_cookies("https://labs.google/api"))
        self.assertFalse(client._should_attach_runtime_session_cookies("https://example.com/api"))
        merged = client._merge_cookie_header("SID=explicit", {"SID": "browser", "NID": "extra"})
        self.assertEqual(merged, "SID=explicit; NID=extra")

    def test_browser_headers_normalize_chrome_brand_and_language(self):
        client = FlowClient(proxy_manager=None)
        ua = "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36"
        self.assertIn("Google Chrome", client._normalize_sec_ch_ua_header('"Chromium";v="149"', user_agent=ua))
        self.assertEqual(
            client._normalize_accept_language_header("en-US,en,fr"),
            "en-US,en;q=0.9,fr;q=0.8",
        )


class FlowRequestIdentityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_applies_bundle_cookies_origin_referer_and_client_hints(self):
        client = FlowClient(proxy_manager=None)
        client._set_request_fingerprint(
            {
                "user_agent": "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36",
                "accept_language": "en-US,en;q=0.9",
                "sec_ch_ua": '"Chromium";v="149"',
                "sec_ch_ua_mobile": "?0",
                "sec_ch_ua_platform": '"Windows"',
                "session_cookies": {"SID": "browser-cookie", "NID": "extra-cookie"},
                "project_id": "project-1",
            }
        )
        captured = {}
        response = SimpleNamespace(status_code=200, headers={}, text="{}", json=lambda: {})

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, _url, **kwargs):
                captured.update(kwargs)
                return response

        with patch("sub2gen.services.flow_client.AsyncSession", return_value=FakeSession()):
            await client._make_request(
                "POST",
                "https://labs.google/fx/api/test",
                headers={"Cookie": "SID=explicit-cookie"},
                json_data={"clientContext": {"projectId": "project-1"}},
            )

        headers = captured["headers"]
        self.assertEqual(headers["Cookie"], "SID=explicit-cookie; NID=extra-cookie")
        self.assertEqual(headers["Origin"], "https://labs.google")
        self.assertEqual(headers["Referer"], client._build_flow_project_page_url("project-1"))
        self.assertIn("Google Chrome", headers["sec-ch-ua"])


class TokenErrorClassificationTests(unittest.TestCase):
    def test_captcha_provider_errors_do_not_penalize_account(self):
        self.assertFalse(
            GenerationHandler._should_count_token_error(None, Exception("YesCaptcha ERROR_NO_SLOT_AVAILABLE"))
        )
        self.assertTrue(
            GenerationHandler._should_count_token_error(None, Exception("HTTP Error 401: invalid account token"))
        )


if __name__ == "__main__":
    unittest.main()
