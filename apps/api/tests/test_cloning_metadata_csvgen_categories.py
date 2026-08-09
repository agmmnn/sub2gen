"""CSVGEN metadata: Adobe categories injection in outbound settings."""

import base64
import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from sub2gen.api.routes import extension_metadata_session
from sub2gen.core.api_key_manager import ApiKeyManager, AuthContext
from sub2gen.core.models import MetadataSettingsRequest
from sub2gen.core.route_log_sanitize import dumps_for_request_log, sanitize_for_request_log
from sub2gen.services import cloning_metadata_service as cms


class TestRouteLogImagePreviewSanitize(unittest.TestCase):
    def test_image_base64_is_redacted_but_preview_is_available(self):
        payload = {
            "image_base64": "iVBORw0KGgo=",
            "mimeType": "image/png",
            "metadataSettings": {"transparentBackground": True},
        }

        sanitized = sanitize_for_request_log(payload)

        self.assertEqual(sanitized["image_base64"], "<redacted>")
        self.assertEqual(sanitized["imagePreview"]["source"], "image_base64")
        self.assertEqual(sanitized["imagePreview"]["mimeType"], "image/png")
        self.assertEqual(sanitized["imagePreview"]["dataUrl"], "data:image/png;base64,iVBORw0KGgo=")
        self.assertEqual(sanitized["imagePreview"]["base64Length"], len("iVBORw0KGgo="))

    def test_image_data_url_uses_embedded_mime_for_preview(self):
        payload = {
            "image_base64": "data:image/webp;base64, AAAA ",
            "metadataSettings": {},
        }

        sanitized = sanitize_for_request_log(payload)

        self.assertEqual(sanitized["image_base64"], "<redacted>")
        self.assertEqual(sanitized["imagePreview"]["mimeType"], "image/webp")
        self.assertEqual(sanitized["imagePreview"]["dataUrl"], "data:image/webp;base64,AAAA")

    def test_image_url_gets_preview_without_exposing_sensitive_fields(self):
        payload = {"image_url": "https://example.test/image.png", "access_token": "secret"}

        dumped = json.loads(dumps_for_request_log(payload))

        self.assertEqual(dumped["access_token"], "<redacted>")
        self.assertEqual(dumped["imagePreview"], {"source": "image_url", "url": "https://example.test/image.png"})

    def test_non_image_sensitive_fields_remain_redacted(self):
        payload = {"embedding": [1, 2, 3], "session_token": "abc", "text": "ok"}

        sanitized = sanitize_for_request_log(payload)

        self.assertEqual(sanitized["embedding"], "<redacted>")
        self.assertEqual(sanitized["session_token"], "<redacted>")
        self.assertEqual(sanitized["text"], "ok")


class TestAdobeCategoriesForCsvgenSettings(unittest.TestCase):
    def test_helper_shape_and_length(self):
        rows = cms._adobe_stock_categories_for_csvgen_settings()
        self.assertEqual(len(rows), 21)
        self.assertEqual(rows[0], {"id": 1, "name": "Animals"})
        self.assertEqual(rows[-1], {"id": 21, "name": "Travel"})


