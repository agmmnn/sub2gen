import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sub2gen.services.file_cache import FileCache
from sub2gen.services.flow_client import FlowClient
from sub2gen.services.generation_handler import GenerationHandler


def _valid_mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypisom" + (b"\x00" * 2048)


class FlowGetMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_media_uses_at_endpoint_and_current_headers(self):
        client = FlowClient(proxy_manager=None)
        client._make_request = AsyncMock(return_value={"video": {"encodedVideo": "data"}})

        result = await client.get_media("access-token", "media/name")

        self.assertIn("encodedVideo", result["video"])
        kwargs = client._make_request.await_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertTrue(kwargs["url"].endswith("/media/media%2Fname"))
        self.assertEqual(kwargs["at_token"], "access-token")
        self.assertEqual(kwargs["headers"]["Origin"], "https://labs.google")

    async def test_get_media_rejects_missing_credentials_or_name(self):
        client = FlowClient(proxy_manager=None)
        with self.assertRaises(ValueError):
            await client.get_media("", "media-1")
        with self.assertRaises(ValueError):
            await client.get_media("access-token", "")


class Base64VideoCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_mp4_is_validated_and_cached_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp)
            cache.ensure_cache_capacity = AsyncMock(return_value={})
            filename = await cache.cache_base64_video(
                base64.b64encode(_valid_mp4_bytes()).decode(),
                api_key_id=3,
                token_id=7,
                flow_project_id="project-1",
                source_media_name="media-1",
            )
            path = Path(tmp) / filename
            self.assertEqual(path.suffix, ".mp4")
            self.assertEqual(path.read_bytes(), _valid_mp4_bytes())
            self.assertFalse(path.with_suffix(".mp4.part").exists())

    async def test_invalid_or_non_video_base64_is_rejected_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp)
            cache.ensure_cache_capacity = AsyncMock(return_value={})
            with self.assertRaisesRegex(Exception, "Failed to cache base64 video"):
                await cache.cache_base64_video("not-base64")
            with self.assertRaisesRegex(Exception, "not valid media"):
                await cache.cache_base64_video(base64.b64encode(b"plain text" * 200).decode())
            self.assertEqual(list(Path(tmp).glob("*.mp4")), [])
            self.assertEqual(list(Path(tmp).glob("*.part")), [])


class GenerationMediaFallbackTests(unittest.IsolatedAsyncioTestCase):
    def _handler(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = MagicMock()
        handler.flow_client.get_media_url_redirect = AsyncMock(side_effect=RuntimeError("redirect unavailable"))
        handler.flow_client.get_media = AsyncMock()
        handler.file_cache = MagicMock()
        handler.file_cache.cache_base64_video = AsyncMock(return_value="encoded-video.mp4")
        return handler

    @staticmethod
    def _operation():
        return {
            "mediaName": "media-1",
            "operation": {
                "name": "media-1",
                "metadata": {"video": {}},
            },
        }

    async def test_missing_url_fetches_encoded_video_and_returns_owned_cache_url(self):
        handler = self._handler()
        encoded = base64.b64encode(_valid_mp4_bytes()).decode()
        handler.flow_client.get_media.return_value = {"video": {"encodedVideo": encoded}}
        token = SimpleNamespace(id=7, at="access-token", st="session-token")

        result = await handler._resolve_video_asset(
            token,
            self._operation(),
            api_key_id=3,
            project_id="project-1",
            response_state={"base_url": "https://flow.example"},
        )

        self.assertTrue(result["video_is_cached"])
        self.assertEqual(
            result["video_url"],
            "https://flow.example/api/cache/blob/encoded-video.mp4?project_id=project-1",
        )
        handler.flow_client.get_media.assert_awaited_once_with("access-token", "media-1")
        handler.file_cache.cache_base64_video.assert_awaited_once_with(
            encoded,
            api_key_id=3,
            token_id=7,
            flow_project_id="project-1",
            source_media_name="media-1",
        )

    async def test_existing_redirect_url_skips_base64_fetch(self):
        handler = self._handler()
        handler.flow_client.get_media_url_redirect = AsyncMock(
            return_value="https://flow-content.google/video/file.mp4"
        )
        token = SimpleNamespace(id=7, at="access-token", st="session-token")

        result = await handler._resolve_video_asset(token, self._operation())

        self.assertEqual(result["video_url"], "https://flow-content.google/video/file.mp4")
        self.assertFalse(result["video_is_cached"])
        handler.flow_client.get_media.assert_not_awaited()

    async def test_empty_encoded_video_is_reported_without_false_success(self):
        handler = self._handler()
        handler.flow_client.get_media.return_value = {"video": {}}
        token = SimpleNamespace(id=7, at="access-token", st="session-token")

        result = await handler._resolve_video_asset(token, self._operation())

        self.assertEqual(result["video_url"], "")
        self.assertFalse(result["video_is_cached"])
        self.assertIn("empty encodedVideo", result["media_fetch_error"])
        handler.file_cache.cache_base64_video.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
