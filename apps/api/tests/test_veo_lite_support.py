import base64
import json
import os
import sqlite3
import tempfile
import time
import types
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from fastapi import HTTPException
from PIL import Image

import sub2gen.api.routes as routes
from sub2gen.api.admin import _extract_log_job_id
from sub2gen.api.routes import _extract_async_delivery_fields
from sub2gen.core.account_tiers import (
    PAYGATE_TIER_ONE,
    PAYGATE_TIER_TWO,
    is_native_4k_model,
)
from sub2gen.core.config import config
from sub2gen.core.logger import debug_logger
from sub2gen.core.model_resolver import get_base_model_aliases, resolve_model_name
from sub2gen.core.models import (
    ChatCompletionRequest,
    ChatMessage,
    GeminiContent,
    GeminiFileData,
    GeminiGenerateContentRequest,
    GeminiInlineData,
    GeminiPart,
)
from sub2gen.services.file_cache import FileCache
from sub2gen.services.flow_client import FlowClient
from sub2gen.services.generation_handler import MODEL_CONFIG, GenerationHandler, _needs_video_url_resolve


def fake_mp4_bytes(size: int = 2048) -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * max(0, size - 12)


def make_image_bytes(
    size: tuple[int, int],
    *,
    orientation: int | None = None,
) -> bytes:
    image = Image.new("RGB", size, color="white")
    buffer = BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


class FlowClientTransportErrorTests(unittest.TestCase):
    def setUp(self):
        self.client = FlowClient(proxy_manager=None)

    def test_curl_http2_error_uses_urllib_fallback(self):
        self.assertTrue(
            self.client._should_fallback_to_urllib("Failed to perform, curl: (16) .")
        )
        self.assertTrue(
            self.client._is_http2_transport_error("Failed to perform, curl: (16) .")
        )

    def test_curl_http2_error_is_retryable_network_error(self):
        error = "Flow API request failed: Failed to perform, curl: (16) ."

        self.assertTrue(self.client._is_retryable_network_error(error))
        self.assertIn("TLS", self.client._get_retry_reason(error))

    def test_http2_framing_text_is_retryable_network_error(self):
        error = "CURLE_HTTP2: problem detected in the HTTP/2 framing layer"

        self.assertTrue(self.client._is_http2_transport_error(error))
        self.assertTrue(self.client._should_fallback_to_urllib(error))
        self.assertTrue(self.client._is_retryable_network_error(error))


class AsyncImageDeliveryFieldTests(unittest.TestCase):
    def test_failed_upscale_suppresses_cached_1k_fallback(self):
        google_url = "https://flow-content.google/image/source"
        cached_url = "https://api.example.com/api/cache/blob/fallback.jpg"
        payload = {
            "url": cached_url,
            "generated_assets": {
                "type": "image",
                "origin_image_url": google_url,
                "final_image_url": cached_url,
            },
        }

        fields = _extract_async_delivery_fields(
            payload,
            "gemini-3.1-flash-image-landscape-4k",
        )

        self.assertEqual(fields["base_result_urls"], [google_url])
        self.assertIsNone(fields["result_urls"])
        self.assertIsNone(fields["delivery_urls"])
        self.assertEqual(fields["requested_resolution"], "4k")
        self.assertEqual(fields["output_resolution"], "1k")
        self.assertEqual(fields["upscale_status"], "failed")
        self.assertEqual(fields["upscale_error_message"], "Requested image upscale did not complete")

    def test_successful_upscale_delivers_upscaled_url(self):
        google_url = "https://flow-content.google/image/source"
        upscaled_url = "https://api.example.com/api/cache/blob/result_4K.jpg"
        payload = {
            "url": upscaled_url,
            "generated_assets": {
                "type": "image",
                "origin_image_url": google_url,
                "upscaled_image": {
                    "resolution": "4K",
                    "url": upscaled_url,
                },
            },
        }

        fields = _extract_async_delivery_fields(
            payload,
            "gemini-3.1-flash-image-landscape-4k",
        )

        self.assertEqual(fields["base_result_urls"], [google_url])
        self.assertEqual(fields["result_urls"], [upscaled_url])
        self.assertEqual(fields["delivery_urls"], [upscaled_url])
        self.assertEqual(fields["output_resolution"], "4k")
        self.assertEqual(fields["upscale_status"], "completed")

    def test_normal_1k_image_keeps_cached_final_url(self):
        google_url = "https://flow-content.google/image/source"
        cached_url = "https://api.example.com/api/cache/blob/fallback.jpg"
        payload = {
            "url": cached_url,
            "generated_assets": {
                "type": "image",
                "origin_image_url": google_url,
                "final_image_url": cached_url,
            },
        }

        fields = _extract_async_delivery_fields(
            payload,
            "gemini-3.1-flash-image-landscape",
        )

        self.assertEqual(fields["base_result_urls"], [google_url])
        self.assertEqual(fields["result_urls"], [cached_url])
        self.assertEqual(fields["delivery_urls"], [cached_url])
        self.assertIsNone(fields["requested_resolution"])
        self.assertIsNone(fields["output_resolution"])
        self.assertEqual(fields["upscale_status"], "not_requested")


class AsyncImageJobFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_payload_fails_requested_upscale_job(self):
        updates = {}

        class FakeDb:
            async def update_task(self, task_id, **kwargs):
                updates["task_id"] = task_id
                updates.update(kwargs)

        handler = SimpleNamespace(db=FakeDb())
        normalized = SimpleNamespace(
            model="gemini-3.1-flash-image-landscape-4k",
            project_id="project-1",
        )
        payload = {
            "url": "https://api.example.com/api/cache/blob/fallback.jpg",
            "generated_assets": {
                "type": "image",
                "origin_image_url": "https://flow-content.google/image/source",
                "final_image_url": "https://api.example.com/api/cache/blob/fallback.jpg",
            },
        }

        with patch.object(
            routes,
            "_collect_non_stream_result",
            AsyncMock(return_value=json.dumps(payload)),
        ):
            await routes._run_async_generation_task(
                task_id="job-1",
                normalized=normalized,
                base_url_override=None,
                allowed_token_ids=None,
                selection_context=None,
                api_key_id=1,
                handler=handler,
            )

        self.assertEqual(updates["task_id"], "job-1")
        self.assertEqual(updates["status"], "failed")
        self.assertEqual(updates["job_phase"], "failed")
        self.assertEqual(updates["upscale_status"], "failed")
        self.assertEqual(updates["captcha_status"], "idle")
        self.assertEqual(updates["result_urls"], [])
        self.assertEqual(updates["delivery_urls"], [])


class ImageUpscaleFailureHandlerTests(unittest.IsolatedAsyncioTestCase):
    def _build_handler(self, upsample_result=None, upsample_error=None, cache_error=None):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = SimpleNamespace(
            generate_image=AsyncMock(
                return_value=(
                    {
                        "media": [
                            {
                                "name": "media-1",
                                "image": {
                                    "generatedImage": {
                                        "fifeUrl": "https://flow-content.google/image/source"
                                    }
                                },
                            }
                        ]
                    },
                    "session-1",
                    {},
                )
            ),
            upsample_image=AsyncMock(side_effect=upsample_error)
            if upsample_error
            else AsyncMock(return_value=upsample_result),
            _get_retry_reason=lambda _err: None,
        )
        handler.file_cache = SimpleNamespace(
            cache_base64_image=AsyncMock(side_effect=cache_error)
            if cache_error
            else AsyncMock(return_value="upscaled_4K.jpg"),
            download_and_cache=AsyncMock(return_value="fallback.jpg"),
        )
        handler._update_request_log_progress = AsyncMock()
        handler._maybe_update_poll_task = AsyncMock()

        async def execute_with_extension_fallback(_name, _extension_operation, local_operation):
            return await local_operation()

        handler._execute_with_extension_fallback = execute_with_extension_fallback
        return handler

    async def _run_image_generation(self, handler, *, cache_enabled=True):
        token = SimpleNamespace(
            id=7,
            at="at-token",
            image_concurrency=1,
            user_paygate_tier="PAYGATE_TIER_ONE",
        )
        generation_result = handler._create_generation_result()
        response_state = handler._create_response_state()
        chunks = []
        original_cache_enabled = config.cache_enabled
        try:
            config.set_cache_enabled(cache_enabled)
            async for chunk in handler._handle_image_generation(
                token=token,
                project_id="project-1",
                model_config=MODEL_CONFIG["gemini-3.1-flash-image-landscape-4k"],
                prompt="prompt",
                images=None,
                stream=False,
                api_key_id=1,
                perf_trace={},
                generation_result=generation_result,
                response_state=response_state,
                request_log_state={"id": None, "progress": 0, "api_key_id": 1},
                pending_token_state={"active": False},
                poll_task_id="job-1",
            ):
                chunks.append(json.loads(chunk))
        finally:
            config.set_cache_enabled(original_cache_enabled)
        return generation_result, chunks, handler

    async def test_upscale_exception_fails_without_1k_fallback(self):
        handler = self._build_handler(upsample_error=Exception("upstream boom"))

        generation_result, chunks, handler = await self._run_image_generation(handler)

        self.assertFalse(generation_result["success"])
        self.assertEqual(generation_result["error_status_code"], 502)
        self.assertEqual(chunks[-1]["error"]["status_code"], 502)
        self.assertEqual(chunks[-1]["upscale_status"], "failed")
        handler.file_cache.download_and_cache.assert_not_awaited()
        self.assertEqual(handler._maybe_update_poll_task.await_args.kwargs["status"], "failed")
        self.assertEqual(handler._maybe_update_poll_task.await_args.kwargs["upscale_status"], "failed")

    async def test_empty_upscale_response_fails_without_1k_fallback(self):
        handler = self._build_handler(upsample_result="")

        generation_result, chunks, handler = await self._run_image_generation(handler)

        self.assertFalse(generation_result["success"])
        self.assertIn("empty image", chunks[-1]["error"]["message"])
        handler.file_cache.download_and_cache.assert_not_awaited()

    async def test_upscale_cache_failure_fails_without_inline_fallback(self):
        handler = self._build_handler(upsample_result="base64-image", cache_error=Exception("disk full"))

        generation_result, chunks, handler = await self._run_image_generation(handler)

        self.assertFalse(generation_result["success"])
        self.assertEqual(chunks[-1]["upscale_status"], "failed")
        self.assertIn("could not cache high-resolution image", chunks[-1]["error"]["message"])
        handler.file_cache.download_and_cache.assert_not_awaited()

    async def test_upscale_cache_disabled_returns_inline_data_url(self):
        handler = self._build_handler(upsample_result="base64-image")

        generation_result, chunks, handler = await self._run_image_generation(handler, cache_enabled=False)

        self.assertTrue(generation_result["success"])
        handler.file_cache.cache_base64_image.assert_not_awaited()
        handler.file_cache.download_and_cache.assert_not_awaited()
        self.assertEqual(
            chunks[-1]["generated_assets"]["upscaled_image"]["url"],
            "data:image/png;base64,base64-image",
        )


