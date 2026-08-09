import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sub2gen.services.cache_backends.digitalocean import (
    DigitalOceanSpacesBackend,
    DigitalOceanSpacesSettings,
)
from sub2gen.services.cache_backends.local import LocalCacheBackend
from sub2gen.services.file_cache import FileCache


class LocalCacheBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_read_range_list_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalCacheBackend(Path(tmp))
            await backend.store_bytes("movie.mp4", b"0123456789", "video/mp4")
            self.assertEqual(await backend.read_bytes("movie.mp4"), b"0123456789")
            opened = await backend.open("movie.mp4", "bytes=2-5")
            data = b"".join([chunk async for chunk in opened.body])
            self.assertEqual(opened.status_code, 206)
            self.assertEqual(opened.content_range, "bytes 2-5/10")
            self.assertEqual(data, b"2345")
            self.assertEqual(len(await backend.list()), 1)
            self.assertEqual(await backend.clear(), (1, 10))


class _TransferConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.put_calls = []

    def head_bucket(self, **kwargs):
        return {}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[kwargs["Key"]] = bytes(kwargs["Body"])

    def head_object(self, **kwargs):
        data = self.objects[kwargs["Key"]]
        return {
            "ContentLength": len(data),
            "ContentType": "image/png",
            "LastModified": datetime.now(timezone.utc),
            "ETag": '"etag"',
        }

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(data),
                    "LastModified": datetime.now(timezone.utc),
                    "ETag": '"etag"',
                }
                for key, data in self.objects.items()
                if key.startswith(prefix)
            ],
            "IsTruncated": False,
        }


class DigitalOceanSpacesBackendTests(unittest.IsolatedAsyncioTestCase):
    def _modules(self, client):
        boto3 = types.ModuleType("boto3")
        boto3.client = lambda *args, **kwargs: client
        s3 = types.ModuleType("boto3.s3")
        transfer = types.ModuleType("boto3.s3.transfer")
        transfer.TransferConfig = _TransferConfig
        return {"boto3": boto3, "boto3.s3": s3, "boto3.s3.transfer": transfer}

    async def test_cdn_upload_uses_public_acl_and_builds_prefixed_url(self):
        client = _FakeS3Client()
        settings = DigitalOceanSpacesSettings(
            access_key_id="key",
            secret_access_key="secret",
            region="nyc3",
            bucket="bucket",
            prefix="sub2gen/cache",
            delivery_mode="cdn",
            cdn_base_url="https://cdn.example.com",
            api_token="token",
            cdn_endpoint_id="endpoint",
        )
        with patch.dict(sys.modules, self._modules(client)):
            backend = DigitalOceanSpacesBackend(settings)
            stored = await backend.store_bytes("image.png", b"png", "image/png")
        self.assertEqual(stored.key, "sub2gen/cache/image.png")
        self.assertEqual(client.put_calls[0]["ACL"], "public-read")
        self.assertEqual(
            backend.public_url("image.png"),
            "https://cdn.example.com/sub2gen/cache/image.png",
        )

    async def test_cdn_file_listing_includes_direct_public_url(self):
        client = _FakeS3Client()
        settings = DigitalOceanSpacesSettings(
            access_key_id="key",
            secret_access_key="secret",
            region="nyc3",
            bucket="bucket",
            prefix="sub2gen/cache",
            delivery_mode="cdn",
            cdn_base_url="https://cdn.example.com",
            api_token="token",
            cdn_endpoint_id="endpoint",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, self._modules(client)):
            backend = DigitalOceanSpacesBackend(settings)
            await backend.store_bytes("image #1.png", b"png", "image/png")
            cache = FileCache(cache_dir=tmp)
            cache.provider = "digitalocean"
            cache.delivery_mode = "cdn"
            cache.backend = backend
            files = await cache.list_files()

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["name"], "image #1.png")
        self.assertEqual(files[0]["kind"], "image")
        self.assertEqual(files[0]["provider"], "digitalocean")
        self.assertEqual(files[0]["delivery_mode"], "cdn")
        self.assertEqual(
            files[0]["public_url"],
            "https://cdn.example.com/sub2gen/cache/image%20%231.png",
        )

    async def test_local_file_listing_has_no_public_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp)
            await cache.backend.store_bytes("image.png", b"png", "image/png")
            files = await cache.list_files()

        self.assertEqual(len(files), 1)
        self.assertIsNone(files[0]["public_url"])

    async def test_spaces_proxy_file_listing_has_no_public_url(self):
        client = _FakeS3Client()
        settings = DigitalOceanSpacesSettings(
            access_key_id="key",
            secret_access_key="secret",
            region="nyc3",
            bucket="bucket",
            prefix="sub2gen/cache",
            delivery_mode="proxy",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, self._modules(client)):
            backend = DigitalOceanSpacesBackend(settings)
            await backend.store_bytes("image.png", b"png", "image/png")
            cache = FileCache(cache_dir=tmp)
            cache.provider = "digitalocean"
            cache.delivery_mode = "proxy"
            cache.backend = backend
            files = await cache.list_files()

        self.assertEqual(len(files), 1)
        self.assertIsNone(files[0]["public_url"])

    def test_missing_cdn_purge_configuration_is_rejected(self):
        settings = DigitalOceanSpacesSettings(
            access_key_id="key",
            secret_access_key="secret",
            region="nyc3",
            bucket="bucket",
            delivery_mode="cdn",
            cdn_base_url="https://cdn.example.com",
        )
        self.assertIn("SUB2GEN_DO_API_TOKEN", settings.missing())
        self.assertIn("SUB2GEN_DO_CDN_ENDPOINT_ID", settings.missing())

    async def test_spaces_promotion_failure_does_not_leave_local_fallback(self):
        class FailingBackend:
            async def store_file(self, *args, **kwargs):
                raise RuntimeError("upload failed")

        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp)
            cache.provider = "digitalocean"
            cache.backend = FailingBackend()
            Path(tmp, "image.png").write_bytes(b"staged")
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                await cache._record_cache_metadata(
                    filename="image.png",
                    api_key_id=None,
                    token_id=None,
                    media_type="image",
                    source_url=None,
                )
            self.assertFalse(Path(tmp, "image.png").exists())


if __name__ == "__main__":
    unittest.main()