class TestCsvgenPostIncludesCategories(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._captured_json = None

        async def fake_post(*_a, **kw):
            self._captured_json = kw.get("json")
            m = MagicMock()
            m.status_code = 200
            m.text = '{"title":"t","keywords":"k","description":""}'
            return m

        inner = MagicMock()
        inner.post = AsyncMock(side_effect=fake_post)
        outer = MagicMock()
        outer.__aenter__ = AsyncMock(return_value=inner)
        outer.__aexit__ = AsyncMock(return_value=None)
        self._session_cm = outer

        self._cfg = SimpleNamespace(
            sub2gen_metadata_backend="csvgen",
            sub2gen_metadata_provider_order="",
            sub2gen_metadata_enabled_providers="",
            sub2gen_metadata_provider_retry_count=1,
            sub2gen_metadata_primary_model="",
            sub2gen_metadata_model="gemini-2.5-flash",
            sub2gen_metadata_enabled_models="",
            sub2gen_metadata_fallback_models="",
            sub2gen_csvgen_cookie="session=fake",
            sub2gen_csvgen_api_keys="",
        )

    async def test_categories_present_when_include_category_and_adobe(self):
        async def fake_fetch(_self, _url, _b64, *_args, **_kwargs):
            return b"\xff\xd8\xff", "image/jpeg"

        with (
            patch.object(cms.CloningMetadataService, "_fetch_image", fake_fetch),
            patch.object(cms, "app_config", self._cfg),
            patch.object(cms, "AsyncSession", return_value=self._session_cm),
        ):
            svc = cms.CloningMetadataService(llm_chain=MagicMock())
            await svc.generate_metadata(
                {
                    "image_base64": "AAAA",
                    "metadataSettings": {
                        "platforms": ["adobe-stock"],
                        "includeCategory": True,
                    },
                }
            )

        settings = self._captured_json["settings"]
        cats = settings.get("categories")
        self.assertIsNotNone(cats)
        self.assertEqual(len(cats), 21)
        self.assertEqual(cats[0], {"id": 1, "name": "Animals"})

    async def test_categories_absent_without_include_category(self):
        async def fake_fetch(_self, _url, _b64, *_args, **_kwargs):
            return b"\xff\xd8\xff", "image/jpeg"

        with (
            patch.object(cms.CloningMetadataService, "_fetch_image", fake_fetch),
            patch.object(cms, "app_config", self._cfg),
            patch.object(cms, "AsyncSession", return_value=self._session_cm),
        ):
            svc = cms.CloningMetadataService(llm_chain=MagicMock())
            await svc.generate_metadata(
                {
                    "image_base64": "AAAA",
                    "metadataSettings": {
                        "platforms": ["adobe-stock"],
                        "includeCategory": False,
                    },
                }
            )

        settings = self._captured_json["settings"]
        self.assertNotIn("categories", settings)

    async def test_categories_absent_without_adobe_platform(self):
        async def fake_fetch(_self, _url, _b64, *_args, **_kwargs):
            return b"\xff\xd8\xff", "image/jpeg"

        with (
            patch.object(cms.CloningMetadataService, "_fetch_image", fake_fetch),
            patch.object(cms, "app_config", self._cfg),
            patch.object(cms, "AsyncSession", return_value=self._session_cm),
        ):
            svc = cms.CloningMetadataService(llm_chain=MagicMock())
            await svc.generate_metadata(
                {
                    "image_base64": "AAAA",
                    "metadataSettings": {
                        "platforms": ["shutterstock"],
                        "includeCategory": True,
                    },
                }
            )

        settings = self._captured_json["settings"]
        self.assertNotIn("categories", settings)

    async def test_adobe_stock_platform_matching_is_case_insensitive(self):
        async def fake_fetch(_self, _url, _b64, *_args, **_kwargs):
            return b"\xff\xd8\xff", "image/jpeg"

        with (
            patch.object(cms.CloningMetadataService, "_fetch_image", fake_fetch),
            patch.object(cms, "app_config", self._cfg),
            patch.object(cms, "AsyncSession", return_value=self._session_cm),
        ):
            svc = cms.CloningMetadataService(llm_chain=MagicMock())
            await svc.generate_metadata(
                {
                    "image_base64": "AAAA",
                    "metadataSettings": {
                        "platforms": ["Adobe-Stock"],
                        "includeCategory": True,
                    },
                }
            )

        settings = self._captured_json["settings"]
        self.assertIn("categories", settings)
        self.assertEqual(len(settings["categories"]), 21)


class TestMetadataImageMimeHandling(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_image_detects_png_data_url(self):
        png_bytes = b"\x89PNG\r\n\x1a\nfake"
        data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
        svc = cms.CloningMetadataService(llm_chain=MagicMock())

        content, mime = await svc._fetch_image(None, data_url)

        self.assertEqual(content, png_bytes)
        self.assertEqual(mime, "image/png")

    async def test_fetch_image_honors_explicit_mime_for_raw_base64(self):
        payload = base64.b64encode(b"not-magic-but-webp-by-hint").decode("ascii")
        svc = cms.CloningMetadataService(llm_chain=MagicMock())

        _content, mime = await svc._fetch_image(None, payload, mime_type_hint="image/webp")

        self.assertEqual(mime, "image/webp")

    async def test_generate_metadata_passes_png_mime_to_llm(self):
        png_bytes = b"\x89PNG\r\n\x1a\nfake"
        llm = MagicMock()
        llm.resolve_provider_chain.return_value = ["gemini_native"]
        llm.invoke_model_json = AsyncMock(
            return_value={"metadataSets": [{"title": "Yellow tray cutout", "keywords": ["tray"], "description": ""}]}
        )
        cfg = SimpleNamespace(
            sub2gen_metadata_backend="gemini_native",
            sub2gen_metadata_provider_order="",
            sub2gen_metadata_enabled_providers="",
            sub2gen_metadata_provider_retry_count=0,
            sub2gen_metadata_primary_model="gemini-2.5-flash",
            sub2gen_metadata_model="gemini-2.5-flash",
            sub2gen_metadata_enabled_models="",
            sub2gen_metadata_fallback_models="",
        )
        with patch.object(cms, "app_config", cfg):
            svc = cms.CloningMetadataService(llm_chain=llm)
            await svc.generate_metadata(
                {
                    "image_base64": base64.b64encode(png_bytes).decode("ascii"),
                    "mimeType": "image/png",
                    "metadataSettings": {"transparentBackground": True},
                    "dnaNoBgWorkflowActive": True,
                }
            )

        self.assertEqual(llm.invoke_model_json.await_args.kwargs["mime_type"], "image/png")
        prompt = llm.invoke_model_json.await_args.kwargs["prompt_text"]
        self.assertIn("isolated on transparent background", prompt)
        self.assertIn("Do not say solid black background", prompt)
        self.assertIn("Transparent alpha may appear black, dark, white, gray, or checkerboard", prompt)

    async def test_transparent_background_prompt_without_dna_no_bg_workflow(self):
        png_bytes = b"\x89PNG\r\n\x1a\nfake"
        llm = MagicMock()
        llm.resolve_provider_chain.return_value = ["gemini_native"]
        llm.invoke_model_json = AsyncMock(
            return_value={"metadataSets": [{"title": "Yellow tray cutout", "keywords": ["tray"], "description": ""}]}
        )
        cfg = SimpleNamespace(
            sub2gen_metadata_backend="gemini_native",
            sub2gen_metadata_provider_order="",
            sub2gen_metadata_enabled_providers="",
            sub2gen_metadata_provider_retry_count=0,
            sub2gen_metadata_primary_model="gemini-2.5-flash",
            sub2gen_metadata_model="gemini-2.5-flash",
            sub2gen_metadata_enabled_models="",
            sub2gen_metadata_fallback_models="",
        )
        with patch.object(cms, "app_config", cfg):
            svc = cms.CloningMetadataService(llm_chain=llm)
            await svc.generate_metadata(
                {
                    "image_base64": base64.b64encode(png_bytes).decode("ascii"),
                    "mimeType": "image/png",
                    "metadataSettings": {"transparentBackground": True},
                    "dnaNoBgWorkflowActive": False,
                }
            )

        prompt = llm.invoke_model_json.await_args.kwargs["prompt_text"]
        self.assertIn("isolated on transparent background", prompt)
        self.assertIn("Do not say solid black background", prompt)
        self.assertIn("Transparent alpha may appear black, dark, white, gray, or checkerboard", prompt)


class TestMetadataProviderRouting(unittest.IsolatedAsyncioTestCase):
    def _cfg(self):
        return SimpleNamespace(
            sub2gen_metadata_backend="gemini_native",
            sub2gen_metadata_provider_order="openrouter,cloudflare,gemini_native",
            sub2gen_metadata_enabled_providers="openrouter",
            sub2gen_metadata_provider_retry_count=0,
            sub2gen_metadata_primary_model="openrouter/auto",
            sub2gen_metadata_model="openrouter/auto",
            sub2gen_metadata_enabled_models="",
            sub2gen_metadata_fallback_models="",
        )

    async def test_without_explicit_backend_uses_enabled_provider_order(self):
        llm = MagicMock()
        llm.resolve_provider_chain.side_effect = cms.LlmProviderChain.resolve_provider_chain
        llm.invoke_model_json = AsyncMock(
            return_value={"metadataSets": [{"title": "Yellow tray cutout", "keywords": ["tray"], "description": ""}]}
        )

        with patch.object(cms, "app_config", self._cfg()):
            svc = cms.CloningMetadataService(llm_chain=llm)
            await svc.generate_metadata(
                {
                    "image_base64": base64.b64encode(b"\xff\xd8\xff").decode("ascii"),
                    "mimeType": "image/jpeg",
                    "metadataSettings": {"transparentBackground": True},
                }
            )

        self.assertEqual(llm.invoke_model_json.await_args.kwargs["provider"], "openrouter")

    async def test_explicit_backend_still_overrides_provider_order(self):
        llm = MagicMock()
        llm.resolve_provider_chain.side_effect = cms.LlmProviderChain.resolve_provider_chain
        llm.invoke_model_json = AsyncMock(
            return_value={"metadataSets": [{"title": "Yellow tray cutout", "keywords": ["tray"], "description": ""}]}
        )

        with patch.object(cms, "app_config", self._cfg()):
            svc = cms.CloningMetadataService(llm_chain=llm)
            await svc.generate_metadata(
                {
                    "backend": "cloudflare",
                    "image_base64": base64.b64encode(b"\xff\xd8\xff").decode("ascii"),
                    "mimeType": "image/jpeg",
                    "metadataSettings": {"transparentBackground": True},
                }
            )

        self.assertEqual(llm.invoke_model_json.await_args.kwargs["provider"], "cloudflare")


class TestSub2GenMetadataExtensionContract(unittest.TestCase):
    def test_metadata_scoped_managed_key_activates(self):
        context = AuthContext(7, "stock-team", False, set(), {"adobe:metadata"})
        result = asyncio.run(extension_metadata_session(context))
        self.assertEqual(result["service"], "sub2gen-metadata")
        self.assertEqual(result["keyLabel"], "stock-team")
        self.assertEqual(result["capabilities"], ["adobe:metadata"])

    def test_legacy_and_wrong_scope_keys_are_rejected(self):
        with self.assertRaises(HTTPException) as legacy_error:
            asyncio.run(extension_metadata_session(AuthContext(None, "legacy", True, set(), {"*"})))
        self.assertEqual(legacy_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as scope_error:
            asyncio.run(extension_metadata_session(AuthContext(8, "video", False, set(), {"generate:video"})))
        self.assertEqual(scope_error.exception.detail, "Missing scope: adobe:metadata")

    def test_language_and_asset_type_are_validated_and_used(self):
        settings = MetadataSettingsRequest(language="ja", assetType="illustration")
        self.assertEqual(settings.language, "ja")
        with self.assertRaises(ValidationError):
            MetadataSettingsRequest(language="xx")

        prompt = cms.CloningMetadataService()._build_metadata_prompt(
            {"language": "de", "assetType": "vector illustration"}, False
        )
        self.assertIn("strictly in German", prompt)
        self.assertIn("Adobe asset type: vector illustration", prompt)


class _Sub2GenMetadataAuthDatabase:
    def __init__(self, row):
        self.row = row

    async def get_client_api_key_by_hash(self, _key_hash):
        return self.row

    async def get_api_key_account_ids(self, _key_id):
        return []

    async def get_api_key_rate_limits(self, _key_id, _endpoint):
        return None

    async def touch_api_key_usage(self, _key_id):
        return None


class TestSub2GenMetadataAuthentication(unittest.TestCase):
    def test_missing_invalid_disabled_and_expired_keys_are_rejected(self):
        endpoint = "/api/extension/metadata-session"
        manager = ApiKeyManager(_Sub2GenMetadataAuthDatabase(None), lambda: "")
        with self.assertRaisesRegex(PermissionError, "Missing API key"):
            asyncio.run(manager.authenticate(None, endpoint=endpoint))
        with self.assertRaisesRegex(PermissionError, "Invalid API key"):
            asyncio.run(manager.authenticate("wrong", endpoint=endpoint))

        disabled = ApiKeyManager(
            _Sub2GenMetadataAuthDatabase({"id": 4, "is_active": False, "scopes": "adobe:metadata"}), lambda: ""
        )
        with self.assertRaisesRegex(PermissionError, "disabled"):
            asyncio.run(disabled.authenticate("s2g_live_disabled", endpoint=endpoint))

        expired = ApiKeyManager(
            _Sub2GenMetadataAuthDatabase({
                "id": 4,
                "is_active": True,
                "scopes": "adobe:metadata",
                "expires_at": "past",
                "expires_unix": int(time.time()) - 1,
            }),
            lambda: "",
        )
        with self.assertRaisesRegex(PermissionError, "expired"):
            asyncio.run(expired.authenticate("s2g_live_expired", endpoint=endpoint))


if __name__ == "__main__":
    unittest.main()