class VeoLiteModelResolverTests(unittest.TestCase):
    def test_resolve_t2v_lite_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "veo_3_1_t2v_lite",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_t2v_lite_portrait")

    def test_resolve_quality_4s_upsample_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "veo_3_1_t2v_4s_4k",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_t2v_portrait_4s_4k")

    def test_resolve_video_image_size_to_upsample_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="landscape", imageSize="1080p")
        )

        resolved = resolve_model_name(
            "veo_3_1_i2v_s_6s",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_i2v_s_6s_1080p")

    def test_resolve_quality_8s_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "veo_3_1_t2v_8s",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_t2v_portrait_8s")

    def test_resolve_quality_8s_upsample_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(
                aspectRatio="portrait",
                imageSize="4k",
            )
        )

        resolved = resolve_model_name(
            "veo_3_1_i2v_s_8s",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_i2v_s_portrait_8s_4k")

    def test_image_models_follow_nearest_reference_image_aspect(self):
        cases = {
            (1600, 900): "landscape",
            (900, 1600): "portrait",
            (1000, 1000): "square",
            (1200, 900): "four-three",
            (900, 1200): "three-four",
        }

        for size, expected in cases.items():
            with self.subTest(size=size):
                resolved = resolve_model_name(
                    "gemini-3.0-pro-image",
                    request=types.SimpleNamespace(generationConfig=None),
                    model_config=MODEL_CONFIG,
                    images=[make_image_bytes(size)],
                )
                self.assertEqual(resolved, f"gemini-3.0-pro-image-{expected}")

    def test_video_inference_collapses_to_landscape_or_portrait(self):
        cases = {
            (1600, 900): "veo_3_1_i2v_s_8s",
            (1000, 1000): "veo_3_1_i2v_s_8s",
            (900, 1600): "veo_3_1_i2v_s_portrait_8s",
        }

        for size, expected in cases.items():
            with self.subTest(size=size):
                resolved = resolve_model_name(
                    "veo_3_1_i2v_s_8s",
                    request=types.SimpleNamespace(generationConfig=None),
                    model_config=MODEL_CONFIG,
                    images=[make_image_bytes(size)],
                )
                self.assertEqual(resolved, expected)

    def test_explicit_aspect_ratio_overrides_reference_image(self):
        resolved = resolve_model_name(
            "gemini-3.0-pro-image",
            request=types.SimpleNamespace(
                generationConfig=types.SimpleNamespace(aspectRatio="landscape")
            ),
            model_config=MODEL_CONFIG,
            images=[make_image_bytes((900, 1600))],
        )

        self.assertEqual(resolved, "gemini-3.0-pro-image-landscape")

    def test_first_non_empty_image_controls_inferred_aspect(self):
        resolved = resolve_model_name(
            "gemini-3.0-pro-image",
            request=types.SimpleNamespace(generationConfig=None),
            model_config=MODEL_CONFIG,
            images=[b"", make_image_bytes((900, 1600)), make_image_bytes((1600, 900))],
        )

        self.assertEqual(resolved, "gemini-3.0-pro-image-portrait")

    def test_exif_orientation_is_applied_before_inference(self):
        resolved = resolve_model_name(
            "gemini-3.0-pro-image",
            request=types.SimpleNamespace(generationConfig=None),
            model_config=MODEL_CONFIG,
            images=[make_image_bytes((1600, 900), orientation=6)],
        )

        self.assertEqual(resolved, "gemini-3.0-pro-image-portrait")

    def test_unreadable_or_missing_images_preserve_default_aspect(self):
        for images in (None, [], [b"not-an-image"]):
            with self.subTest(images=images):
                resolved = resolve_model_name(
                    "gemini-3.0-pro-image",
                    request=types.SimpleNamespace(generationConfig=None),
                    model_config=MODEL_CONFIG,
                    images=images,
                )
                self.assertEqual(resolved, "gemini-3.0-pro-image-landscape")

    def test_missing_pillow_preserves_default_aspect(self):
        real_import = __import__

        def import_without_pillow(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("Pillow unavailable")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_pillow):
            resolved = resolve_model_name(
                "gemini-3.0-pro-image",
                request=types.SimpleNamespace(generationConfig=None),
                model_config=MODEL_CONFIG,
                images=[make_image_bytes((900, 1600))],
            )

        self.assertEqual(resolved, "gemini-3.0-pro-image-landscape")


class VeoLiteGenerationHandlerTests(unittest.TestCase):
    def test_tier_two_does_not_upgrade_lite_model_to_fake_ultra(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v_lite",
                "allow_tier_upgrade": False,
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v_lite")
        self.assertIsNone(message)

    def test_tier_two_still_upgrades_regular_model(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v_fast",
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v_fast_ultra")
        self.assertIn("ultra", message)

    def test_quality_model_does_not_upgrade_to_fake_ultra(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v",
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v")
        self.assertIsNone(message)

    def test_quality_4s_upsample_model_generates_then_upsamples(self):
        cfg = MODEL_CONFIG["veo_3_1_t2v_4s_4k"]

        self.assertEqual(cfg["model_key"], "veo_3_1_t2v_quality_4s")
        self.assertEqual(cfg["video_type"], "t2v")
        self.assertEqual(cfg["upsample"]["model_key"], "veo_3_1_upsampler_4k")
        self.assertEqual(cfg["upsample"]["resolution"], "VIDEO_RESOLUTION_4K")

    def test_quality_6s_i2v_1080p_model_generates_then_upsamples(self):
        cfg = MODEL_CONFIG["veo_3_1_i2v_s_6s_1080p"]

        self.assertEqual(cfg["model_key"], "veo_3_1_i2v_s_quality_6s_fl")
        self.assertEqual(cfg["video_type"], "i2v")
        self.assertEqual(cfg["upsample"]["model_key"], "veo_3_1_upsampler_1080p")
        self.assertEqual(cfg["upsample"]["resolution"], "VIDEO_RESOLUTION_1080P")

    def test_direct_upsampler_keys_are_not_public_models(self):
        self.assertNotIn("veo_3_1_upsampler_4k", MODEL_CONFIG)
        self.assertNotIn("veo_3_1_upsampler_1080p", MODEL_CONFIG)

    def test_explicit_8s_aliases_reuse_default_upstream_keys(self):
        expected_model_keys = {
            "veo_3_1_t2v_fast_8s": "veo_3_1_t2v_fast",
            "veo_3_1_t2v_8s": "veo_3_1_t2v",
            "veo_3_1_i2v_s_fast_8s_fl": "veo_3_1_i2v_s_fast_fl",
            "veo_3_1_i2v_s_8s": "veo_3_1_i2v_s_fl",
            "veo_3_1_r2v_fast_8s": "veo_3_1_r2v_fast_landscape",
            "veo_3_1_r2v_fast_ultra_8s": "veo_3_1_r2v_fast_landscape_ultra",
            "veo_3_1_r2v_fast_ultra_relaxed_8s": "veo_3_1_r2v_fast_landscape_ultra_relaxed",
        }

        for alias, model_key in expected_model_keys.items():
            with self.subTest(alias=alias):
                self.assertEqual(MODEL_CONFIG[alias]["model_key"], model_key)

    def test_default_duration_models_include_complete_8s_aliases(self):
        expected_aliases = {
            "veo_3_1_t2v_fast_landscape_8s": "veo_3_1_t2v_fast_8s",
            "veo_3_1_t2v_fast_portrait_8s": "veo_3_1_t2v_fast_portrait",
            "veo_3_1_t2v_landscape_8s": "veo_3_1_t2v_8s",
            "veo_3_1_t2v_landscape_8s_4k": "veo_3_1_t2v_8s_4k",
            "veo_3_1_t2v_landscape_8s_1080p": "veo_3_1_t2v_8s_1080p",
            "veo_3_1_t2v_lite_landscape_8s": "veo_3_1_t2v_lite_8s_landscape",
            "veo_3_1_i2v_s_fast_landscape_8s_fl": "veo_3_1_i2v_s_fast_8s_fl",
            "veo_3_1_i2v_s_landscape_8s": "veo_3_1_i2v_s_8s",
            "veo_3_1_i2v_s_landscape_8s_4k": "veo_3_1_i2v_s_8s_4k",
            "veo_3_1_i2v_s_landscape_8s_1080p": "veo_3_1_i2v_s_8s_1080p",
            "veo_3_1_i2v_lite_landscape_8s": "veo_3_1_i2v_lite_8s_landscape",
            "veo_3_1_interpolation_lite_landscape_8s": "veo_3_1_interpolation_lite_8s_landscape",
            "veo_3_1_r2v_fast_landscape_8s": "veo_3_1_r2v_fast_8s",
            "veo_3_1_r2v_fast_landscape_ultra_8s": "veo_3_1_r2v_fast_ultra_8s",
            "veo_3_1_r2v_fast_landscape_ultra_relaxed_8s": "veo_3_1_r2v_fast_ultra_relaxed_8s",
        }

        for alias, target in expected_aliases.items():
            with self.subTest(alias=alias):
                self.assertIn(alias, MODEL_CONFIG)
                self.assertEqual(MODEL_CONFIG[alias], MODEL_CONFIG[target])

    def test_base_alias_catalog_exposes_8s_model_families(self):
        aliases = get_base_model_aliases()
        expected = {
            "veo_3_1_t2v_fast_8s",
            "veo_3_1_t2v_8s",
            "veo_3_1_t2v_8s_4k",
            "veo_3_1_t2v_8s_1080p",
            "veo_3_1_t2v_lite_8s",
            "veo_3_1_i2v_s_fast_8s_fl",
            "veo_3_1_i2v_s_8s",
            "veo_3_1_i2v_s_8s_4k",
            "veo_3_1_i2v_s_8s_1080p",
            "veo_3_1_i2v_lite_8s",
            "veo_3_1_interpolation_lite_8s",
            "veo_3_1_r2v_fast_8s",
            "veo_3_1_r2v_fast_ultra_8s",
            "veo_3_1_r2v_fast_ultra_relaxed_8s",
        }

        self.assertTrue(expected.issubset(aliases))


class ReferenceImageRouteNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_history_reference_drives_aspect_and_keeps_scope(self):
        portrait_image = make_image_bytes((900, 1600))
        request = ChatCompletionRequest(
            model="gemini-3.0-pro-image",
            messages=[
                ChatMessage(role="user", content="Generate an image"),
                ChatMessage(
                    role="assistant",
                    content="![result](https://example.com/history.png)",
                ),
                ChatMessage(role="user", content="Continue from the previous image"),
            ],
        )

        with patch.object(
            routes,
            "retrieve_image_data",
            new=AsyncMock(return_value=portrait_image),
        ) as retrieve:
            normalized = await routes._normalize_openai_request(
                request,
                SimpleNamespace(file_cache=None),
                api_key_id=17,
                allowed_token_ids={3, 5},
            )

        self.assertEqual(normalized.model, "gemini-3.0-pro-image-portrait")
        self.assertEqual(normalized.images, [portrait_image])
        retrieve.assert_awaited_once_with(
            "https://example.com/history.png",
            ANY,
            api_key_id=17,
            allowed_token_ids={3, 5},
        )

    async def test_historical_image_is_prepended_before_current_image(self):
        landscape_image = make_image_bytes((1600, 900))
        portrait_history = make_image_bytes((900, 1600))
        request = ChatCompletionRequest(
            model="gemini-3.0-pro-image",
            messages=[
                ChatMessage(role="user", content="Generate an image"),
                ChatMessage(
                    role="assistant",
                    content="![history](https://example.com/history.png)",
                ),
                ChatMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "Use both references"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/current.png"},
                        },
                    ],
                ),
            ],
        )

        async def retrieve_by_url(uri, _handler, **_kwargs):
            if uri.endswith("current.png"):
                return landscape_image
            return portrait_history

        with patch.object(
            routes,
            "retrieve_image_data",
            new=AsyncMock(side_effect=retrieve_by_url),
        ):
            normalized = await routes._normalize_openai_request(
                request, SimpleNamespace(file_cache=None)
            )

        self.assertEqual(normalized.images, [portrait_history, landscape_image])
        self.assertEqual(normalized.model, "gemini-3.0-pro-image-portrait")

    async def test_gemini_inline_image_drives_aspect(self):
        portrait_image = make_image_bytes((900, 1600))
        request = GeminiGenerateContentRequest(
            contents=[
                GeminiContent(
                    role="user",
                    parts=[
                        GeminiPart(text="Edit this image"),
                        GeminiPart(
                            inlineData=GeminiInlineData(
                                mimeType="image/jpeg",
                                data=base64.b64encode(portrait_image).decode("ascii"),
                            )
                        ),
                    ],
                )
            ]
        )

        normalized = await routes._normalize_gemini_request(
            "gemini-3.0-pro-image",
            request,
            SimpleNamespace(file_cache=None),
        )

        self.assertEqual(normalized.model, "gemini-3.0-pro-image-portrait")
        self.assertEqual(normalized.images, [portrait_image])

    async def test_gemini_file_image_drives_aspect_and_keeps_scope(self):
        portrait_image = make_image_bytes((900, 1600))
        request = GeminiGenerateContentRequest(
            contents=[
                GeminiContent(
                    role="user",
                    parts=[
                        GeminiPart(text="Edit this image"),
                        GeminiPart(
                            fileData=GeminiFileData(
                                fileUri="https://example.com/reference.jpg",
                                mimeType="image/jpeg",
                            )
                        ),
                    ],
                )
            ]
        )

        with patch.object(
            routes,
            "retrieve_image_data",
            new=AsyncMock(return_value=portrait_image),
        ) as retrieve:
            normalized = await routes._normalize_gemini_request(
                "gemini-3.0-pro-image",
                request,
                SimpleNamespace(file_cache=None),
                api_key_id=19,
                allowed_token_ids={7},
            )

        self.assertEqual(normalized.model, "gemini-3.0-pro-image-portrait")
        retrieve.assert_awaited_once_with(
            "https://example.com/reference.jpg",
            ANY,
            api_key_id=19,
            allowed_token_ids={7},
        )


class VideoCacheDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _build_handler(
        self,
        operation_status,
        cache_result="cached.mp4",
        cache_error=None,
        resolved_cdn_url=(
            "https://flow-content.google/video/media-1"
            "?Expires=1781281902&Signature=abc"
        ),
    ):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = SimpleNamespace(
            check_video_status=AsyncMock(return_value=operation_status),
            resolve_media_download_url=AsyncMock(return_value=resolved_cdn_url),
        )
        handler.file_cache = SimpleNamespace(
            download_and_cache=AsyncMock(side_effect=cache_error)
            if cache_error
            else AsyncMock(return_value=cache_result),
        )
        self.db_updates = []

        class FakeDb:
            async def update_task(_, task_id, **kwargs):
                self.db_updates.append({"task_id": task_id, **kwargs})

        handler.db = FakeDb()
        handler._update_request_log_progress = AsyncMock()
        handler._maybe_update_poll_task = AsyncMock()
        return handler

    async def _run_poll(self, handler, *, cache_enabled=True):
        token = SimpleNamespace(id=7, at="at-token", st="st-token", video_concurrency=1)
        generation_result = handler._create_generation_result()
        response_state = handler._create_response_state()
        response_state["base_url"] = "https://api.example.com"
        chunks = []

        original_poll_interval = config._config["flow"]["poll_interval"]
        original_max_attempts = config._config["flow"]["max_poll_attempts"]
        original_cache_enabled = config.cache_enabled
        try:
            config._config["flow"]["poll_interval"] = 0
            config._config["flow"]["max_poll_attempts"] = 1
            config.set_cache_enabled(cache_enabled)
            async for chunk in handler._poll_video_result(
                token=token,
                project_id="project-1",
                operations=[{"operation": {"name": "media-1"}, "projectId": "project-1"}],
                stream=False,
                api_key_id=11,
                generation_result=generation_result,
                response_state=response_state,
                request_log_state={"id": None, "progress": 0, "api_key_id": 11},
            ):
                chunks.append(json.loads(chunk))
        finally:
            config._config["flow"]["poll_interval"] = original_poll_interval
            config._config["flow"]["max_poll_attempts"] = original_max_attempts
            config.set_cache_enabled(original_cache_enabled)

        return generation_result, chunks

    def _successful_status(self, video_metadata):
        return {
            "operations": [
                {
                    "operation": {
                        "name": "media-1",
                        "metadata": {"video": video_metadata},
                    },
                    "mediaName": "media-1",
                    "projectId": "project-1",
                    "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
                }
            ]
        }

    async def test_video_redirect_source_is_cached_and_only_cache_url_is_returned(self):
        source_url = "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1"
        resolved_cdn_url = (
            "https://flow-content.google/video/media-1"
            "?Expires=1781281902&KeyName=labs-flow-prod-cdn-key&Signature=abc"
        )
        handler = self._build_handler(
            self._successful_status(
                {
                    "fifeUrl": source_url,
                    "mediaGenerationId": "media-1",
                    "aspectRatio": "VIDEO_ASPECT_RATIO_PORTRAIT",
                }
            ),
            resolved_cdn_url=resolved_cdn_url,
        )

        generation_result, chunks = await self._run_poll(handler)

        self.assertTrue(generation_result["success"])
        handler.flow_client.resolve_media_download_url.assert_awaited_once_with(
            media_id="media-1",
            st="st-token",
            at="at-token",
            token_id=7,
        )
        handler.file_cache.download_and_cache.assert_awaited_once_with(
            resolved_cdn_url,
            "video",
            api_key_id=11,
            token_id=7,
            flow_project_id="project-1",
            auth_token=None,
            session_token=None,
        )
        returned_url = chunks[-1]["generated_assets"]["final_video_url"]
        self.assertIn("/api/cache/blob/cached.mp4", returned_url)
        self.assertNotIn("media.getMediaUrlRedirect", returned_url)
        self.assertNotIn("flow-content.google", chunks[-1]["choices"][0]["message"]["content"])
        self.assertEqual(self.db_updates[-1]["result_urls"], [returned_url])

    async def test_video_cache_disabled_returns_safe_source_url_without_cache_write(self):
        source_url = "https://flow-content.google/video/source"
        handler = self._build_handler(
            self._successful_status(
                {
                    "fifeUrl": source_url,
                    "mediaGenerationId": "media-1",
                }
            )
        )

        generation_result, chunks = await self._run_poll(handler, cache_enabled=False)

        self.assertTrue(generation_result["success"])
        handler.file_cache.download_and_cache.assert_not_awaited()
        self.assertEqual(chunks[-1]["generated_assets"]["final_video_url"], source_url)
        self.assertEqual(self.db_updates[-1]["result_urls"], [source_url])

    async def test_video_cache_disabled_fails_when_source_requires_backend_auth(self):
        source_url = "https://labs.google/fx/api/media/private-video"
        handler = self._build_handler(
            self._successful_status(
                {
                    "fifeUrl": source_url,
                    "mediaGenerationId": None,
                }
            )
        )

        generation_result, chunks = await self._run_poll(handler, cache_enabled=False)

        self.assertFalse(generation_result["success"])
        self.assertEqual(generation_result["error_status_code"], 502)
        handler.file_cache.download_and_cache.assert_not_awaited()
        self.assertEqual(chunks[-1]["error"]["code"], "cache_required")
        self.assertEqual(chunks[-1]["video_cache_status"], "cache_required")

    async def test_video_cache_failure_fails_without_returning_source_url(self):
        source_url = "https://flow-content.google/video/source"
        handler = self._build_handler(
            self._successful_status(
                {
                    "fifeUrl": source_url,
                    "mediaGenerationId": "media-1",
                }
            ),
            cache_error=Exception("disk full"),
        )

        generation_result, chunks = await self._run_poll(handler)

        self.assertFalse(generation_result["success"])
        self.assertEqual(generation_result["error_status_code"], 502)
        self.assertEqual(chunks[-1]["error"]["status_code"], 502)
        self.assertEqual(chunks[-1]["video_cache_status"], "failed")

    async def test_cdn_cache_rejection_reports_cdn_download_status(self):
        source_url = "https://flow-content.google/video/source?Expires=1&Signature=abc"
        handler = self._build_handler(
            self._successful_status(
                {
                    "fifeUrl": source_url,
                    "mediaGenerationId": "media-1",
                }
            ),
            cache_error=Exception("Video CDN download was rejected by flow-content.google"),
        )

        generation_result, chunks = await self._run_poll(handler)

        self.assertFalse(generation_result["success"])
        self.assertEqual(chunks[-1]["video_cache_status"], "cdn_download_rejected")
        self.assertNotIn(source_url, json.dumps(chunks[-1], ensure_ascii=False))
        self.assertEqual(self.db_updates[-1]["status"], "failed")

    async def test_serving_base_uri_is_cached_before_delivery(self):
        source_url = "https://flow-content.google/video/serving-base"
        handler = self._build_handler(
            self._successful_status(
                {
                    "fifeUrl": source_url,
                    "mediaGenerationId": "media-1",
                }
            )
        )

        generation_result, chunks = await self._run_poll(handler)

        self.assertTrue(generation_result["success"])
        handler.file_cache.download_and_cache.assert_awaited_once_with(
            source_url,
            "video",
            api_key_id=11,
            token_id=7,
            flow_project_id="project-1",
            auth_token=None,
            session_token=None,
        )
        handler.flow_client.resolve_media_download_url.assert_not_called()
        self.assertIn("/api/cache/blob/cached.mp4", chunks[-1]["generated_assets"]["final_video_url"])

    async def test_video_redirect_resolve_failure_does_not_return_source_url(self):
        source_url = "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1"
        handler = self._build_handler(
            self._successful_status(
                {
                    "fifeUrl": source_url,
                    "mediaGenerationId": "media-1",
                }
            ),
        )
        handler.flow_client.resolve_media_download_url = AsyncMock(
            side_effect=Exception("Media redirect rejected by Flow media endpoint (HTTP 403)")
        )

        generation_result, chunks = await self._run_poll(handler)

        self.assertFalse(generation_result["success"])
        self.assertEqual(generation_result["error_status_code"], 502)
        self.assertEqual(chunks[-1]["video_cache_status"], "resolve_failed")
        handler.file_cache.download_and_cache.assert_not_called()
        self.assertNotIn(source_url, json.dumps(chunks[-1], ensure_ascii=False))


class FileCacheVideoDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_download_uses_redirect_and_labs_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            fake_response = SimpleNamespace(
                status_code=200,
                content=fake_mp4_bytes(),
                headers={"content-type": "video/mp4"},
            )
            fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            with patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()):
                filename = await cache.download_and_cache(
                    "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1",
                    "video",
                    api_key_id=1,
                    token_id=2,
                    flow_project_id="project-1",
                    auth_token="at-token",
                    session_token="st-token",
                )

            self.assertTrue(filename.endswith(".mp4"))
            call_kwargs = fake_session.get.await_args.kwargs
            self.assertTrue(call_kwargs["allow_redirects"])
            self.assertEqual(call_kwargs["headers"]["Origin"], "https://labs.google")
            self.assertEqual(call_kwargs["headers"]["Referer"], "https://labs.google/fx/tools/flow")
            self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer at-token")
            self.assertIn("__Secure-next-auth.session-token=st-token", call_kwargs["headers"]["Cookie"])
            self.assertEqual(call_kwargs["headers"]["Sec-Fetch-Site"], "same-origin")

    async def test_video_download_uses_httpx_when_cli_tools_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            httpx_response = SimpleNamespace(
                status_code=200,
                content=fake_mp4_bytes(),
                headers={"content-type": "video/mp4"},
            )
            fake_httpx_client = SimpleNamespace(get=AsyncMock(return_value=httpx_response))

            class FailingAsyncSession:
                async def __aenter__(self):
                    raise Exception("curl_cffi transport failed")

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            class FakeHttpxClient:
                def __init__(self, *args, **kwargs):
                    self.kwargs = kwargs

                async def __aenter__(self):
                    return fake_httpx_client

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            with (
                patch("sub2gen.services.file_cache.AsyncSession", return_value=FailingAsyncSession()),
                patch("sub2gen.services.file_cache.httpx.AsyncClient", FakeHttpxClient),
                patch("subprocess.run", side_effect=FileNotFoundError("curl")) as run_mock,
            ):
                filename = await cache.download_and_cache(
                    "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1",
                    "video",
                    auth_token="at-token",
                )

            self.assertTrue(filename.endswith(".mp4"))
            run_mock.assert_not_called()
            httpx_call_kwargs = fake_httpx_client.get.await_args.kwargs
            self.assertEqual(httpx_call_kwargs["headers"]["Authorization"], "Bearer at-token")
            self.assertEqual(httpx_call_kwargs["headers"]["Origin"], "https://labs.google")

    async def test_cdn_video_download_omits_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            fake_response = SimpleNamespace(
                status_code=200,
                content=fake_mp4_bytes(),
                headers={"content-type": "video/mp4"},
            )
            fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            cdn_url = (
                "https://flow-content.google/video/media-1"
                "?Expires=1781281902&Signature=abc"
            )
            with patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()):
                await cache.download_and_cache(
                    cdn_url,
                    "video",
                    auth_token="at-token",
                    session_token="st-token",
                )

            headers = fake_session.get.await_args.kwargs["headers"]
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)
            self.assertNotIn("Origin", headers)

    async def test_cdn_video_download_uses_no_cors_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            cdn_url = (
                "https://flow-content.google/video/media-1"
                "?Expires=1781281902&Signature=abc"
            )
            headers = cache._build_download_headers("video", url=cdn_url)
            self.assertEqual(headers["Sec-Fetch-Mode"], "no-cors")
            self.assertEqual(headers["Sec-Fetch-Site"], "cross-site")
            self.assertNotIn("Origin", headers)

    async def test_video_download_uses_cross_site_headers_for_cdn_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            fake_response = SimpleNamespace(
                status_code=200,
                content=fake_mp4_bytes(),
                headers={"content-type": "video/mp4"},
            )
            fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            cdn_url = (
                "https://flow-content.google/video/media-1"
                "?Expires=1781281902&Signature=abc"
            )
            with patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()):
                await cache.download_and_cache(cdn_url, "video")

            headers = fake_session.get.await_args.kwargs["headers"]
            self.assertEqual(headers["Sec-Fetch-Site"], "cross-site")
            self.assertEqual(headers["Sec-Fetch-Mode"], "no-cors")
            self.assertNotIn("Cookie", headers)
            self.assertNotIn("Authorization", headers)

    async def test_cdn_403_retries_with_media_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            proxy_manager = SimpleNamespace(
                get_media_proxy_url=AsyncMock(return_value="http://media-proxy:8080"),
            )
            cache = FileCache(
                cache_dir=tmp,
                proxy_manager=proxy_manager,
                db=SimpleNamespace(record_cache_file=AsyncMock()),
            )
            cdn_url = (
                "https://flow-content.google/video/media-1"
                "?Expires=1781281902&Signature=abc"
            )
            forbidden = SimpleNamespace(
                status_code=403,
                content=b"forbidden",
                headers={"content-type": "text/plain"},
            )
            success = SimpleNamespace(
                status_code=200,
                content=fake_mp4_bytes(),
                headers={"content-type": "video/mp4"},
            )
            fake_session = SimpleNamespace(
                get=AsyncMock(side_effect=[forbidden, success]),
            )

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            with patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()):
                filename = await cache.download_and_cache(cdn_url, "video")

            self.assertTrue(filename.endswith(".mp4"))
            self.assertEqual(fake_session.get.await_count, 2)
            first_proxy = fake_session.get.await_args_list[0].kwargs["proxy"]
            second_proxy = fake_session.get.await_args_list[1].kwargs["proxy"]
            self.assertIsNone(first_proxy)
            self.assertEqual(second_proxy, "http://media-proxy:8080")

    async def test_video_download_rejects_tiny_mp4_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            fake_response = SimpleNamespace(
                status_code=200,
                content=b"\x00\x00\x00\x18ftypmp42bad",
                headers={"content-type": "video/mp4"},
            )
            fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            with (
                patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()),
                patch("sub2gen.services.file_cache.httpx.AsyncClient", return_value=FakeAsyncSession()),
                patch("subprocess.run", side_effect=FileNotFoundError("curl")),
            ):
                with self.assertRaises(Exception) as ctx:
                    await cache.download_and_cache(
                        "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1",
                        "video",
                    )
            self.assertIn("not valid media", str(ctx.exception))

    async def test_video_download_rejects_text_plain_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            fake_response = SimpleNamespace(
                status_code=200,
                content=b"Authentication required",
                headers={"content-type": "text/plain"},
            )
            fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            with (
                patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()),
                patch("sub2gen.services.file_cache.httpx.AsyncClient", return_value=FakeAsyncSession()),
                patch("subprocess.run", side_effect=FileNotFoundError("curl")),
            ):
                with self.assertRaises(Exception) as ctx:
                    await cache.download_and_cache(
                        "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1",
                        "video",
                    )
            self.assertIn("not valid media", str(ctx.exception))

    async def test_video_download_rejects_json_error_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            fake_response = SimpleNamespace(
                status_code=200,
                content=b'{"error":"not ready"}' + b" " * 2048,
                headers={"content-type": "application/json"},
            )
            fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            with (
                patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()),
                patch("sub2gen.services.file_cache.httpx.AsyncClient", return_value=FakeAsyncSession()),
                patch("subprocess.run", side_effect=FileNotFoundError("curl")),
            ):
                with self.assertRaises(Exception) as ctx:
                    await cache.download_and_cache(
                        "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1",
                        "video",
                    )
            self.assertIn("not valid media", str(ctx.exception))

    async def test_invalid_existing_video_cache_is_deleted_and_redownloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            source_url = "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1"
            filename = cache._generate_cache_filename(source_url, "video")
            bad_path = cache.cache_dir / filename
            bad_path.write_bytes(b"\x00\x00\x00\x18ftypmp42bad")
            fake_response = SimpleNamespace(
                status_code=200,
                content=fake_mp4_bytes(),
                headers={"content-type": "video/mp4"},
            )
            fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            with patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()):
                returned_filename = await cache.download_and_cache(source_url, "video")

            self.assertEqual(returned_filename, filename)
            self.assertEqual(bad_path.read_bytes(), fake_response.content)

    async def test_video_download_rejects_html_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            fake_response = SimpleNamespace(
                status_code=200,
                content=b"<!doctype html><html></html>",
                headers={"content-type": "text/html"},
            )
            fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            with (
                patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()),
                patch("sub2gen.services.file_cache.httpx.AsyncClient", return_value=FakeAsyncSession()),
                patch("subprocess.run", side_effect=FileNotFoundError("curl")),
            ):
                with self.assertRaises(Exception) as ctx:
                    await cache.download_and_cache(
                        "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1",
                        "video",
                    )
            self.assertIn("not valid media", str(ctx.exception))
            self.assertNotIn("curl", str(ctx.exception).lower())

    async def test_video_download_rejected_by_flow_is_not_reported_as_missing_curl(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            fake_response = SimpleNamespace(
                status_code=403,
                content=b"forbidden",
                headers={"content-type": "text/plain"},
            )
            fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            with (
                patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()),
                patch("sub2gen.services.file_cache.httpx.AsyncClient", return_value=FakeAsyncSession()),
                patch("subprocess.run", side_effect=FileNotFoundError("curl")),
            ):
                with self.assertRaises(Exception) as ctx:
                    await cache.download_and_cache(
                        "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1",
                        "video",
                    )
            self.assertIn("rejected by Flow media endpoint", str(ctx.exception))
            self.assertNotIn("本机未安装 curl", str(ctx.exception))

    async def test_cdn_download_rejection_uses_cdn_specific_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = FileCache(cache_dir=tmp, db=SimpleNamespace(record_cache_file=AsyncMock()))
            cdn_url = (
                "https://flow-content.google/video/media-1"
                "?Expires=1781281902&Signature=abc"
            )
            fake_response = SimpleNamespace(
                status_code=403,
                content=b"forbidden",
                headers={"content-type": "text/plain"},
            )
            fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

            class FakeAsyncSession:
                async def __aenter__(self):
                    return fake_session

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            with (
                patch("sub2gen.services.file_cache.AsyncSession", return_value=FakeAsyncSession()),
                patch("sub2gen.services.file_cache.httpx.AsyncClient", return_value=FakeAsyncSession()),
                patch("subprocess.run", side_effect=FileNotFoundError("curl")),
            ):
                with self.assertRaises(Exception) as ctx:
                    await cache.download_and_cache(cdn_url, "video")
            self.assertIn("flow-content.google", str(ctx.exception))


class VeoLiteFlowClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FlowClient(proxy_manager=None)
        self.client._acquire_video_launch_gate = AsyncMock(return_value=(True, None, None))
        self.client._release_video_launch_gate = AsyncMock()
        self.client._get_recaptcha_token = AsyncMock(return_value=("recaptcha-token", "browser-1"))
        self.client._notify_browser_captcha_request_finished = AsyncMock()

    async def test_upsample_image_error_emits_terminal_failed_progress(self):
        updates = []

        async def collect_progress(update):
            updates.append(dict(update))

        self.client._make_request = AsyncMock(side_effect=Exception("bad request"))

        with self.assertRaises(Exception):
            await self.client.upsample_image(
                at="at-token",
                project_id="project-1",
                media_id="media-1",
                poll_task_progress=collect_progress,
            )

        self.assertTrue(
            any(
                update.get("status") == "failed"
                and update.get("job_phase") == "failed"
                and update.get("upscale_status") == "failed"
                for update in updates
            )
        )

    async def test_upsample_image_empty_response_emits_terminal_failed_progress(self):
        updates = []

        async def collect_progress(update):
            updates.append(dict(update))

        self.client._make_request = AsyncMock(return_value={})

        result = await self.client.upsample_image(
            at="at-token",
            project_id="project-1",
            media_id="media-1",
            poll_task_progress=collect_progress,
        )

        self.assertEqual(result, "")
        self.assertTrue(
            any(
                update.get("status") == "failed"
                and update.get("job_phase") == "failed"
                and update.get("upscale_status") == "failed"
                for update in updates
            )
        )

    async def test_generate_video_text_uses_v2_payload_for_lite(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["url"] = url
            captured["json_data"] = json_data
            return {"operations": [{"operation": {"name": "task-1"}}]}

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        await self.client.generate_video_text(
            at="at-token",
            project_id="project-1",
            prompt="猫猫",
            model_key="veo_3_1_t2v_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=True,
        )

        json_data = captured["json_data"]
        request_data = json_data["requests"][0]
        self.assertTrue(json_data["useV2ModelConfig"])
        self.assertIn("batchId", json_data["mediaGenerationContext"])
        self.assertEqual(
            request_data["textInput"]["structuredPrompt"]["parts"][0]["text"],
            "猫猫",
        )
        self.assertNotIn("prompt", request_data["textInput"])
        self.assertEqual(request_data["videoModelKey"], "veo_3_1_t2v_lite")
        self.assertEqual(
            json_data["mediaGenerationContext"]["audioFailurePreference"],
            "BLOCK_SILENCED_VIDEOS",
        )

    async def test_generate_video_text_normalizes_media_only_create_response(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["json_data"] = json_data
            return {
                "remainingCredits": 30,
                "workflows": [
                    {
                        "name": "workflow-1",
                        "metadata": {"primaryMediaId": "media-1"},
                        "projectId": "project-1",
                    }
                ],
                "media": [
                    {
                        "name": "media-1",
                        "projectId": "project-1",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_PENDING"
                            }
                        },
                    }
                ],
            }

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await self.client.generate_video_text(
            at="at-token",
            project_id="project-1",
            prompt="猫猫",
            model_key="veo_3_1_t2v_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=True,
        )

        self.assertEqual(
            captured["json_data"]["mediaGenerationContext"]["audioFailurePreference"],
            "BLOCK_SILENCED_VIDEOS",
        )
        self.assertEqual(result["operations"][0]["operation"]["name"], "media-1")
        self.assertEqual(result["operations"][0]["projectId"], "project-1")
        self.assertEqual(
            result["operations"][0]["status"],
            "MEDIA_GENERATION_STATUS_PENDING",
        )

    async def test_check_video_status_uses_media_payload_and_normalizes_response(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["json_data"] = json_data
            return {
                "media": [
                    {
                        "name": "media-1",
                        "projectId": "project-1",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                            }
                        },
                        "video": {
                            "fifeUrl": "https://flow-content.google/video/11111111-1111-1111-1111-111111111111?token=abc",
                            "generatedVideo": {
                                "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE"
                            },
                        },
                    }
                ]
            }

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await self.client.check_video_status(
            at="at-token",
            operations=[
                {
                    "operation": {"name": "media-1"},
                    "projectId": "project-1",
                }
            ],
        )

        self.assertEqual(
            captured["json_data"],
            {"media": [{"name": "media-1", "projectId": "project-1"}]},
        )
        operation = result["operations"][0]
        self.assertEqual(operation["operation"]["name"], "media-1")
        self.assertEqual(operation["status"], "MEDIA_GENERATION_STATUS_SUCCESSFUL")
        self.assertEqual(
            operation["operation"]["metadata"]["video"]["fifeUrl"],
            "https://flow-content.google/video/11111111-1111-1111-1111-111111111111?token=abc",
        )

    async def test_check_video_status_uses_serving_base_uri_as_video_url(self):
        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            return {
                "media": [
                    {
                        "name": "media-1",
                        "projectId": "project-1",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                            }
                        },
                        "video": {
                            "servingBaseUri": "https://flow-content.google/video/serving-base",
                            "mediaGenerationId": "video-media-1",
                        },
                    }
                ]
            }

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await self.client.check_video_status(
            at="at-token",
            operations=[
                {
                    "operation": {"name": "media-1"},
                    "projectId": "project-1",
                }
            ],
        )

        video = result["operations"][0]["operation"]["metadata"]["video"]
        self.assertEqual(video["fifeUrl"], "https://flow-content.google/video/serving-base")
        self.assertEqual(video["mediaGenerationId"], "video-media-1")

    async def test_check_video_status_synthesizes_media_redirect_url(self):
        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            return {
                "media": [
                    {
                        "name": "media 1/with symbols",
                        "projectId": "project-1",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                            }
                        },
                        "video": {},
                    }
                ]
            }

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await self.client.check_video_status(
            at="at-token",
            operations=[
                {
                    "operation": {"name": "media 1/with symbols"},
                    "projectId": "project-1",
                }
            ],
        )

        video = result["operations"][0]["operation"]["metadata"]["video"]
        self.assertEqual(
            video["fifeUrl"],
            f"{self.client.labs_base_url}/trpc/media.getMediaUrlRedirect?name=media%201%2Fwith%20symbols",
        )
        self.assertEqual(video["mediaGenerationId"], "media 1/with symbols")

    async def test_check_video_status_without_url_or_media_id_does_not_synthesize_url(self):
        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            return {
                "media": [
                    {
                        "projectId": "project-1",
                        "mediaMetadata": {
                            "mediaStatus": {
                                "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                            }
                        },
                        "video": {
                            "operation": {"name": "operation-1"},
                        },
                    }
                ]
            }

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await self.client.check_video_status(
            at="at-token",
            operations=[
                {
                    "operation": {"name": "operation-1"},
                    "projectId": "project-1",
                }
            ],
        )

        operation = result["operations"][0]
        self.assertNotIn("metadata", operation["operation"])

    async def test_generate_video_start_end_uses_v2_payload_for_interpolation_lite(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token, **kwargs):
            captured["url"] = url
            captured["json_data"] = json_data
            return {"operations": [{"operation": {"name": "task-2"}}]}

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        await self.client.generate_video_start_end(
            at="at-token",
            project_id="project-1",
            prompt="变身猫猫",
            model_key="veo_3_1_interpolation_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
            start_media_id="start-media",
            end_media_id="end-media",
            use_v2_model_config=True,
        )

        json_data = captured["json_data"]
        request_data = json_data["requests"][0]
        self.assertTrue(json_data["useV2ModelConfig"])
        self.assertIn("batchId", json_data["mediaGenerationContext"])
        self.assertEqual(request_data["videoModelKey"], "veo_3_1_interpolation_lite")
        self.assertEqual(request_data["startImage"]["mediaId"], "start-media")
        self.assertEqual(request_data["endImage"]["mediaId"], "end-media")
        self.assertEqual(
            request_data["textInput"]["structuredPrompt"]["parts"][0]["text"],
            "变身猫猫",
        )

    async def test_resolve_media_download_url_returns_location_on_307(self):
        redirect_url = f"{self.client.labs_base_url}/trpc/media.getMediaUrlRedirect?name=media-1"
        cdn_url = "https://flow-content.google/video/media-1?token=abc"
        fake_response = SimpleNamespace(
            status_code=307,
            headers={"location": cdn_url},
        )
        fake_session = SimpleNamespace(get=AsyncMock(return_value=fake_response))

        class FakeAsyncSession:
            async def __aenter__(self):
                return fake_session

            async def __aexit__(self, exc_type, exc, tb):
                return None

        with patch("sub2gen.services.flow_client.AsyncSession", return_value=FakeAsyncSession()):
            resolved = await self.client.resolve_media_download_url(
                media_id="media-1",
                st="st-token",
                at="at-token",
            )

        self.assertEqual(resolved, cdn_url)
        call_kwargs = fake_session.get.await_args.kwargs
        self.assertFalse(call_kwargs["allow_redirects"])
        self.assertIn("__Secure-next-auth.session-token=st-token", call_kwargs["headers"]["Cookie"])

    async def test_resolve_media_download_url_uses_st_cookie(self):
        captured_headers = {}
        redirect_url = f"{self.client.labs_base_url}/trpc/media.getMediaUrlRedirect?name=media-1"
        cdn_url = "https://flow-content.google/video/media-1?token=abc"

        async def fake_fetch(redirect, headers, proxy_url):
            captured_headers.update(headers)
            return cdn_url

        self.client._fetch_media_redirect_location = AsyncMock(side_effect=fake_fetch)

        resolved = await self.client.resolve_media_download_url(
            media_id="media-1",
            st="session-token-value",
            at="at-token",
        )

        self.assertEqual(resolved, cdn_url)
        self.assertIn(
            "__Secure-next-auth.session-token=session-token-value",
            captured_headers["Cookie"],
        )
        self.assertNotIn("Authorization", captured_headers)

    async def test_media_redirect_request_logs_redact_credentials_and_signed_query(self):
        st_token = "secret-session-token"
        signed_url = (
            "https://flow-content.google/video/media-1"
            "?Expires=1781281902&KeyName=cdn-key&Signature=secret-signature&token=secret-token"
        )
        self.client._resolve_media_redirect_proxy = AsyncMock(
            return_value="http://proxy-user:proxy-pass@proxy.example:8080"
        )
        self.client._fetch_media_redirect_location = AsyncMock(return_value=signed_url)

        previous_debug = config.debug_enabled
        try:
            config.set_debug_enabled(True)
            with (
                patch.object(debug_logger, "log_request") as log_request,
                patch.object(debug_logger, "log_info") as log_info,
            ):
                resolved = await self.client.resolve_media_download_url(
                    media_id="media-1",
                    st=st_token,
                )
        finally:
            config.set_debug_enabled(previous_debug)

        self.assertEqual(resolved, signed_url)
        request_kwargs = log_request.call_args.kwargs
        self.assertEqual(request_kwargs["headers"]["Cookie"], "<redacted>")
        self.assertEqual(request_kwargs["proxy"], "http://proxy.example:8080")
        self.assertEqual(
            request_kwargs["url"],
            f"{self.client.labs_base_url}/trpc/media.getMediaUrlRedirect",
        )
        logged_text = repr(log_request.call_args_list) + repr(log_info.call_args_list)
        for secret in (st_token, "proxy-user", "proxy-pass", "secret-signature", "secret-token"):
            self.assertNotIn(secret, logged_text)

    async def test_media_redirect_response_logs_remove_signed_query(self):
        signed_url = (
            "https://flow-content.google/video/media-1"
            "?Expires=1781281902&Signature=secret-signature"
        )
        self.client._fetch_media_redirect_location_curl = AsyncMock(
            return_value=(307, signed_url)
        )
        self.client._fetch_media_redirect_location_httpx = AsyncMock()

        previous_debug = config.debug_enabled
        try:
            config.set_debug_enabled(True)
            with patch.object(debug_logger, "log_response") as log_response:
                resolved = await self.client._fetch_media_redirect_location(
                    redirect_url=f"{self.client.labs_base_url}/trpc/media.getMediaUrlRedirect?name=media-1",
                    headers={"Cookie": "__Secure-next-auth.session-token=secret"},
                    proxy_url=None,
                )
        finally:
            config.set_debug_enabled(previous_debug)

        self.assertEqual(resolved, signed_url)
        response_kwargs = log_response.call_args.kwargs
        self.assertEqual(
            response_kwargs["headers"]["Location"],
            "https://flow-content.google/video/media-1",
        )
        self.assertEqual(response_kwargs["body"]["transport"], "curl_cffi")
        self.assertIsInstance(response_kwargs["duration_ms"], float)
        self.assertNotIn("secret-signature", repr(log_response.call_args_list))
        self.client._fetch_media_redirect_location_httpx.assert_not_awaited()

    async def test_media_redirect_failure_logs_sanitize_signed_urls(self):
        signed_url = "https://flow-content.google/video/media-1?Signature=secret-signature"
        self.assertEqual(
            self.client._sanitize_media_redirect_url_for_log(
                "https://user:password@flow-content.google/video/media-1?Signature=secret"
            ),
            "https://flow-content.google/video/media-1",
        )
        self.client._fetch_media_redirect_location_curl = AsyncMock(
            side_effect=RuntimeError(f"curl failed for {signed_url}")
        )
        self.client._fetch_media_redirect_location_httpx = AsyncMock(
            side_effect=RuntimeError(f"httpx failed for {signed_url}")
        )

        with (
            patch.object(debug_logger, "log_warning") as log_warning,
            patch.object(debug_logger, "log_error") as log_error,
        ):
            with self.assertRaisesRegex(RuntimeError, "httpx failed"):
                await self.client._fetch_media_redirect_location(
                    redirect_url=f"{self.client.labs_base_url}/trpc/media.getMediaUrlRedirect?name=media-1",
                    headers={},
                    proxy_url=None,
                )

        logged_text = repr(log_warning.call_args_list) + repr(log_error.call_args_list)
        self.assertNotIn("secret-signature", logged_text)
        self.assertNotIn("Signature=", logged_text)
        self.assertIn("https://flow-content.google/video/media-1", logged_text)

    async def test_get_media_url_redirect_delegates_to_current_resolver(self):
        cdn_url = "https://flow-content.google/video/media-1?token=abc"
        self.client.resolve_media_download_url = AsyncMock(return_value=cdn_url)

        resolved = await self.client.get_media_url_redirect(
            st=" st-token ",
            media_name=" media-1 ",
        )

        self.assertEqual(resolved, cdn_url)
        self.client.resolve_media_download_url.assert_awaited_once_with(
            media_id="media-1",
            st="st-token",
        )

    async def test_get_media_url_redirect_validates_required_inputs(self):
        self.client.resolve_media_download_url = AsyncMock()

        with self.assertRaisesRegex(ValueError, "media_name"):
            await self.client.get_media_url_redirect(st="st-token", media_name=" ")
        with self.assertRaisesRegex(ValueError, "ST token"):
            await self.client.get_media_url_redirect(st=" ", media_name="media-1")

        self.client.resolve_media_download_url.assert_not_awaited()

    async def test_needs_video_url_resolve_only_for_redirect_or_missing_cdn(self):
        redirect = "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=media-1"
        cdn = "https://flow-content.google/video/media-1?token=abc"
        local = "http://127.0.0.1:8000/tmp/concat.mp4"

        self.assertTrue(_needs_video_url_resolve(None, "media-1"))
        self.assertTrue(_needs_video_url_resolve(redirect, "media-1"))
        self.assertFalse(_needs_video_url_resolve(cdn, "media-1"))
        self.assertFalse(_needs_video_url_resolve(local, "media-1"))
        self.assertFalse(_needs_video_url_resolve(redirect, None))


class FileCacheSpaceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reclaims_stale_then_expired_then_oldest_and_stops(self):
        with tempfile.TemporaryDirectory() as root:
            cache_dir = Path(root) / "tmp"
            cache_dir.mkdir()
            cache = FileCache(cache_dir=str(cache_dir), default_timeout=60)
            now = time.time()
            files = {
                "stale.mp4.part": now - 600,
                "expired.mp4": now - 300,
                "old.mp4": now - 30,
                "new.mp4": now - 10,
            }
            for name, mtime in files.items():
                path = cache_dir / name
                path.write_bytes(b"x" * 10)
                os.utime(path, (mtime, mtime))

            initial_names = set(files)

            def disk_usage():
                remaining = sum(
                    (cache_dir / name).stat().st_size
                    for name in initial_names
                    if (cache_dir / name).exists()
                )
                freed = 40 - remaining
                return SimpleNamespace(total=1000, used=1000 - freed, free=freed)

            cache._disk_usage = disk_usage
            result = await cache.reclaim_cache_space(target_free_bytes=25)

            self.assertEqual(result["removed_count"], 3)
            self.assertFalse((cache_dir / "stale.mp4.part").exists())
            self.assertFalse((cache_dir / "expired.mp4").exists())
            self.assertFalse((cache_dir / "old.mp4").exists())
            self.assertTrue((cache_dir / "new.mp4").exists())

    async def test_recovery_never_touches_database_directories_symlinks_or_other_files(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            cache_dir = root_path / "tmp"
            data_dir = root_path / "data"
            cache_dir.mkdir()
            data_dir.mkdir()
            database = data_dir / "sub2gen.db"
            database.write_bytes(b"database")
            unrelated = cache_dir / "notes.txt"
            unrelated.write_bytes(b"keep")
            nested = cache_dir / "nested"
            nested.mkdir()
            (nested / "inside.mp4").write_bytes(b"keep")
            media = cache_dir / "evict.mp4"
            media.write_bytes(b"media")
            link = cache_dir / "linked.mp4"
            try:
                link.symlink_to(database)
            except OSError:
                link = None

            cache = FileCache(cache_dir=str(cache_dir))
            cache._disk_usage = lambda: SimpleNamespace(total=1000, used=1000, free=0)
            await cache.reclaim_cache_space(target_free_bytes=100)

            self.assertTrue(database.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(nested.is_dir())
            self.assertFalse((nested / "inside.mp4").exists())
            if link is not None:
                self.assertTrue(link.is_symlink())

    async def test_base64_write_checks_capacity_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as root:
            cache = FileCache(cache_dir=root)
            cache.ensure_cache_capacity = AsyncMock(return_value={})

            def fail_write(path, _content):
                path.with_suffix(f"{path.suffix}.part").write_bytes(b"partial")
                raise OSError(28, "disk full")

            cache._write_cached_content = fail_write
            with self.assertRaisesRegex(Exception, "disk full"):
                await cache.cache_base64_image("aGVsbG8=")

            cache.ensure_cache_capacity.assert_awaited_once_with(5)
            self.assertEqual(list(Path(root).glob("*.part")), [])


class StartupStorageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_emergency_prune_compacts_history_without_deleting_tokens(self):
        if os.name == "nt":
            self.skipTest("Windows keeps SQLite file handles longer than Railway/Linux")
        from sub2gen.main import _emergency_prune_sqlite_history

        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "sub2gen.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE tokens (id INTEGER PRIMARY KEY, st TEXT)")
                conn.execute("CREATE TABLE request_logs (id INTEGER PRIMARY KEY, request_body TEXT)")
                conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, prompt TEXT)")
                conn.execute("CREATE TABLE cache_files (id INTEGER PRIMARY KEY, filename TEXT)")
                conn.execute("INSERT INTO tokens (id, st) VALUES (1, 'keep')")
                for i in range(20):
                    conn.execute(
                        "INSERT INTO request_logs (request_body) VALUES (?)",
                        ("x" * 1000,),
                    )
                    conn.execute("INSERT INTO tasks (prompt) VALUES (?)", (f"task-{i}",))
                    conn.execute("INSERT INTO cache_files (filename) VALUES (?)", (f"{i}.mp4",))
                conn.commit()

            result = _emergency_prune_sqlite_history(SimpleNamespace(db_path=str(db_path)))

            self.assertTrue(result["success"])
            self.assertEqual(result["deleted_rows"]["request_logs"], 20)
            self.assertEqual(result["deleted_rows"]["tasks"], 20)
            self.assertEqual(result["deleted_rows"]["cache_files"], 20)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT st FROM tokens").fetchone()[0], "keep")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM cache_files").fetchone()[0], 0)

    async def test_sqlite_full_reclaims_cache_and_retries_once(self):
        from sub2gen.main import _init_database_with_storage_recovery

        database = SimpleNamespace(
            init_db=AsyncMock(
                side_effect=[sqlite3.OperationalError("database or disk is full"), None]
            )
        )
        cache = SimpleNamespace(
            _cleanup_expired_files=AsyncMock(return_value={}),
            reclaim_cache_space=AsyncMock(
                return_value={
                    "free_after": 600,
                    "target_free": 500,
                    "reclaimed_bytes": 400,
                }
            ),
        )

        await _init_database_with_storage_recovery(database, cache)
        self.assertEqual(database.init_db.await_count, 2)
        cache._cleanup_expired_files.assert_awaited_once()
        cache.reclaim_cache_space.assert_awaited_once()

    async def test_sqlite_disk_io_error_reclaims_cache_and_retries_once(self):
        from sub2gen.main import _init_database_with_storage_recovery

        database = SimpleNamespace(
            init_db=AsyncMock(
                side_effect=[sqlite3.OperationalError("disk I/O error"), None]
            )
        )
        cache = SimpleNamespace(
            _cleanup_expired_files=AsyncMock(return_value={}),
            reclaim_cache_space=AsyncMock(
                return_value={
                    "free_after": 600,
                    "target_free": 500,
                    "reclaimed_bytes": 400,
                }
            ),
        )

        await _init_database_with_storage_recovery(database, cache)
        self.assertEqual(database.init_db.await_count, 2)
        cache._cleanup_expired_files.assert_awaited_once()
        cache.reclaim_cache_space.assert_awaited_once()

    async def test_config_seed_storage_error_retries_full_startup_branch(self):
        from sub2gen.main import _init_database_with_storage_recovery

        database = SimpleNamespace(
            init_db=AsyncMock(return_value=None),
            init_config_from_toml=AsyncMock(
                side_effect=[sqlite3.OperationalError("disk I/O error"), None]
            ),
        )
        cache = SimpleNamespace(
            _cleanup_expired_files=AsyncMock(return_value={}),
            reclaim_cache_space=AsyncMock(
                return_value={
                    "free_after": 600,
                    "target_free": 500,
                    "reclaimed_bytes": 400,
                }
            ),
        )

        await _init_database_with_storage_recovery(
            database,
            cache,
            config_dict={"global": {}},
            is_first_startup=True,
        )
        self.assertEqual(database.init_db.await_count, 2)
        self.assertEqual(database.init_config_from_toml.await_count, 2)
        cache.reclaim_cache_space.assert_awaited_once()

    async def test_migration_storage_error_retries_full_startup_branch(self):
        from sub2gen.main import _init_database_with_storage_recovery

        database = SimpleNamespace(
            init_db=AsyncMock(return_value=None),
            check_and_migrate_db=AsyncMock(
                side_effect=[sqlite3.OperationalError("disk I/O error"), None]
            ),
        )
        cache = SimpleNamespace(
            _cleanup_expired_files=AsyncMock(return_value={}),
            reclaim_cache_space=AsyncMock(
                return_value={
                    "free_after": 600,
                    "target_free": 500,
                    "reclaimed_bytes": 400,
                }
            ),
        )

        await _init_database_with_storage_recovery(
            database,
            cache,
            config_dict={"global": {}},
            is_first_startup=False,
        )
        self.assertEqual(database.init_db.await_count, 2)
        self.assertEqual(database.check_and_migrate_db.await_count, 2)
        cache.reclaim_cache_space.assert_awaited_once()

    async def test_unrecoverable_full_volume_has_concise_diagnostic(self):
        from sub2gen.main import _init_database_with_storage_recovery

        database = SimpleNamespace(
            init_db=AsyncMock(side_effect=sqlite3.OperationalError("database or disk is full"))
        )
        cache = SimpleNamespace(
            _cleanup_expired_files=AsyncMock(return_value={}),
            reclaim_cache_space=AsyncMock(
                return_value={
                    "free_after": 10,
                    "target_free": 500,
                    "reclaimed_bytes": 25,
                }
            ),
        )

        with self.assertRaisesRegex(
            RuntimeError, r"storage I/O remains unavailable.*free=10 bytes, reclaimed=25 bytes"
        ):
            await _init_database_with_storage_recovery(database, cache)
        self.assertEqual(database.init_db.await_count, 1)

    async def test_retry_recoverable_storage_failure_has_concise_diagnostic(self):
        from sub2gen.main import _init_database_with_storage_recovery

        database = SimpleNamespace(
            init_db=AsyncMock(side_effect=sqlite3.OperationalError("disk I/O error"))
        )
        cache = SimpleNamespace(
            _cleanup_expired_files=AsyncMock(return_value={}),
            reclaim_cache_space=AsyncMock(
                return_value={
                    "free_after": 600,
                    "target_free": 500,
                    "reclaimed_bytes": 400,
                }
            ),
        )

        with self.assertRaisesRegex(
            RuntimeError, r"storage I/O remains unavailable.*free=600 bytes, reclaimed=400 bytes"
        ):
            await _init_database_with_storage_recovery(database, cache)
        self.assertEqual(database.init_db.await_count, 2)


