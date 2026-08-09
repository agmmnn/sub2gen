import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sub2gen.core.config import config
from sub2gen.services.flow_client import FlowClient


JPEG_BYTES = b"\xff\xd8\xff" + b"0" * 16


class FlowClientUploadImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_scoped_upload_uses_new_endpoint_with_project_id(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            return {
                "media": {
                    "name": "new-media-id",
                }
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "new-media-id")
        self.assertEqual(len(request_calls), 1)
        self.assertTrue(request_calls[0]["url"].endswith("/flow/uploadImage"))
        self.assertEqual(
            request_calls[0]["json_data"]["clientContext"]["projectId"],
            "project-123",
        )
        self.assertIn("sessionId", request_calls[0]["json_data"]["clientContext"])

    async def test_project_scoped_upload_accepts_media_list_response(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            return {
                "media": [
                    {
                        "name": "new-media-id",
                        "projectId": "project-123",
                    }
                ]
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "new-media-id")
        self.assertEqual(len(request_calls), 1)
        self.assertTrue(request_calls[0]["url"].endswith("/flow/uploadImage"))

    async def test_project_scoped_upload_does_not_fallback_to_legacy_endpoint(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if kwargs["url"].endswith("/flow/uploadImage"):
                raise RuntimeError("HTTP 500: upstream failed")
            self.fail("带 project_id 的上传不应回退到 legacy 接口")

        client._make_request = AsyncMock(side_effect=fake_make_request)

        with self.assertRaisesRegex(RuntimeError, "legacy :uploadUserImage fallback is disabled"):
            await client.upload_image(
                at="test-at",
                image_bytes=JPEG_BYTES,
                aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
                project_id="project-123",
            )

        self.assertEqual(len(request_calls), 1)
        self.assertEqual(
            request_calls[0]["json_data"]["clientContext"]["projectId"],
            "project-123",
        )

    async def test_upload_without_project_id_keeps_legacy_fallback(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if kwargs["url"].endswith("/flow/uploadImage"):
                raise RuntimeError("HTTP 500: upstream failed")
            if kwargs["url"].endswith(":uploadUserImage"):
                return {
                    "mediaGenerationId": {
                        "mediaGenerationId": "legacy-media-id",
                    }
                }
            self.fail(f"Unexpected url: {kwargs['url']}")

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id=None,
        )

        self.assertEqual(media_id, "legacy-media-id")
        self.assertEqual(len(request_calls), 2)
        self.assertNotIn(
            "projectId",
            request_calls[1]["json_data"]["clientContext"],
        )


class FlowClientBrowserIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_browser_user_agent_uses_identity_api_and_cache(self):
        client = FlowClient(proxy_manager=None)
        client._get_personal_browser_identity = AsyncMock(
            return_value=(None, "browser-runtime-ua")
        )

        self.assertEqual(await client._generate_real_browser_user_agent(), "browser-runtime-ua")
        self.assertEqual(await client._generate_real_browser_user_agent(), "browser-runtime-ua")
        client._get_personal_browser_identity.assert_awaited_once()

    async def test_personal_request_reuses_complete_browser_fingerprint(self):
        client = FlowClient(proxy_manager=None)
        browser_fingerprint = {
            "user_agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36",
            "accept_language": "en-US,en;q=0.9",
            "sec_ch_ua": '"Chromium";v="124"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Linux"',
        }
        client._get_personal_browser_identity = AsyncMock(
            return_value=(browser_fingerprint, browser_fingerprint["user_agent"])
        )

        captured = {}
        response = SimpleNamespace(status_code=200, headers={}, text="{}", json=lambda: {})
        session = SimpleNamespace()

        async def post(_url, **kwargs):
            captured.update(kwargs)
            return response

        session.post = post

        class FakeAsyncSession:
            async def __aenter__(self):
                return session

            async def __aexit__(self, exc_type, exc, tb):
                return None

        previous_method = config.captcha_method
        try:
            config.set_captcha_method("personal")
            with patch("sub2gen.services.flow_client.AsyncSession", return_value=FakeAsyncSession()):
                await client._make_request("POST", "https://example.test/api", json_data={})
        finally:
            config.set_captcha_method(previous_method)

        headers = captured["headers"]
        self.assertEqual(headers["User-Agent"], browser_fingerprint["user_agent"])
        self.assertEqual(headers["Accept-Language"], browser_fingerprint["accept_language"])
        self.assertEqual(
            headers["sec-ch-ua"],
            '"Google Chrome";v="124", "Chromium";v="124", "Not)A;Brand";v="24"',
        )
        self.assertEqual(headers["sec-ch-ua-mobile"], "?0")
        self.assertEqual(headers["sec-ch-ua-platform"], '"Linux"')
        client._get_personal_browser_identity.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
