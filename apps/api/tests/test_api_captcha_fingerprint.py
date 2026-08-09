"""Tests for API/extension captcha User-Agent and proxy binding."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sub2gen.core.config import config
from sub2gen.services.browser_captcha_extension import (
    ExtensionCaptchaService,
    ExtensionConnection,
    normalize_extension_captcha_user_agent,
)
from sub2gen.services.flow_client import FlowClient


PROVIDER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


class _FakeProxyManager:
    def __init__(self, proxy_url="http://127.0.0.1:8080"):
        self.proxy_url = proxy_url
        self.calls = 0

    async def get_request_proxy_url(self):
        self.calls += 1
        return self.proxy_url

    async def get_proxy_url(self):
        return self.proxy_url


class _RotatingProxyManager:
    def __init__(self):
        self.calls = 0

    async def get_request_proxy_url(self):
        self.calls += 1
        return None if self.calls == 1 else "http://different-proxy:8080"


class _FakeCaptchaSession:
    def __init__(self, user_agent=PROVIDER_UA):
        self.calls = []
        self.user_agent = user_agent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, _url, **kwargs):
        self.calls.append(kwargs)
        response = MagicMock()
        response.status_code = 200
        if len(self.calls) == 1:
            response.json.return_value = {"errorId": 0, "taskId": "task-1"}
        else:
            response.json.return_value = {
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "gRecaptchaResponse": "captcha-token",
                    "userAgent": self.user_agent,
                },
            }
        return response


class _ExtensionRespondingWebSocket:
    def __init__(self):
        self.service = None
        self.response_websocket = self
        self.response_payload = {}

    async def send_text(self, data):
        request = json.loads(data)
        response = {
            "req_id": request["req_id"],
            "status": "success",
            "token": "extension-token",
            **self.response_payload,
        }
        await self.service.handle_message(self.response_websocket, json.dumps(response))
        if self.response_websocket is not self:
            await self.service.handle_message(
                self,
                json.dumps(
                    {
                        "req_id": request["req_id"],
                        "status": "success",
                        "token": "owner-token",
                        "user_agent": PROVIDER_UA,
                    }
                ),
            )


class ExtensionCaptchaMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def _solve(self, response_payload, *, response_websocket=None):
        service = ExtensionCaptchaService(db=None)
        websocket = _ExtensionRespondingWebSocket()
        websocket.service = service
        websocket.response_payload = response_payload
        if response_websocket is not None:
            websocket.response_websocket = response_websocket
        token, req_id = await service._extension_recaptcha_token_once(
            ExtensionConnection(websocket=websocket),
            project_id="project-1",
            action="IMAGE_GENERATION",
            route_key="",
            managed_api_key_id=1,
            timeout=1,
        )
        return service, token, req_id

    def test_extension_user_agent_normalization(self):
        self.assertEqual(normalize_extension_captcha_user_agent(f"  {PROVIDER_UA}  "), PROVIDER_UA)
        for value in (None, 123, "", "bad\rvalue", "bad\nvalue", "x" * 513):
            with self.subTest(value=repr(value)[:40]):
                self.assertIsNone(normalize_extension_captcha_user_agent(value))

    async def test_snake_and_legacy_camel_case_user_agents_are_consumed(self):
        for field_name in ("user_agent", "userAgent"):
            with self.subTest(field_name=field_name):
                service, token, req_id = await self._solve({field_name: f"  {PROVIDER_UA}  "})
                self.assertEqual(token, "extension-token")
                self.assertTrue(req_id.startswith("req_"))
                self.assertEqual(service.consume_token_user_agent(req_id), PROVIDER_UA)
                self.assertIsNone(service.consume_token_user_agent(req_id))

    async def test_missing_or_invalid_user_agent_keeps_token(self):
        for payload in (
            {},
            {"user_agent": "bad\r\nvalue"},
            {"user_agent": "x" * 513},
            {"user_agent": 123},
        ):
            with self.subTest(payload=repr(payload)[:60]):
                service, token, req_id = await self._solve(payload)
                self.assertEqual(token, "extension-token")
                self.assertIsNone(service.consume_token_user_agent(req_id))

    async def test_non_owner_websocket_cannot_supply_user_agent(self):
        service, token, req_id = await self._solve(
            {"user_agent": "Attacker-UA/1"},
            response_websocket=object(),
        )
        self.assertEqual(token, "owner-token")
        self.assertEqual(service.consume_token_user_agent(req_id), PROVIDER_UA)


class ApiCaptchaProviderResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_returns_token_and_user_agent_using_supplied_proxy(self):
        client = FlowClient(proxy_manager=_FakeProxyManager())
        session = _FakeCaptchaSession()
        with (
            patch("sub2gen.services.flow_client.AsyncSession", return_value=session),
            patch("sub2gen.services.flow_client.config") as captcha_config,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            captcha_config.yescaptcha_api_key = "test-key"
            captcha_config.yescaptcha_base_url = "https://api.yescaptcha.test"
            captcha_config.yescaptcha_task_type = "RecaptchaV3TaskProxylessM1S9"
            result = await client._get_api_captcha_token(
                "yescaptcha",
                "project-1",
                proxy_url="http://127.0.0.1:8080",
                proxy_resolved=True,
            )

        self.assertEqual(result, ("captcha-token", PROVIDER_UA))
        self.assertEqual(
            session.calls[0]["proxies"],
            {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"},
        )
        self.assertNotIn("userAgent", session.calls[0]["json"]["task"])


class ApiCaptchaFingerprintTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_method = config.captcha_method
        config.set_captcha_method("yescaptcha")

    async def asyncTearDown(self):
        config.set_captcha_method(self.previous_method)

    async def test_provider_user_agent_merges_with_exact_solver_proxy(self):
        proxy_manager = _FakeProxyManager()
        client = FlowClient(proxy_manager=proxy_manager)
        client._get_api_captcha_token = AsyncMock(return_value=("captcha-token", PROVIDER_UA))

        token, browser_id = await client._get_recaptcha_token("project-1")

        self.assertEqual((token, browser_id), ("captcha-token", None))
        self.assertEqual(proxy_manager.calls, 1)
        client._get_api_captcha_token.assert_awaited_once_with(
            "yescaptcha",
            "project-1",
            "IMAGE_GENERATION",
            proxy_url="http://127.0.0.1:8080",
            proxy_resolved=True,
        )
        fingerprint = client.get_request_fingerprint()
        self.assertEqual(fingerprint["proxy_url"], "http://127.0.0.1:8080")
        self.assertEqual(fingerprint["user_agent"], PROVIDER_UA)
        self.assertEqual(fingerprint["project_id"], "project-1")
        self.assertEqual(fingerprint["origin"], "https://labs.google")
        self.assertIn('"Google Chrome";v="147"', fingerprint["sec_ch_ua"])

    async def test_legacy_plain_token_and_missing_user_agent_remain_usable(self):
        client = FlowClient(proxy_manager=_FakeProxyManager())
        client._get_api_captcha_token = AsyncMock(return_value="legacy-token")

        token, _ = await client._get_recaptcha_token("project-1")

        self.assertEqual(token, "legacy-token")
        fingerprint = client.get_request_fingerprint()
        self.assertEqual(fingerprint["proxy_url"], "http://127.0.0.1:8080")
        self.assertEqual(fingerprint["project_id"], "project-1")
        self.assertTrue(fingerprint["user_agent"])

    async def test_failed_provider_result_clears_fingerprint(self):
        client = FlowClient(proxy_manager=_FakeProxyManager())
        client._get_api_captcha_token = AsyncMock(return_value=None)

        self.assertEqual(await client._get_recaptcha_token("project-1"), (None, None))
        self.assertIsNone(client.get_request_fingerprint())

    async def test_direct_solver_binding_blocks_later_rotating_proxy(self):
        proxy_manager = _RotatingProxyManager()
        client = FlowClient(proxy_manager=proxy_manager)
        client._get_api_captcha_token = AsyncMock(return_value=("captcha-token", PROVIDER_UA))
        await client._get_recaptcha_token("project-1")
        self.assertEqual(client.get_request_fingerprint()["proxy_url"], "")

        captured = {}
        response = SimpleNamespace(status_code=200, headers={}, text="{}", json=lambda: {})

        class FakeFlowSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _url, **kwargs):
                captured.update(kwargs)
                return response

        with patch("sub2gen.services.flow_client.AsyncSession", return_value=FakeFlowSession()):
            await client._make_request("POST", "https://aisandbox-pa.googleapis.com/v1/test", json_data={})
        self.assertIsNone(captured["proxy"])

    async def test_flow_request_uses_provider_user_agent_and_matching_client_hints(self):
        proxy_manager = _FakeProxyManager()
        client = FlowClient(proxy_manager=proxy_manager)
        client._get_api_captcha_token = AsyncMock(return_value=("captcha-token", PROVIDER_UA))
        await client._get_recaptcha_token("project-1")

        captured = {}
        response = SimpleNamespace(status_code=200, headers={}, text="{}", json=lambda: {})

        class FakeFlowSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _url, **kwargs):
                captured.update(kwargs)
                return response

        with patch("sub2gen.services.flow_client.AsyncSession", return_value=FakeFlowSession()):
            await client._make_request(
                "POST",
                "https://aisandbox-pa.googleapis.com/v1/test",
                json_data={"clientContext": {"projectId": "project-1"}},
            )

        headers = captured["headers"]
        self.assertEqual(headers["User-Agent"], PROVIDER_UA)
        self.assertIn('"Google Chrome";v="147"', headers["sec-ch-ua"])
        self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')
        self.assertEqual(captured["proxy"], "http://127.0.0.1:8080")

    async def test_extension_user_agent_builds_fingerprint_and_emits_set_ua(self):
        config.set_captcha_method("extension")
        service = SimpleNamespace(
            get_token=AsyncMock(return_value=("extension-token", "req_1")),
            consume_token_user_agent=lambda req_id: PROVIDER_UA if req_id == "req_1" else None,
        )
        events = []

        async def progress(payload):
            events.append(payload)

        client = FlowClient(proxy_manager=None)
        with patch.object(ExtensionCaptchaService, "get_instance", new=AsyncMock(return_value=service)):
            token, browser_id = await client._get_recaptcha_token(
                "project-extension",
                poll_task_progress=progress,
            )

        self.assertEqual((token, browser_id), ("extension-token", None))
        fingerprint = client.get_request_fingerprint()
        self.assertEqual(fingerprint["user_agent"], PROVIDER_UA)
        self.assertEqual(fingerprint["project_id"], "project-extension")
        self.assertEqual(fingerprint["origin"], "https://labs.google")
        self.assertIn("project-extension", fingerprint["referer"])
        self.assertNotIn("proxy_url", fingerprint)
        self.assertIn('"Google Chrome";v="147"', fingerprint["sec_ch_ua"])
        self.assertEqual(
            events,
            [{
                "captcha_status": "user_agent_set",
                "captcha_user_agent_set": True,
                "captcha_provider": "extension",
            }],
        )
        self.assertNotIn(PROVIDER_UA, str(events))

    async def test_token_only_extension_keeps_fallback_without_badge(self):
        config.set_captcha_method("extension")
        service = SimpleNamespace(
            get_token=AsyncMock(return_value=("legacy-token", "req_2")),
            consume_token_user_agent=lambda _req_id: None,
        )
        events = []

        async def progress(payload):
            events.append(payload)

        client = FlowClient(proxy_manager=None)
        with patch.object(ExtensionCaptchaService, "get_instance", new=AsyncMock(return_value=service)):
            token, _ = await client._get_recaptcha_token(
                "project-extension",
                poll_task_progress=progress,
            )

        self.assertEqual(token, "legacy-token")
        self.assertIsNone(client.get_request_fingerprint())
        self.assertEqual(events, [])

    async def test_extension_user_agent_reaches_native_request_headers(self):
        config.set_captcha_method("extension")
        service = SimpleNamespace(
            get_token=AsyncMock(return_value=("extension-token", "req_3")),
            consume_token_user_agent=lambda _req_id: PROVIDER_UA,
        )
        client = FlowClient(proxy_manager=None)
        with patch.object(ExtensionCaptchaService, "get_instance", new=AsyncMock(return_value=service)):
            await client._get_recaptcha_token("project-extension")

        captured = {}
        response = SimpleNamespace(status_code=200, headers={}, text="{}", json=lambda: {})

        class FakeFlowSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _url, **kwargs):
                captured.update(kwargs)
                return response

        with patch("sub2gen.services.flow_client.AsyncSession", return_value=FakeFlowSession()):
            await client._make_request(
                "POST",
                "https://aisandbox-pa.googleapis.com/v1/test",
                json_data={"clientContext": {"projectId": "project-extension"}},
            )

        self.assertEqual(captured["headers"]["User-Agent"], PROVIDER_UA)
        self.assertIn('"Google Chrome";v="147"', captured["headers"]["sec-ch-ua"])
        self.assertEqual(captured["headers"]["sec-ch-ua-platform"], '"Windows"')


if __name__ == "__main__":
    unittest.main()