NATIVE_IMAGE_2K = "gemini-3.1-flash-image-landscape-2k"
NATIVE_IMAGE_4K = "gemini-3.1-flash-image-landscape-4k"
NATIVE_VIDEO_1080P = "veo_3_1_t2v_1080p"
NATIVE_VIDEO_4K = "veo_3_1_t2v_4k"
NATIVE_NON_4K_ULTRA = "veo_3_1_t2v_fast_ultra"
GEMINIGEN_IMAGE_4K = "geminigen-nano-banana-pro-image-square-4k"


def make_tier_token(
    token_id: int,
    tier: str,
    *,
    is_active: bool = True,
    image_enabled: bool = True,
    video_enabled: bool = True,
):
    return SimpleNamespace(
        id=token_id,
        user_paygate_tier=tier,
        is_active=is_active,
        image_enabled=image_enabled,
        video_enabled=video_enabled,
    )


def make_tier_auth_context(*token_ids: int):
    return SimpleNamespace(key_id=99, allowed_accounts=set(token_ids))


class _TierNativeDb:
    def __init__(self, tokens):
        self.tokens = list(tokens)

    async def get_active_tokens(self):
        return list(self.tokens)


class _TierTokenManager:
    def __init__(self, tokens):
        self.tokens = list(tokens)

    async def get_active_tokens(self):
        return list(self.tokens)


class _TierLoadBalancer:
    def __init__(self, tokens):
        self.token_manager = _TierTokenManager(tokens)

    async def _get_token_load(self, token_id, **kwargs):
        return 0, None


def make_tier_handler(tokens):
    tokens = list(tokens)
    return SimpleNamespace(
        db=_TierNativeDb(tokens),
        load_balancer=_TierLoadBalancer(tokens),
    )


class _TierGeminiGenDb:
    async def get_geminigen_config(self):
        return SimpleNamespace(enabled=True, video_enabled=True)

    async def list_geminigen_accounts(self):
        return [SimpleNamespace(is_active=True, bearer_token="test-token")]


class _DisabledProviderDb:
    async def get_geminigen_config(self):
        return SimpleNamespace(enabled=False, video_enabled=False)

    async def get_runway_config(self):
        return SimpleNamespace(enabled=False)


def make_catalog_container(handler, geminigen_db=None):
    return SimpleNamespace(
        db=handler.db,
        geminigen_service=SimpleNamespace(db=geminigen_db or _DisabledProviderDb()),
        runway_service=SimpleNamespace(db=_DisabledProviderDb()),
    )


class Native4KCatalogUnitTests(unittest.TestCase):
    def test_4k_identifier_and_alias_filtering(self):
        self.assertTrue(is_native_4k_model(NATIVE_IMAGE_4K))
        self.assertTrue(is_native_4k_model(NATIVE_VIDEO_4K))
        self.assertFalse(is_native_4k_model(NATIVE_IMAGE_2K))
        self.assertFalse(is_native_4k_model(NATIVE_NON_4K_ULTRA))

        aliases = get_base_model_aliases(include_4k=False)
        self.assertIn("sizes: 2k", aliases["gemini-3.1-flash-image"])
        self.assertNotIn("4k", aliases["gemini-3.1-flash-image"])
        self.assertIn(NATIVE_NON_4K_ULTRA, aliases)
        self.assertFalse(any(is_native_4k_model(alias) for alias in aliases))


class Native4KCatalogAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_pro_only_catalog_hides_4k_but_keeps_2k_1080p_and_ultra_names(self):
        handler = make_tier_handler([make_tier_token(1, PAYGATE_TIER_ONE)])
        container = make_catalog_container(handler)
        openai_catalog = await routes._get_openai_model_catalog(handler.db)
        gemini_catalog = await routes._get_gemini_model_catalog(
            handler.db, container.geminigen_service
        )
        alias_payload = await routes.list_model_aliases(make_tier_auth_context(1), container)
        missing_model = await routes.get_gemini_model(
            NATIVE_IMAGE_4K,
            make_tier_auth_context(1),
            container,
        )

        openai_ids = {item["id"] for item in openai_catalog}
        alias_ids = {item["id"] for item in alias_payload["data"]}
        self.assertIn(NATIVE_IMAGE_2K, openai_ids)
        self.assertIn(NATIVE_VIDEO_1080P, openai_ids)
        self.assertIn(NATIVE_NON_4K_ULTRA, openai_ids)
        self.assertNotIn(NATIVE_IMAGE_4K, openai_ids)
        self.assertNotIn(NATIVE_VIDEO_4K, openai_ids)
        self.assertNotIn(NATIVE_IMAGE_4K, gemini_catalog)
        self.assertFalse(any(is_native_4k_model(alias) for alias in alias_ids))
        self.assertNotIn("4k", gemini_catalog["gemini-3.1-flash-image"])
        self.assertEqual(missing_model.status_code, 404)

    async def test_active_ultra_globally_enables_4k_catalog_entries(self):
        handler = make_tier_handler(
            [
                make_tier_token(1, PAYGATE_TIER_ONE),
                make_tier_token(2, PAYGATE_TIER_TWO),
            ]
        )
        container = make_catalog_container(handler)
        openai_catalog = await routes._get_openai_model_catalog(handler.db)
        gemini_catalog = await routes._get_gemini_model_catalog(
            handler.db, container.geminigen_service
        )
        alias_payload = await routes.list_model_aliases(make_tier_auth_context(1), container)
        model_payload = await routes.get_gemini_model(
            NATIVE_IMAGE_4K,
            make_tier_auth_context(1),
            container,
        )

        openai_ids = {item["id"] for item in openai_catalog}
        alias_ids = {item["id"] for item in alias_payload["data"]}
        self.assertIn(NATIVE_IMAGE_4K, openai_ids)
        self.assertIn(NATIVE_VIDEO_4K, openai_ids)
        self.assertIn(NATIVE_IMAGE_4K, gemini_catalog)
        self.assertIn(NATIVE_VIDEO_4K, alias_ids)
        self.assertIn("4k", gemini_catalog["gemini-3.1-flash-image"])
        self.assertEqual(model_payload["name"], f"models/{NATIVE_IMAGE_4K}")

    async def test_inactive_ultra_does_not_enable_4k(self):
        handler = make_tier_handler(
            [
                make_tier_token(1, PAYGATE_TIER_ONE),
                make_tier_token(2, PAYGATE_TIER_TWO, is_active=False),
            ]
        )
        catalog = await routes._get_openai_model_catalog(handler.db)

        self.assertFalse(any(is_native_4k_model(item["id"]) for item in catalog))

    async def test_native_filter_does_not_hide_geminigen_4k(self):
        handler = make_tier_handler([make_tier_token(1, PAYGATE_TIER_ONE)])
        geminigen_service = SimpleNamespace(db=_TierGeminiGenDb())
        container = make_catalog_container(handler, geminigen_service.db)
        payload = await routes.list_models(make_tier_auth_context(1), container)

        ids = {item["id"] for item in payload["data"]}
        self.assertNotIn(NATIVE_IMAGE_4K, ids)
        self.assertIn(GEMINIGEN_IMAGE_4K, ids)


class Native4KGenerationEligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_pro_only_assignment_rejects_explicit_and_alias_resolved_4k(self):
        request = SimpleNamespace(
            generationConfig=SimpleNamespace(
                aspectRatio="landscape",
                imageSize="4k",
            )
        )
        alias_resolved = resolve_model_name(
            "gemini-3.1-flash-image",
            request=request,
            model_config=MODEL_CONFIG,
        )
        self.assertEqual(alias_resolved, NATIVE_IMAGE_4K)

        handler = make_tier_handler([make_tier_token(1, PAYGATE_TIER_ONE)])
        for model in (NATIVE_IMAGE_4K, NATIVE_VIDEO_4K, alias_resolved):
            with self.subTest(model=model):
                with self.assertRaises(HTTPException) as raised:
                    await routes._select_random_active_project_for_api_key(
                        make_tier_auth_context(1),
                        model,
                        handler,
                    )
                self.assertEqual(raised.exception.status_code, 403)
                self.assertIn("active Ultra account assigned", raised.exception.detail)

    async def test_4k_selection_uses_only_assigned_ultra(self):
        handler = make_tier_handler(
            [
                make_tier_token(1, PAYGATE_TIER_ONE),
                make_tier_token(2, PAYGATE_TIER_TWO),
            ]
        )
        selected, project_id = await routes._select_random_active_project_for_api_key(
            make_tier_auth_context(1, 2),
            NATIVE_IMAGE_4K,
            handler,
        )

        self.assertEqual(selected, {2})
        self.assertIsNone(project_id)

    async def test_pro_assignment_can_generate_2k_and_1080p(self):
        handler = make_tier_handler([make_tier_token(1, PAYGATE_TIER_ONE)])
        for model in (NATIVE_IMAGE_2K, NATIVE_VIDEO_1080P):
            with self.subTest(model=model):
                selected, _ = await routes._select_random_active_project_for_api_key(
                    make_tier_auth_context(1),
                    model,
                    handler,
                )
                self.assertEqual(selected, {1})


class _NativeLogLoadBalancer:
    def __init__(self, token=None, events=None):
        self.token = token
        self.events = events if events is not None else []
        self.release_pending = AsyncMock()

    async def select_token(self, **kwargs):
        self.events.append("select_token")
        return self.token


def make_native_log_handler(token=None, events=None):
    handler = GenerationHandler.__new__(GenerationHandler)
    handler.flow_client = SimpleNamespace(
        clear_request_fingerprint=lambda: None,
        set_managed_api_key_id=lambda api_key_id: None,
        clear_managed_api_key_id=lambda: None,
    )
    handler.load_balancer = _NativeLogLoadBalancer(token=token, events=events)
    handler.token_manager = SimpleNamespace(ensure_valid_token=AsyncMock(return_value=None))
    handler._update_request_log_progress = AsyncMock()
    return handler


class NativeLiveRequestLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_stream_creates_processing_row_before_selection_and_reuses_it(self):
        events = []
        handler = make_native_log_handler(events=events)

        async def log_request(*args, **kwargs):
            events.append(f"log:{kwargs.get('status_code')}")
            return 41

        handler._log_request = AsyncMock(side_effect=log_request)
        chunks = [
            chunk
            async for chunk in handler.handle_generation(
                model="gemini-3.1-flash-image-landscape",
                prompt="test prompt",
                stream=False,
                api_key_id=7,
                poll_task_id="gen-public-job",
            )
        ]

        self.assertEqual(events, ["log:102", "select_token", "log:503"])
        self.assertTrue(chunks)
        initial = handler._log_request.await_args_list[0].kwargs
        final = handler._log_request.await_args_list[-1].kwargs
        self.assertEqual(initial["request_data"]["request_id"], "gen-public-job")
        self.assertEqual(initial["response_data"]["request_id"], "gen-public-job")
        self.assertEqual(final["log_id"], 41)
        self.assertEqual(final["status_text"], "failed")

    async def test_stream_creates_row_before_first_client_chunk(self):
        events = []
        handler = make_native_log_handler(events=events)

        async def log_request(*args, **kwargs):
            events.append(f"log:{kwargs.get('status_code')}")
            return 51

        handler._log_request = AsyncMock(side_effect=log_request)
        generation = handler.handle_generation(
            model="gemini-3.1-flash-image-landscape",
            prompt="test prompt",
            stream=True,
            api_key_id=7,
        )

        first_chunk = await anext(generation)
        self.assertIn("data:", first_chunk)
        self.assertEqual(events, ["log:102"])
        await generation.aclose()

    async def test_invalid_token_finalizes_existing_processing_row(self):
        token = SimpleNamespace(id=9, email="native@example.com")
        handler = make_native_log_handler(token=token)
        handler._log_request = AsyncMock(return_value=61)

        chunks = [
            chunk
            async for chunk in handler.handle_generation(
                model="gemini-3.1-flash-image-landscape",
                prompt="test prompt",
                stream=False,
                api_key_id=7,
            )
        ]

        self.assertTrue(chunks)
        self.assertEqual(handler._log_request.await_count, 2)
        final = handler._log_request.await_args_list[-1].kwargs
        self.assertEqual(final["log_id"], 61)
        self.assertEqual(final["status_code"], 503)
        self.assertEqual(final["status_text"], "failed")
        handler.load_balancer.release_pending.assert_awaited_once()

    async def test_token_selection_exception_finalizes_processing_row(self):
        handler = make_native_log_handler()
        handler._log_request = AsyncMock(return_value=66)
        handler.load_balancer.select_token = AsyncMock(
            side_effect=RuntimeError("selection exploded")
        )

        chunks = [
            chunk
            async for chunk in handler.handle_generation(
                model="gemini-3.1-flash-image-landscape",
                prompt="test prompt",
                stream=False,
                api_key_id=7,
            )
        ]

        self.assertTrue(chunks)
        self.assertEqual(handler._log_request.await_count, 2)
        final = handler._log_request.await_args_list[-1].kwargs
        self.assertEqual(final["log_id"], 66)
        self.assertEqual(final["status_code"], 500)
        self.assertEqual(final["status_text"], "failed")
        self.assertIn("selection exploded", final["response_data"]["error"])

    async def test_progress_payload_keeps_native_request_id(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.db = SimpleNamespace(update_request_log=AsyncMock())

        await handler._update_request_log_progress(
            {"id": 71, "request_id": "gen-live", "api_key_id": 7},
            token_id=9,
            status_text="video_polling",
            progress=55,
        )

        update = handler.db.update_request_log.await_args.kwargs
        payload = json.loads(update["response_body"])
        self.assertEqual(payload["request_id"], "gen-live")
        self.assertEqual(update["status_code"], 102)
        self.assertEqual(update["progress"], 55)

    async def test_non_stream_video_poll_persists_progress(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = SimpleNamespace(
            check_video_status=AsyncMock(return_value={"operations": []})
        )
        handler._update_request_log_progress = AsyncMock()
        handler._fail_video_task = AsyncMock()
        token = SimpleNamespace(id=9, at="at-token")
        generation_result = handler._create_generation_result()

        with patch(
            "sub2gen.services.generation_handler.config",
            SimpleNamespace(max_poll_attempts=1, poll_interval=0),
        ):
            chunks = [
                chunk
                async for chunk in handler._poll_video_result(
                    token,
                    "project-1",
                    [{"operation": {"name": "operation-1"}}],
                    False,
                    generation_result=generation_result,
                    request_log_state={"id": 81, "request_id": "gen-video"},
                )
            ]

        self.assertTrue(chunks)
        handler._update_request_log_progress.assert_awaited_once()
        progress = handler._update_request_log_progress.await_args.kwargs
        self.assertEqual(progress["status_text"], "video_polling")
        self.assertEqual(progress["progress"], 45)

    def test_admin_job_id_extraction_recognizes_native_request_id(self):
        self.assertEqual(
            _extract_log_job_id('{"status":"processing","request_id":"gen-native"}'),
            "gen-native",
        )
        self.assertEqual(
            _extract_log_job_id(
                {"performance": {"request_id": "gen-native-completed"}}
            ),
            "gen-native-completed",
        )


if __name__ == "__main__":
    unittest.main()
