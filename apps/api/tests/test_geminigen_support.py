import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, time as datetime_time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from sub2gen.core.database import Database
from sub2gen.core.config import config
from sub2gen.core.api_key_manager import AuthContext
from sub2gen.services.cloning_metadata_service import (
    _cloning_remaining_timeout,
    _ensure_meaningful_image_prompt,
    _normalize_image_prompt,
)
from sub2gen.services.geminigen_service import (
    GEMINIGEN_CAPACITY_ERROR_CODE,
    GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
    GeminiGenService,
    GeminiGenUpstreamError,
)
from sub2gen.services.llm_provider_chain import LlmProviderChain, extract_non_empty_json_object
from sub2gen.services.runway_service import RunwayService
from sub2gen.api import routes
from sub2gen.core.geminigen_manifest import GEMINIGEN_MODEL_BY_ID, GEMINIGEN_MODEL_MANIFEST
from sub2gen.core.models import GeminiGenAccount, GeminiGenTask, RequestLog, RunwayAccount, RunwayModel, RunwayTask, Token
from sub2gen.core.storage_errors import (
    is_sqlite_recoverable_storage_error,
    is_sqlite_storage_full_error,
    sqlite_operational_error_handler,
)
from sub2gen.core.studio_model_catalog import geminigen_studio_metadata, native_studio_metadata


def test_blank_cloning_prompt_is_rejected():
    blank = _normalize_image_prompt({})
    try:
        _ensure_meaningful_image_prompt(blank)
    except HTTPException as exc:
        assert exc.status_code == 422
        assert "blank cloning prompt" in str(exc.detail)
    else:
        raise AssertionError("blank cloning prompt was accepted")


def test_meaningful_cloning_prompt_is_preserved():
    prompt = _normalize_image_prompt({"scene": "Clinician closing a hyperbaric oxygen chamber door"})
    out = _ensure_meaningful_image_prompt(prompt)
    assert out["scene"] == "Clinician closing a hyperbaric oxygen chamber door"


def test_empty_provider_json_content_is_rejected():
    try:
        extract_non_empty_json_object("", "Gemini")
    except HTTPException as exc:
        assert exc.status_code == 422
        assert "Gemini returned empty JSON content" in str(exc.detail)
    else:
        raise AssertionError("empty provider JSON content was accepted")


def test_non_empty_provider_json_content_still_parses():
    assert extract_non_empty_json_object('{"scene":"x"}') == {"scene": "x"}


def test_non_retryable_model_output_does_not_try_fallback_model():
    class Chain(LlmProviderChain):
        def __init__(self):
            self.calls = []

        async def _invoke_gemini(self, model, *args, **kwargs):
            self.calls.append(model)
            raise HTTPException(status_code=422, detail="Gemini returned empty JSON content")

    chain = Chain()
    try:
        asyncio.run(
            chain.invoke_model_json(
                provider="gemini_native",
                model="primary-model",
                fallback_models=["fallback-model"],
                prompt_text="prompt",
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("non-retryable model output was accepted")
    assert chain.calls == ["primary-model"]


def test_retryable_model_error_tries_fallback_model():
    class Chain(LlmProviderChain):
        def __init__(self):
            self.calls = []

        async def _invoke_gemini(self, model, *args, **kwargs):
            self.calls.append(model)
            if model == "primary-model":
                raise HTTPException(status_code=502, detail="temporary upstream failure")
            return {"scene": "fallback worked"}

    chain = Chain()
    out = asyncio.run(
        chain.invoke_model_json(
            provider="gemini_native",
            model="primary-model",
            fallback_models=["fallback-model"],
            prompt_text="prompt",
        )
    )
    assert out == {"scene": "fallback worked"}
    assert chain.calls == ["primary-model", "fallback-model"]


def test_cloning_deadline_returns_controlled_timeout_before_proxy_limit():
    try:
        _cloning_remaining_timeout(time.monotonic() - 1, 60.0)
    except HTTPException as exc:
        assert exc.status_code == 504
        assert "deadline exceeded" in str(exc.detail)
    else:
        raise AssertionError("expired cloning deadline did not fail")


def test_extract_artifact_urls_prefers_final_download_url_over_preview():
    payload = {
        "generated_image": {
            "image_url": "https://cdn.example/preview.jpg",
        },
        "file_download_url": "https://cdn.example/final.png",
    }

    assert GeminiGenService.extract_artifact_urls(payload, "image") == [
        "https://cdn.example/final.png"
    ]


def test_extract_artifact_urls_dedupes_signed_urls_for_same_artifact():
    payload = {
        "generated_image": {
            "image_url": "https://cdn.example/preview.jpg",
        },
        "result": {
            "file_download_url": "https://cdn.example/final.png?Expires=1&Signature=abc",
        },
        "data": [
            {
                "download_url": "https://cdn.example/final.png?Expires=2&Signature=def",
            }
        ],
    }

    assert GeminiGenService.extract_artifact_urls(payload, "image") == [
        "https://cdn.example/final.png?Expires=1&Signature=abc"
    ]


def test_extract_artifact_urls_preserves_distinct_multi_outputs():
    payload = {
        "data": [
            {"file_download_url": "https://cdn.example/result-1.png?Signature=abc"},
            {"file_download_url": "https://cdn.example/result-2.png?Signature=def"},
        ],
    }

    assert GeminiGenService.extract_artifact_urls(payload, "image") == [
        "https://cdn.example/result-1.png?Signature=abc",
        "https://cdn.example/result-2.png?Signature=def",
    ]


def _local_date_as_utc_naive(day):
    local_tz = datetime.now().astimezone().tzinfo
    local_noon = datetime.combine(day, datetime_time(hour=12), tzinfo=local_tz)
    return local_noon.astimezone(timezone.utc).replace(tzinfo=None)


def test_dashboard_stats_combine_flow_and_terminal_geminigen_tasks():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            today = datetime.now().astimezone().date()
            today_completed_at = _local_date_as_utc_naive(today)
            old_completed_at = _local_date_as_utc_naive(today - timedelta(days=2))

            token_id = await db.add_token(Token(st="session", email="flow@example.com"))
            async with db._connect(write=True) as conn:
                await conn.execute(
                    """
                    UPDATE token_stats
                    SET image_count = 4, video_count = 3, error_count = 2,
                        today_image_count = 2, today_video_count = 1, today_error_count = 1,
                        today_date = ?
                    WHERE token_id = ?
                    """,
                    (today.isoformat(), token_id),
                )
                await conn.commit()

            tasks = [
                ("image-today", "image", "completed", today_completed_at),
                ("image-old", "image", "completed", old_completed_at),
                ("video-today", "video", "completed", today_completed_at),
                ("error-today", "image", "failed", today_completed_at),
                ("error-old", "video", "failed", old_completed_at),
            ]
            for job_id, kind, status, completed_at in tasks:
                await db.create_geminigen_task(
                    GeminiGenTask(
                        job_id=job_id,
                        public_model_id=f"test-{kind}-model",
                        kind=kind,
                        endpoint_type="veo-video" if kind == "video" else "imagen",
                        status=status,
                        completed_at=completed_at,
                    )
                )

            return await db.get_dashboard_stats()
        finally:
            os.unlink(tmp.name)

    stats = asyncio.run(run())

    assert stats["total_images"] == 6
    assert stats["total_videos"] == 4
    assert stats["total_errors"] == 4
    assert stats["today_images"] == 3
    assert stats["today_videos"] == 2
    assert stats["today_errors"] == 2


def test_dashboard_stats_exclude_non_terminal_and_cancelled_geminigen_tasks():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            completed_at = _local_date_as_utc_naive(datetime.now().astimezone().date())
            tasks = [
                ("queued", "image", "queued", None),
                ("processing", "video", "processing", None),
                ("cancelled", "image", "cancelled", completed_at),
            ]
            for job_id, kind, status, terminal_at in tasks:
                await db.create_geminigen_task(
                    GeminiGenTask(
                        job_id=job_id,
                        public_model_id=f"test-{kind}-model",
                        kind=kind,
                        endpoint_type="veo-video" if kind == "video" else "imagen",
                        status=status,
                        completed_at=terminal_at,
                    )
                )
            return await db.get_dashboard_stats()
        finally:
            os.unlink(tmp.name)

    stats = asyncio.run(run())

    assert stats["total_images"] == 0
    assert stats["total_videos"] == 0
    assert stats["total_errors"] == 0
    assert stats["today_images"] == 0
    assert stats["today_videos"] == 0
    assert stats["today_errors"] == 0
    assert stats["total_metadata"] == 0
    assert stats["today_metadata"] == 0


def test_geminigen_account_generation_stats_count_completed_per_account():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            first_id = await db.create_geminigen_account(label="primary", raw_cookie="", bearer_token="token-1")
            second_id = await db.create_geminigen_account(label="secondary", raw_cookie="", bearer_token="token-2")
            today = datetime.now().astimezone().date()
            today_completed_at = _local_date_as_utc_naive(today)
            old_completed_at = _local_date_as_utc_naive(today - timedelta(days=2))
            tasks = [
                ("first-image-today", first_id, "image", "completed", today_completed_at),
                ("first-image-old", first_id, "image", "completed", old_completed_at),
                ("first-video-today", first_id, "video", "completed", today_completed_at),
                ("first-failed", first_id, "image", "failed", today_completed_at),
                ("first-cancelled", first_id, "video", "cancelled", today_completed_at),
                ("first-queued", first_id, "image", "queued", None),
                ("second-video-old", second_id, "video", "completed", old_completed_at),
            ]
            for job_id, account_id, kind, status, completed_at in tasks:
                await db.create_geminigen_task(
                    GeminiGenTask(
                        job_id=job_id,
                        account_id=account_id,
                        public_model_id=f"test-{kind}-model",
                        kind=kind,
                        endpoint_type="veo-video" if kind == "video" else "imagen",
                        status=status,
                        completed_at=completed_at,
                    )
                )
            return await db.get_geminigen_account_generation_stats([first_id, second_id])
        finally:
            os.unlink(tmp.name)

    stats = asyncio.run(run())

    assert stats[1] == {
        "image_generated_today": 1,
        "image_generated_total": 2,
        "video_generated_today": 1,
        "video_generated_total": 1,
    }
    assert stats[2] == {
        "image_generated_today": 0,
        "image_generated_total": 0,
        "video_generated_today": 0,
        "video_generated_total": 1,
    }


def test_dashboard_stats_include_durable_metadata_counts():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            await db.increment_operation_stat("adobe:metadata", success=True)
            await db.increment_operation_stat("adobe:metadata", success=True)
            await db.increment_operation_stat("adobe:metadata", success=False)
            return await db.get_dashboard_stats()
        finally:
            os.unlink(tmp.name)

    stats = asyncio.run(run())

    assert stats["total_metadata"] == 2
    assert stats["today_metadata"] == 2
    assert stats["metadata_errors"] == 1
    assert stats["today_metadata_errors"] == 1


def test_metadata_stats_survive_request_log_retention_cleanup():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            await db.increment_operation_stat("adobe:metadata", success=True)
            old_created = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
            async with db._connect(write=True) as conn:
                await conn.execute(
                    """
                    INSERT INTO request_logs (operation, status_code, duration, created_at, updated_at)
                    VALUES ('adobe:metadata', 200, 0.1, ?, ?)
                    """,
                    (old_created, old_created),
                )
                await conn.commit()

            deleted = await db.delete_request_logs_older_than(days=3)
            stats = await db.get_dashboard_stats()
            return deleted, stats
        finally:
            os.unlink(tmp.name)

    deleted, stats = asyncio.run(run())

    assert deleted == 1
    assert stats["total_metadata"] == 1
    assert stats["today_metadata"] == 1


def _sample_geminigen_me_payload():
    return {
        "id": 301913,
        "uuid": "56fd932a-d887-11f0-b3aa-6673a75f95e0",
        "email": "huzaifaphantompak@gmail.com",
        "full_name": "Muhammad Huzaifa",
        "is_active": True,
        "user_credit": {
            "plan_credit": 1,
            "purchased_credit": 0,
            "locked_credit": 0,
            "available_credit": 1,
            "subscription_credit": 0,
        },
        "user_plan": {
            "product": {"name": "Premium"},
            "expire_at": "2025-12-13T23:59:59",
        },
        "user_benefits": [
            {
                "id": 22869,
                "product": {"name": "Image Generation 30 days"},
                "expire_at": "2026-07-27T10:15:08",
                "is_active": True,
                "estimated_remaining": 0,
            },
            {
                "id": 27961,
                "product": {"name": "Grok 30 days"},
                "expire_at": "2026-07-05T00:07:03",
                "is_active": False,
                "estimated_remaining": 0,
            },
        ],
        "remaining_bulk_videos": 100,
        "remaining_daily_videos": None,
        "remaining_grok_max_daily_videos": None,
        "remaining_grok_max_daily_720p_videos": None,
        "remaining_grok_max_daily_10s_videos": None,
    }


def _sample_geminigen_quota_payload(path: str):
    return {
        "/api/user/quota/image-gen": {
            "is_image_gen_max": True,
            "is_image_premium": True,
            "concurrent_streams": 0,
            "remaining_image_gen_max_daily_images": 0,
            "remaining_free_daily_images": 0,
        },
        "/api/user/quota/grok-image": {
            "is_grok_image_max": True,
            "is_image_premium": True,
            "concurrent_streams": 0,
            "remaining_grok_image_max_daily_images": 50,
            "remaining_free_daily_images": 0,
        },
        "/api/user/quota/grok-max": {
            "is_grok_max": False,
            "concurrent_streams": 0,
            "remaining_grok_max_daily_videos": 0,
            "remaining_grok_max_daily_720p_videos": 0,
            "remaining_grok_max_daily_10s_videos": 0,
            "remaining_grok_max_daily_15s_videos": 0,
        },
    }[path]


def test_geminigen_me_profile_parser_normalizes_user_credit_plan_and_benefits():
    parsed = GeminiGenService.parse_me_profile(_sample_geminigen_me_payload())
    benefits = json.loads(parsed["active_benefits_json"])

    assert parsed["profile_user_id"] == 301913
    assert parsed["profile_uuid"] == "56fd932a-d887-11f0-b3aa-6673a75f95e0"
    assert parsed["profile_email"] == "huzaifaphantompak@gmail.com"
    assert parsed["profile_full_name"] == "Muhammad Huzaifa"
    assert parsed["profile_is_active"] is True
    assert parsed["available_credit"] == 1
    assert parsed["plan_credit"] == 1
    assert parsed["plan_name"] == "Premium"
    assert parsed["plan_expire_at"] == datetime(2025, 12, 13, 23, 59, 59)
    assert parsed["remaining_bulk_videos"] == 100
    assert parsed["remaining_daily_videos"] is None
    assert benefits == [
        {
            "id": 22869,
            "name": "Image Generation 30 days",
            "expire_at": "2026-07-27T10:15:08",
            "is_active": True,
            "estimated_remaining": 0,
        }
    ]


def test_geminigen_quota_parsers_normalize_snapgen_payloads():
    image_gen = GeminiGenService.parse_image_gen_quota(
        _sample_geminigen_quota_payload("/api/user/quota/image-gen")
    )
    grok_image = GeminiGenService.parse_grok_image_quota(
        _sample_geminigen_quota_payload("/api/user/quota/grok-image")
    )
    grok_max = GeminiGenService.parse_grok_max_quota(
        _sample_geminigen_quota_payload("/api/user/quota/grok-max")
    )

    assert image_gen["is_image_gen_max"] is True
    assert image_gen["remaining_image_gen_max_daily_images"] == 0
    assert grok_image["remaining_grok_image_max_daily_images"] == 50
    assert grok_max["remaining_grok_max_daily_15s_videos"] == 0


def test_geminigen_and_snapgen_host_normalization_updates_origin_headers():
    async def run():
        service = GeminiGenService.__new__(GeminiGenService)

        async def guard(**_kwargs):
            return "guard"

        service._compute_x_guard_id = guard
        account = GeminiGenAccount(bearer_token="token")
        gemini_headers = await service._headers(
            account,
            "/api/user/quota/image-gen",
            base_url="https://api.geminigen.ai",
        )
        snap_headers = await service._headers(
            account,
            "/api/user/quota/image-gen",
            base_url="https://snapgen.ai",
        )
        return gemini_headers, snap_headers

    gemini_headers, snap_headers = asyncio.run(run())

    assert GeminiGenService._api_base_url("https://geminigen.ai") == "https://api.geminigen.ai"
    assert GeminiGenService._api_base_url("https://snapgen.ai") == "https://api.snapgen.ai"
    assert gemini_headers["Origin"] == "https://geminigen.ai"
    assert gemini_headers["Referer"] == "https://geminigen.ai/"
    assert snap_headers["Origin"] == "https://snapgen.ai"
    assert snap_headers["Referer"] == "https://snapgen.ai/"
    assert snap_headers["x-guard-id"] == "guard"


def test_geminigen_account_profile_sync_stores_me_payload():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            account_id = await db.create_geminigen_account(label="primary", raw_cookie="", bearer_token="token")
            service = GeminiGenService(db, None)

            async def fetch(_account, _base_url):
                return _sample_geminigen_me_payload()

            async def fetch_json(_account, _base_url, path):
                return _sample_geminigen_quota_payload(path)

            service._fetch_me_profile_payload = fetch
            service._fetch_account_json_payload = fetch_json
            result = await service.sync_account_profile(account_id)
            account = await db.get_geminigen_account(account_id)
            blocked_imagen = await db.acquire_geminigen_account("image", endpoint_type="imagen")
            allowed_grok = await db.acquire_geminigen_account("image", endpoint_type="grok-image")
            return result, account, blocked_imagen, allowed_grok
        finally:
            os.unlink(tmp.name)

    result, account, blocked_imagen, allowed_grok = asyncio.run(run())

    assert result["success"] is True
    assert account.profile_email == "huzaifaphantompak@gmail.com"
    assert account.profile_full_name == "Muhammad Huzaifa"
    assert account.available_credit == 1
    assert account.plan_name == "Premium"
    assert account.remaining_bulk_videos == 100
    assert account.profile_sync_status == "healthy"
    assert account.profile_sync_error == ""
    assert account.profile_synced_at is not None
    assert account.quota_sync_status == "healthy"
    assert account.remaining_image_gen_max_daily_images == 0
    assert account.remaining_grok_image_max_daily_images == 50
    assert account.image_gen_daily_limit_reset_at is not None
    assert account.grok_image_daily_limit_reset_at is None
    assert blocked_imagen is None
    assert allowed_grok is not None


def test_geminigen_account_profile_sync_failure_preserves_saved_profile():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            account_id = await db.create_geminigen_account(label="primary", raw_cookie="", bearer_token="token")
            await db.update_geminigen_account(
                account_id,
                profile_email="saved@example.com",
                available_credit=7,
                profile_sync_status="healthy",
            )
            service = GeminiGenService(db, None)

            async def fetch(_account, _base_url):
                raise RuntimeError("upstream unavailable")

            service._fetch_me_profile_payload = fetch
            result = await service.sync_account_profile(account_id)
            account = await db.get_geminigen_account(account_id)
            return result, account
        finally:
            os.unlink(tmp.name)

    result, account = asyncio.run(run())

    assert result["success"] is False
    assert account.profile_email == "saved@example.com"
    assert account.available_credit == 7
    assert account.profile_sync_status == "failed"
    assert account.profile_sync_error == "upstream unavailable"
    assert account.profile_synced_at is not None


def test_geminigen_quota_sync_is_best_effort_and_preserves_failed_bucket():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            account_id = await db.create_geminigen_account(label="primary", raw_cookie="", bearer_token="token")
            await db.update_geminigen_account(
                account_id,
                is_image_gen_max=True,
                remaining_image_gen_max_daily_images=12,
            )
            service = GeminiGenService(db, None)

            async def fetch_me(_account, _base_url):
                return _sample_geminigen_me_payload()

            async def fetch_json(_account, _base_url, path):
                if path == "/api/user/quota/image-gen":
                    raise RuntimeError("quota unavailable")
                return _sample_geminigen_quota_payload(path)

            service._fetch_me_profile_payload = fetch_me
            service._fetch_account_json_payload = fetch_json
            result = await service.sync_account_profile(account_id)
            account = await db.get_geminigen_account(account_id)
            return result, account
        finally:
            os.unlink(tmp.name)

    result, account = asyncio.run(run())

    assert result["success"] is True
    assert result["quota_status"] == "partial"
    assert "image_gen: quota unavailable" in result["quota_error"]
    assert account.profile_sync_status == "healthy"
    assert account.remaining_image_gen_max_daily_images == 12
    assert account.remaining_grok_image_max_daily_images == 50
    assert account.quota_sync_status == "partial"


def test_geminigen_account_profile_stale_uses_one_hour_ttl():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fresh = GeminiGenAccount(profile_synced_at=now - timedelta(minutes=30))
    stale = GeminiGenAccount(profile_synced_at=now - timedelta(hours=2))
    missing = GeminiGenAccount()

    assert GeminiGenService.is_account_profile_stale(fresh) is False
    assert GeminiGenService.is_account_profile_stale(stale) is True
    assert GeminiGenService.is_account_profile_stale(missing) is True


def test_geminigen_image_capacity_uses_sum_of_account_slots_not_default_global_cap():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            account_ids = [
                await db.create_geminigen_account(label="primary", raw_cookie="", bearer_token="token-1", image_concurrency=5),
                await db.create_geminigen_account(label="secondary", raw_cookie="", bearer_token="token-2", image_concurrency=5),
            ]

            acquired = [await db.acquire_geminigen_account("image") for _ in range(10)]
            blocked = await db.acquire_geminigen_account("image")
            accounts = [await db.get_geminigen_account(account_id) for account_id in account_ids]
            return acquired, blocked, accounts
        finally:
            os.unlink(tmp.name)

    acquired, blocked, accounts = asyncio.run(run())

    assert len([account for account in acquired if account is not None]) == 10
    assert blocked is None
    assert sorted(account.image_in_flight for account in accounts) == [5, 5]
    assert all(account.image_in_flight <= account.image_concurrency for account in accounts)


def test_geminigen_disabled_account_is_excluded_from_pool_capacity():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            enabled_ids = [
                await db.create_geminigen_account(
                    label=f"enabled-{index}",
                    raw_cookie="",
                    bearer_token=f"token-{index}",
                    image_concurrency=5,
                )
                for index in range(2)
            ]
            disabled_id = await db.create_geminigen_account(
                label="disabled",
                raw_cookie="",
                bearer_token="disabled-token",
                is_active=False,
                image_concurrency=5,
            )

            acquired = [await db.acquire_geminigen_account("image") for _ in range(10)]
            blocked = await db.acquire_geminigen_account("image")
            accounts = {
                account_id: await db.get_geminigen_account(account_id)
                for account_id in [*enabled_ids, disabled_id]
            }
            return acquired, blocked, accounts, enabled_ids, disabled_id
        finally:
            os.unlink(tmp.name)

    acquired, blocked, accounts, enabled_ids, disabled_id = asyncio.run(run())

    assert len([account for account in acquired if account is not None]) == 10
    assert {account.id for account in acquired} == set(enabled_ids)
    assert blocked is None
    assert sorted(accounts[account_id].image_in_flight for account_id in enabled_ids) == [5, 5]
    assert accounts[disabled_id].image_in_flight == 0


def test_geminigen_account_delete_preserves_task_history_without_foreign_key_error():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            account_id = await db.create_geminigen_account(
                label="delete-me",
                raw_cookie="",
                bearer_token="token",
            )
            await db.create_geminigen_task(
                GeminiGenTask(
                    job_id="geminigen-delete-history",
                    account_id=account_id,
                    public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
                    kind="image",
                    endpoint_type="imagen",
                    status="completed",
                )
            )

            deleted = await db.delete_geminigen_account(account_id)
            account = await db.get_geminigen_account(account_id)
            task = await db.get_geminigen_task("geminigen-delete-history")
            return deleted, account, task
        finally:
            os.unlink(tmp.name)

    deleted, account, task = asyncio.run(run())

    assert deleted is True
    assert account is None
    assert task is not None
    assert task.account_id is None


def test_geminigen_admin_delete_cancels_assigned_jobs_before_removing_account():
    async def run():
        from sub2gen.api import admin as admin_routes

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            account_id = await db.create_geminigen_account(
                label="delete-active",
                raw_cookie="",
                bearer_token="token",
            )
            await db.update_geminigen_account(account_id, image_in_flight=1)
            await db.create_geminigen_task(
                GeminiGenTask(
                    job_id="geminigen-delete-active",
                    account_id=account_id,
                    public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
                    kind="image",
                    endpoint_type="imagen",
                    status="processing",
                )
            )
            result = await admin_routes.delete_geminigen_account(
                account_id,
                token="admin-token",
                container=SimpleNamespace(db=db),
            )
            account = await db.get_geminigen_account(account_id)
            task = await db.get_geminigen_task("geminigen-delete-active")
            return result, account, task
        finally:
            os.unlink(tmp.name)

    result, account, task = asyncio.run(run())

    assert result["success"] is True
    assert result["tasks_cleared"] == 1
    assert account is None
    assert task.status == "cancelled"
    assert task.account_id is None


def test_geminigen_video_capacity_is_independent_from_image_slots():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            account_ids = [
                await db.create_geminigen_account(
                    label="primary",
                    raw_cookie="",
                    bearer_token="token-1",
                    image_concurrency=1,
                    video_concurrency=5,
                ),
                await db.create_geminigen_account(
                    label="secondary",
                    raw_cookie="",
                    bearer_token="token-2",
                    image_concurrency=1,
                    video_concurrency=5,
                ),
            ]

            videos = [await db.acquire_geminigen_account("video") for _ in range(10)]
            video_blocked = await db.acquire_geminigen_account("video")
            images = [await db.acquire_geminigen_account("image") for _ in range(2)]
            image_blocked = await db.acquire_geminigen_account("image")
            accounts = [await db.get_geminigen_account(account_id) for account_id in account_ids]
            return videos, video_blocked, images, image_blocked, accounts
        finally:
            os.unlink(tmp.name)

    videos, video_blocked, images, image_blocked, accounts = asyncio.run(run())

    assert len([account for account in videos if account is not None]) == 10
    assert video_blocked is None
    assert len([account for account in images if account is not None]) == 2
    assert image_blocked is None
    assert sorted(account.video_in_flight for account in accounts) == [5, 5]
    assert sorted(account.image_in_flight for account in accounts) == [1, 1]


def test_geminigen_acquire_respects_excluded_account_cooldown():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            first_id = await db.create_geminigen_account(label="primary", raw_cookie="", bearer_token="token-1", image_concurrency=5)
            second_id = await db.create_geminigen_account(label="secondary", raw_cookie="", bearer_token="token-2", image_concurrency=5)

            acquired = await db.acquire_geminigen_account("image", excluded_account_ids=[first_id])
            first = await db.get_geminigen_account(first_id)
            second = await db.get_geminigen_account(second_id)
            return acquired, first, second
        finally:
            os.unlink(tmp.name)

    acquired, first, second = asyncio.run(run())

    assert acquired.id == second.id
    assert first.image_in_flight == 0
    assert second.image_in_flight == 1


def test_geminigen_daily_limit_is_persisted_per_endpoint_and_expires():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            db = Database(tmp.name)
            await db.init_db()
            account_id = await db.create_geminigen_account(
                label="daily-limited",
                raw_cookie="",
                bearer_token="token",
                image_concurrency=1,
                video_concurrency=1,
            )
            future_reset = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)
            await db.update_geminigen_account(
                account_id,
                image_gen_daily_limit_reset_at=future_reset,
            )

            blocked_image = await db.acquire_geminigen_account("image", endpoint_type="imagen")
            allowed_grok = await db.acquire_geminigen_account("image", endpoint_type="grok-image")
            await db.release_geminigen_account(account_id, "image")
            allowed_video = await db.acquire_geminigen_account("video")
            await db.release_geminigen_account(account_id, "video")

            reopened = Database(tmp.name)
            persisted = await reopened.get_geminigen_account(account_id)
            state = await reopened.get_geminigen_daily_limit_pool_state("image", endpoint_type="imagen")

            past_reset = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            await reopened.update_geminigen_account(
                account_id,
                image_gen_daily_limit_reset_at=past_reset,
            )
            allowed_after_reset = await reopened.acquire_geminigen_account("image", endpoint_type="imagen")
            return blocked_image, allowed_grok, allowed_video, persisted, state, allowed_after_reset
        finally:
            os.unlink(tmp.name)

    blocked_image, allowed_grok, allowed_video, persisted, state, allowed_after_reset = asyncio.run(run())

    assert blocked_image is None
    assert allowed_grok is not None
    assert allowed_video is not None
    assert persisted.image_gen_daily_limit_reset_at is not None
    assert state["account_count"] == 1
    assert state["daily_limited_count"] == 1
    assert allowed_after_reset is not None


def test_geminigen_cached_quota_decrement_is_atomic_and_bucket_specific():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            account_id = await db.create_geminigen_account(
                label="quota",
                raw_cookie="",
                bearer_token="token",
                image_concurrency=5,
            )
            await db.update_geminigen_account(
                account_id,
                is_image_gen_max=True,
                remaining_image_gen_max_daily_images=2,
                image_gen_quota_synced_at=datetime.now(timezone.utc).replace(tzinfo=None),
                is_grok_image_max=True,
                remaining_grok_image_max_daily_images=50,
            )
            decremented = await asyncio.gather(
                db.decrement_geminigen_cached_quota(account_id, "imagen"),
                db.decrement_geminigen_cached_quota(account_id, "imagen"),
            )
            account = await db.get_geminigen_account(account_id)
            return decremented, account
        finally:
            os.unlink(tmp.name)

    decremented, account = asyncio.run(run())

    assert decremented == [True, True]
    assert account.remaining_image_gen_max_daily_images == 0
    assert account.remaining_grok_image_max_daily_images == 50
    assert account.image_gen_daily_limit_reset_at is not None
    assert account.grok_image_daily_limit_reset_at is None


def test_geminigen_cached_quota_is_not_decremented_when_sync_is_stale():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            account_id = await db.create_geminigen_account(label="stale", raw_cookie="", bearer_token="token")
            await db.update_geminigen_account(
                account_id,
                is_grok_image_max=True,
                remaining_grok_image_max_daily_images=10,
                grok_image_quota_synced_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),
            )
            decremented = await db.decrement_geminigen_cached_quota(account_id, "grok-image")
            account = await db.get_geminigen_account(account_id)
            return decremented, account
        finally:
            os.unlink(tmp.name)

    decremented, account = asyncio.run(run())

    assert decremented is False
    assert account.remaining_grok_image_max_daily_images == 10
    assert account.grok_image_daily_limit_reset_at is None


def test_geminigen_task_daily_limit_error_fields_are_persisted():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            db = Database(tmp.name)
            await db.init_db()
            retry_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
            await db.create_geminigen_task(
                GeminiGenTask(
                    job_id="geminigen-daily-error-fields",
                    public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
                    kind="image",
                    endpoint_type="imagen",
                    status="failed",
                    error_message="daily limit",
                    error_code=GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                    retry_at=retry_at,
                )
            )
            return await db.get_geminigen_task("geminigen-daily-error-fields")
        finally:
            os.unlink(tmp.name)

    task = asyncio.run(run())

    assert task.error_code == GEMINIGEN_DAILY_LIMIT_ERROR_CODE
    assert task.retry_at is not None


def test_geminigen_daily_limit_columns_are_added_to_existing_tables():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            connection = sqlite3.connect(tmp.name)
            try:
                connection.execute(
                    "CREATE TABLE geminigen_accounts (id INTEGER PRIMARY KEY, image_daily_limit_reset_at TIMESTAMP)"
                )
                connection.execute("CREATE TABLE geminigen_tasks (id INTEGER PRIMARY KEY, job_id TEXT)")
                connection.execute(
                    "INSERT INTO geminigen_accounts (id, image_daily_limit_reset_at) VALUES (1, '2099-01-01 00:00:00')"
                )
                connection.commit()
            finally:
                connection.close()
            db = Database(tmp.name)
            async with db._connect(write=True) as connection:
                await db._ensure_geminigen_tables(connection)
                await connection.commit()
            connection = sqlite3.connect(tmp.name)
            try:
                account_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(geminigen_accounts)").fetchall()
                }
                task_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(geminigen_tasks)").fetchall()
                }
                migrated_reset = connection.execute(
                    "SELECT image_gen_daily_limit_reset_at FROM geminigen_accounts WHERE id = 1"
                ).fetchone()[0]
            finally:
                connection.close()
            return account_columns, task_columns, migrated_reset
        finally:
            os.unlink(tmp.name)

    account_columns, task_columns, migrated_reset = asyncio.run(run())

    assert {
        "image_gen_daily_limit_reset_at",
        "grok_image_daily_limit_reset_at",
        "video_daily_limit_reset_at",
    } <= account_columns
    assert {"error_code", "retry_at"} <= task_columns
    assert migrated_reset == "2099-01-01 00:00:00"


def test_geminigen_admin_account_payload_includes_profile_fields():
    from sub2gen.api.admin import _geminigen_account_payload

    account = GeminiGenAccount(
        id=1,
        label="primary",
        bearer_token="secret-token",
        profile_email="huzaifaphantompak@gmail.com",
        profile_full_name="Muhammad Huzaifa",
        available_credit=1,
        plan_name="Premium",
        plan_expire_at=datetime(2025, 12, 13, 23, 59, 59),
        active_benefits_json=json.dumps([{"name": "Image Generation 30 days"}]),
        remaining_bulk_videos=100,
        image_gen_daily_limit_reset_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2),
        is_image_gen_max=True,
        remaining_image_gen_max_daily_images=0,
        is_grok_image_max=True,
        remaining_grok_image_max_daily_images=50,
        profile_synced_at=datetime(2026, 7, 7, 12, 0, 0),
        profile_sync_status="healthy",
    )

    payload = _geminigen_account_payload(account)

    assert payload["profile_email"] == "huzaifaphantompak@gmail.com"
    assert payload["profile_full_name"] == "Muhammad Huzaifa"
    assert payload["available_credit"] == 1
    assert payload["plan_name"] == "Premium"
    assert payload["plan_expire_at"] == "2025-12-13T23:59:59"
    assert payload["active_benefits"] == [{"name": "Image Generation 30 days"}]
    assert payload["remaining_bulk_videos"] == 100
    assert payload["image_gen_daily_limited"] is True
    assert payload["grok_image_daily_limited"] is False
    assert payload["video_daily_limited"] is False
    assert payload["image_gen_daily_limit_reset_at"].endswith("Z")
    assert payload["image_gen_quota"] == {"tier": "max", "remaining": 0, "used": 200, "max": 200}
    assert payload["grok_image_quota"] == {"tier": "max", "remaining": 50, "used": 0, "max": 50}
    assert payload["profile_synced_at"] == "2026-07-07T12:00:00"
    assert payload["profile_sync_status"] == "healthy"
    assert payload["image_generated_today"] == 0
    assert payload["image_generated_total"] == 0
    assert payload["video_generated_today"] == 0
    assert payload["video_generated_total"] == 0


def test_geminigen_admin_account_payload_includes_generation_stats():
    from sub2gen.api.admin import _geminigen_account_payload

    account = GeminiGenAccount(id=1, label="primary", bearer_token="secret-token")
    payload = _geminigen_account_payload(
        account,
        {
            "image_generated_today": 2,
            "image_generated_total": 10,
            "video_generated_today": 1,
            "video_generated_total": 4,
        },
    )

    assert payload["image_generated_today"] == 2
    assert payload["image_generated_total"] == 10
    assert payload["video_generated_today"] == 1
    assert payload["video_generated_total"] == 4


def test_geminigen_admin_account_payload_uses_remaining_only_for_unknown_caps():
    from sub2gen.api.admin import _geminigen_account_payload

    account = GeminiGenAccount(
        id=1,
        label="free",
        bearer_token="token",
        is_image_gen_max=False,
        remaining_image_gen_free_daily_images=7,
        is_grok_image_max=False,
        remaining_grok_image_free_daily_images=3,
    )

    payload = _geminigen_account_payload(account)

    assert payload["image_gen_quota"] == {"tier": "free", "remaining": 7, "used": None, "max": None}
    assert payload["grok_image_quota"] == {"tier": "free", "remaining": 3, "used": None, "max": None}


class _AdminSessionCursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class _FullAdminSessionConnection:
    def __init__(self, row=None):
        self.row = row

    async def execute(self, query, _params=None):
        if query.lstrip().upper().startswith("SELECT"):
            return _AdminSessionCursor(self.row)
        return _AdminSessionCursor(None)

    async def commit(self):
        raise sqlite3.OperationalError("database or disk is full")


def _database_with_connection(connection):
    db = Database(":memory:")

    @asynccontextmanager
    async def connect(*, write=False):
        yield connection

    db._connect = connect
    return db


class _PragmaConnection:
    def __init__(self, wal_error=None):
        self.wal_error = wal_error
        self.statements = []

    async def execute(self, query, _params=None):
        self.statements.append(query)
        if query == "PRAGMA journal_mode = WAL" and self.wal_error is not None:
            raise self.wal_error
        return _AdminSessionCursor(None)


def test_sqlite_storage_full_classifier_uses_code_and_message_fallback():
    coded = sqlite3.OperationalError("write failed")
    coded.sqlite_errorcode = sqlite3.SQLITE_FULL

    assert is_sqlite_storage_full_error(coded) is True
    assert is_sqlite_storage_full_error(
        sqlite3.OperationalError("database or disk is full")
    ) is True
    assert is_sqlite_storage_full_error(sqlite3.OperationalError("database is locked")) is False
    assert is_sqlite_storage_full_error(RuntimeError("disk is full")) is False


def test_sqlite_startup_recoverable_classifier_includes_io_errors():
    coded = sqlite3.OperationalError("write failed")
    coded.sqlite_errorcode = sqlite3.SQLITE_IOERR

    assert is_sqlite_recoverable_storage_error(coded) is True
    assert is_sqlite_recoverable_storage_error(
        sqlite3.OperationalError("disk I/O error")
    ) is True
    assert is_sqlite_recoverable_storage_error(
        sqlite3.OperationalError("database or disk is full")
    ) is True
    assert is_sqlite_recoverable_storage_error(
        sqlite3.OperationalError("database is locked")
    ) is False
    assert is_sqlite_recoverable_storage_error(RuntimeError("disk I/O error")) is False


def test_database_write_pragmas_fallback_to_delete_when_wal_io_fails():
    conn = _PragmaConnection(sqlite3.OperationalError("disk I/O error"))
    db = Database(":memory:")

    asyncio.run(db._configure_write_pragmas(conn))

    assert conn.statements == [
        "PRAGMA journal_mode = WAL",
        "PRAGMA journal_mode = DELETE",
        "PRAGMA synchronous = FULL",
    ]


def test_database_write_pragmas_preserve_non_storage_sqlite_errors():
    error = sqlite3.OperationalError("database is locked")
    conn = _PragmaConnection(error)
    db = Database(":memory:")

    try:
        asyncio.run(db._configure_write_pragmas(conn))
    except sqlite3.OperationalError as exc:
        assert exc is error
    else:
        raise AssertionError("Expected non-storage SQLite error to be re-raised")


def test_valid_admin_session_survives_storage_full_activity_touch():
    expires_at = int(time.time()) + 3600
    db = _database_with_connection(_FullAdminSessionConnection((expires_at,)))

    assert asyncio.run(db.is_admin_session_valid("admin-session")) is True


def test_expired_admin_session_stays_rejected_when_storage_is_full():
    expires_at = int(time.time()) - 1
    db = _database_with_connection(_FullAdminSessionConnection((expires_at,)))

    assert asyncio.run(db.is_admin_session_valid("expired-session")) is False


def test_new_admin_session_storage_failure_maps_to_507_without_returning():
    db = _database_with_connection(_FullAdminSessionConnection())

    try:
        asyncio.run(db.insert_admin_session("new-session", int(time.time()) + 3600))
    except sqlite3.OperationalError as exc:
        response = asyncio.run(sqlite_operational_error_handler(None, exc))
    else:
        raise AssertionError("Expected session persistence to fail")

    payload = json.loads(response.body)
    assert response.status_code == 507
    assert payload["code"] == "storage_full"
    assert "clear cached media" in payload["detail"]


def test_non_storage_sqlite_error_is_not_converted_to_507():
    error = sqlite3.OperationalError("database is locked")

    try:
        asyncio.run(sqlite_operational_error_handler(None, error))
    except sqlite3.OperationalError as exc:
        assert exc is error
    else:
        raise AssertionError("Expected non-storage SQLite error to be re-raised")


def test_runtime_sqlite_io_error_is_sanitized_as_503():
    error = sqlite3.OperationalError("disk I/O error")
    response = asyncio.run(sqlite_operational_error_handler(None, error))
    assert response.status_code == 503
    assert b"storage_unavailable" in response.body


def test_extract_artifact_urls_falls_back_to_preview_without_download_url():
    payload = {
        "generated_image": {
            "image_url": "https://cdn.example/preview.jpg",
        },
    }

    assert GeminiGenService.extract_artifact_urls(payload, "image") == [
        "https://cdn.example/preview.jpg"
    ]


def test_veo_frame_images_are_real_multipart_files():
    service = object.__new__(GeminiGenService)
    image = b"\x89PNG\r\n\x1a\ncontent"

    form = service._build_form(
        public_model_id="geminigen-veo-3.1-fast-i2v-frame-landscape-720p-4s",
        prompt="A storefront at sunrise",
        images=[image],
        options={
            "endpoint_type": "veo-video",
            "options": {
                "model": "veo-3-fast",
                "reference_mode": "frame",
                "duration": "4",
                "resolution": "720p",
                "aspect_ratio": "16:9",
            },
        },
        extra_options={},
        account=GeminiGenAccount(id=1, name="test", bearer_token="token"),
    )

    assert form["mode_image"] == "frame"
    assert "ref_images" not in form
    assert form["_file_parts"] == [
        {
            "name": "ref_images",
            "data": image,
            "filename": "ref_image_1.png",
            "content_type": "image/png",
        }
    ]


def test_veo_ingredient_prompt_gets_reference_tags():
    service = object.__new__(GeminiGenService)
    form = service._build_form(
        public_model_id="geminigen-veo-3.1-fast-i2v-ingredient-landscape-720p-4s",
        prompt="Place the subject outside the store",
        images=[b"\xff\xd8\xffcontent"],
        options={
            "endpoint_type": "veo-video",
            "options": {"model": "veo-3-fast", "reference_mode": "ingredient"},
        },
        extra_options={},
        account=GeminiGenAccount(id=1, name="test", bearer_token="token"),
    )

    assert form["mode_image"] == "ingredient"
    assert form["prompt"].startswith("@image1 ")
    assert form["_file_parts"][0]["content_type"] == "image/jpeg"


def test_concurrent_token_refresh_is_serialized_per_account():
    def token(exp: int) -> str:
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        return f"header.{payload}.signature"

    stale = GeminiGenAccount(
        id=1,
        name="test",
        bearer_token=token(int(time.time()) - 60),
        refresh_token="refresh",
    )

    class FakeDatabase:
        account = stale

        async def get_geminigen_account(self, account_id):
            return self.account

    service = object.__new__(GeminiGenService)
    service.db = FakeDatabase()
    service._token_refresh_locks = {}
    refresh_calls = 0

    async def refresh_once(account, base_url):
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0.01)
        service.db.account = account.model_copy(
            update={"bearer_token": token(int(time.time()) + 3600), "refresh_token": "rotated"}
        )
        return service.db.account

    service._refresh_account_token_unlocked = refresh_once

    async def run():
        return await asyncio.gather(
            service._refresh_account_token(stale, "https://api.geminigen.ai"),
            service._refresh_account_token(stale, "https://api.geminigen.ai"),
        )

    refreshed = asyncio.run(run())

    assert refresh_calls == 1
    assert all(account.refresh_token == "rotated" for account in refreshed)


def test_geminigen_capacity_error_is_retryable_and_sanitized():
    body = json.dumps(
        {
            "detail": {
                "error_code": GEMINIGEN_CAPACITY_ERROR_CODE,
                "error_message": "You have reached the maximum number of 5 concurrent image generations allowed by your plan.",
            }
        }
    )

    error = GeminiGenService._extract_upstream_error(400, body)

    assert error.retryable_capacity is True
    assert error.error_code == GEMINIGEN_CAPACITY_ERROR_CODE
    assert "capacity is full" in str(error)
    assert "/api/generate_image" not in str(error)


def test_geminigen_message_only_capacity_error_is_retryable():
    body = json.dumps(
        {
            "detail": {
                "error_message": "You have reached the maximum number of 5 concurrent image generations allowed by your plan. Please wait for one to finish before starting another.",
            }
        }
    )

    error = GeminiGenService._extract_upstream_error(400, body)

    assert error.retryable_capacity is True
    assert error.error_code is None
    assert "capacity is full" in str(error)


def test_geminigen_non_capacity_400_is_not_retryable():
    body = json.dumps({"detail": {"error_code": "BAD_PROMPT", "error_message": "Prompt is invalid"}})

    error = GeminiGenService._extract_upstream_error(400, body)

    assert error.retryable_capacity is False
    assert error.error_code == "BAD_PROMPT"
    assert "Prompt is invalid" in str(error)


def test_geminigen_daily_limit_code_is_classified_and_sanitized():
    body = json.dumps(
        {
            "detail": {
                "error_code": GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                "error_message": "Daily limit reached (main_limit: 200/200). Please try again tomorrow.",
            }
        }
    )

    error = GeminiGenService._extract_upstream_error(422, body)

    assert error.daily_limit_exceeded is True
    assert error.retryable_capacity is False
    assert error.error_code == GEMINIGEN_DAILY_LIMIT_ERROR_CODE
    assert "resets at 00:00 UTC" in str(error)
    assert "200/200" not in str(error)


def test_geminigen_message_only_daily_limit_422_is_classified():
    body = json.dumps(
        {
            "detail": {
                "error_message": "Daily limit reached. The daily limit resets at 00:00 UTC.",
            }
        }
    )

    error = GeminiGenService._extract_upstream_error(422, body)

    assert error.daily_limit_exceeded is True
    assert error.error_code is None


def test_geminigen_unrelated_422_is_not_daily_limited():
    body = json.dumps({"detail": {"error_code": "INVALID_INPUT", "error_message": "Invalid aspect ratio"}})

    error = GeminiGenService._extract_upstream_error(422, body)

    assert error.daily_limit_exceeded is False
    assert error.retryable_capacity is False


def test_geminigen_global_image_concurrency_does_not_reduce_account_pool_capacity():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            await db.update_geminigen_config(enabled=True, global_image_concurrency=5, global_video_concurrency=5)
            for index in range(3):
                await db.create_geminigen_account(
                    label=f"account-{index}",
                    raw_cookie="",
                    bearer_token=f"token-{index}",
                    image_concurrency=5,
                    video_concurrency=5,
                )

            acquired = [await db.acquire_geminigen_account("image") for _ in range(15)]
            blocked = await db.acquire_geminigen_account("image")
            await db.release_geminigen_account(acquired[0].id, "image")
            acquired_after_release = await db.acquire_geminigen_account("image")
            accounts = await db.list_geminigen_accounts()

            return acquired, blocked, acquired_after_release, accounts
        finally:
            os.unlink(tmp.name)

    acquired, blocked, acquired_after_release, accounts = asyncio.run(run())

    assert all(account is not None for account in acquired)
    assert blocked is None
    assert acquired_after_release is not None
    assert sorted(account.image_in_flight for account in accounts) == [5, 5, 5]


def test_request_logs_include_and_search_geminigen_job_id():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            log_id = await db.add_request_log(
                RequestLog(
                    operation="geminigen_image",
                    request_body="{}",
                    response_body=json.dumps({"status": "queued"}),
                    status_code=102,
                    duration=0.1,
                    status_text="geminigen_queued",
                    progress=0,
                )
            )
            await db.create_geminigen_task(
                GeminiGenTask(
                    job_id="geminigen-search-me",
                    request_log_id=log_id,
                    public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
                    kind="image",
                    endpoint_type="imagen",
                    status="queued",
                )
            )
            await db.add_request_log(
                RequestLog(
                    operation="geminigen_image",
                    request_body="{}",
                    response_body=json.dumps({"status": "queued"}),
                    status_code=102,
                    duration=0.1,
                    status_text="geminigen_queued",
                    progress=0,
                )
            )

            total = await db.count_request_logs(search="search-me")
            logs = await db.get_logs(search="search-me")
            detail = await db.get_log_detail(log_id)
            return total, logs, detail
        finally:
            os.unlink(tmp.name)

    total, logs, detail = asyncio.run(run())

    assert total == 1
    assert len(logs) == 1
    assert logs[0]["job_id"] == "geminigen-search-me"
    assert detail["job_id"] == "geminigen-search-me"


def test_request_log_job_id_extractor_reads_nested_payloads():
    from sub2gen.api.admin import _extract_log_job_id

    assert _extract_log_job_id(json.dumps({"content": {"job_id": "job-native-123"}})) == "job-native-123"
    assert _extract_log_job_id({"response": {"task_id": "task-456"}}) == "task-456"


def test_request_log_retention_deletes_only_old_request_logs():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            old_created = (now_utc - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
            recent_created = (now_utc - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
            async with db._connect(write=True) as conn:
                await conn.execute(
                    """
                    INSERT INTO request_logs (operation, status_code, duration, created_at, updated_at)
                    VALUES ('old', 200, 0.1, ?, ?)
                    """,
                    (old_created, old_created),
                )
                await conn.execute(
                    """
                    INSERT INTO request_logs (operation, status_code, duration, created_at, updated_at)
                    VALUES ('recent', 200, 0.1, ?, ?)
                    """,
                    (recent_created, recent_created),
                )
                await conn.execute(
                    """
                    INSERT INTO geminigen_tasks (job_id, public_model_id, kind, status, completed_at, created_at)
                    VALUES ('old-task', 'geminigen-test', 'image', 'completed', ?, ?)
                    """,
                    (old_created, old_created),
                )
                await conn.commit()

            deleted = await db.delete_request_logs_older_than(days=3)
            async with db._connect() as conn:
                request_count = (await (await conn.execute("SELECT COUNT(*) FROM request_logs")).fetchone())[0]
                task_count = (await (await conn.execute("SELECT COUNT(*) FROM geminigen_tasks")).fetchone())[0]
            return deleted, request_count, task_count
        finally:
            os.unlink(tmp.name)

    deleted, request_count, task_count = asyncio.run(run())

    assert deleted == 1
    assert request_count == 1
    assert task_count == 1


def test_api_key_generation_stats_aggregate_durable_generation_tables():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            today = f"{db._current_stats_date()} 12:00:00"
            async with db._connect(write=True) as conn:
                await conn.execute("INSERT INTO api_clients (id, name) VALUES (1, 'client')")
                await conn.execute(
                    """
                    INSERT INTO api_keys (id, client_id, label, key_prefix, key_hash)
                    VALUES (7, 1, 'key', 'fk_test', 'hash')
                    """
                )
                await conn.execute(
                    "INSERT INTO tokens (id, st, email) VALUES (1, 'st', 'user@example.com')"
                )
                await conn.executemany(
                    """
                    INSERT INTO tasks (task_id, token_id, api_key_id, model, prompt, status, completed_at)
                    VALUES (?, 1, 7, ?, 'prompt', ?, ?)
                    """,
                    [
                        ("native-image", "native-image-model", "completed", today),
                        ("native-video", "native-video-model", "completed", today),
                        ("native-error", "native-image-model", "failed", today),
                    ],
                )
                await conn.executemany(
                    """
                    INSERT INTO geminigen_tasks (job_id, api_key_id, public_model_id, kind, endpoint_type, status, completed_at)
                    VALUES (?, 7, ?, ?, 'imagen', ?, ?)
                    """,
                    [
                        ("geminigen-image", "geminigen-image-model", "image", "completed", today),
                        ("geminigen-error", "geminigen-video-model", "video", "failed", today),
                    ],
                )
                await conn.executemany(
                    """
                    INSERT INTO runway_models (public_model_id, display_name, kind, task_type)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        ("runway-video-model", "Runway Video", "video", "video_task"),
                        ("runway-error-model", "Runway Image", "image", "image_task"),
                    ],
                )
                await conn.executemany(
                    """
                    INSERT INTO runway_tasks (job_id, api_key_id, public_model_id, prompt, status, completed_at)
                    VALUES (?, 7, ?, 'prompt', ?, ?)
                    """,
                    [
                        ("runway-video", "runway-video-model", "completed", today),
                        ("runway-error", "runway-error-model", "failed", today),
                    ],
                )
                await conn.commit()

            return await db.get_api_key_generation_stats(
                7,
                native_image_models=["native-image-model"],
                native_video_models=["native-video-model"],
            )
        finally:
            os.unlink(tmp.name)

    stats = asyncio.run(run())

    assert stats == {
        "total_images": 2,
        "total_videos": 2,
        "total_errors": 3,
        "today_images": 2,
        "today_videos": 2,
        "today_errors": 3,
    }


def test_geminigen_video_disabled_filters_catalog_and_rejects_video_model():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            await db.update_geminigen_config(enabled=True, video_enabled=False)
            await db.create_geminigen_account(label="account", raw_cookie="", bearer_token="token")
            service = SimpleNamespace(db=db)

            catalog = await routes._get_geminigen_openai_model_catalog(service)
            video_id = next(item["id"] for item in GEMINIGEN_MODEL_MANIFEST if item["kind"] == "video")
            error_status = None
            try:
                await routes._require_geminigen_model_enabled(video_id, service)
            except HTTPException as exc:
                error_status = exc.status_code
            return catalog, error_status
        finally:
            os.unlink(tmp.name)

    catalog, error_status = asyncio.run(run())

    assert catalog
    assert all(model["id"] in GEMINIGEN_MODEL_BY_ID for model in catalog)
    assert all(GEMINIGEN_MODEL_BY_ID[model["id"]]["kind"] != "video" for model in catalog)
    assert error_status == 404


class FakeGeminiGenDatabase:
    def __init__(self, *, timeout_image_sec=3.0):
        self.account = GeminiGenAccount(id=1, label="primary", bearer_token="token", image_concurrency=1, image_in_flight=0)
        self.task = GeminiGenTask(
            job_id="geminigen-test",
            request_log_id=10,
            public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
            kind="image",
            endpoint_type="imagen",
            prompt="A calm lake",
            status="queued",
            progress=0,
        )
        self.config = SimpleNamespace(
            enabled=True,
            base_url="https://api.geminigen.ai",
            timeout_image_sec=timeout_image_sec,
            timeout_video_sec=3.0,
            cache_outputs=True,
        )
        self.releases = 0
        self.acquire_exclusions = []
        self.task_updates = []
        self.account_updates = []

    async def get_geminigen_task(self, job_id):
        return self.task

    async def get_geminigen_config(self):
        return self.config

    async def get_geminigen_account(self, account_id):
        return self.account

    async def acquire_geminigen_account(self, kind, excluded_account_ids=None):
        self.acquire_exclusions.append(list(excluded_account_ids or []))
        if self.account.id in (excluded_account_ids or []):
            return None
        self.account.image_in_flight += 1
        return self.account

    async def release_geminigen_account(self, account_id, kind):
        self.releases += 1
        self.account.image_in_flight = max(0, self.account.image_in_flight - 1)

    async def update_geminigen_task(self, job_id, **kwargs):
        self.task_updates.append(kwargs)
        self.task = self.task.model_copy(update=kwargs)

    async def update_geminigen_account(self, account_id, **kwargs):
        self.account_updates.append(kwargs)


def build_capacity_test_service(db):
    service = object.__new__(GeminiGenService)
    service.db = db
    service.file_cache = None
    service.proxy_manager = None
    service._capacity_cooldowns = {}
    service._capacity_cooldown_attempts = {}
    service._token_refresh_locks = {}
    service._finalize_locks = {}
    service._guard_skew_ms = 0
    service._guard_skew_synced_at = 0.0
    service._guard_skew_lock = asyncio.Lock()

    async def update_request_log(*args, **kwargs):
        return None

    service._update_request_log = update_request_log
    return service


def test_geminigen_capacity_releases_slot_and_retries_until_success():
    db = FakeGeminiGenDatabase()
    service = build_capacity_test_service(db)
    post_calls = 0

    def no_cooldown(account_id, kind):
        service._capacity_cooldowns[(account_id, kind)] = time.monotonic() - 1
        return 0.0

    async def no_sleep(*args, **kwargs):
        return None

    async def post_generation(**kwargs):
        nonlocal post_calls
        post_calls += 1
        if post_calls == 1:
            raise GeminiGenService._extract_upstream_error(
                400,
                json.dumps(
                    {
                        "detail": {
                            "error_message": "You have reached the maximum number of 5 concurrent image generations allowed by your plan.",
                        }
                    }
                ),
            )
        return {"uuid": "upstream-uuid"}

    service._set_capacity_cooldown = no_cooldown
    service._sleep_for_capacity_or_slot = no_sleep
    service._post_generation = post_generation

    result = asyncio.run(
        service._start_queued_task(
            "geminigen-test",
            images=[],
            options={},
            request_log_id=10,
            started_at=time.perf_counter(),
        )
    )

    assert result.upstream_uuid == "upstream-uuid"
    assert post_calls == 2
    assert db.releases == 1
    assert any(update.get("status") == "queued" for update in db.task_updates)
    assert db.account.image_in_flight == 1


def test_geminigen_capacity_timeout_uses_sanitized_error():
    db = FakeGeminiGenDatabase(timeout_image_sec=0.01)
    service = build_capacity_test_service(db)

    def long_cooldown(account_id, kind):
        service._capacity_cooldowns[(account_id, kind)] = time.monotonic() + 30
        return 30.0

    async def no_sleep(*args, **kwargs):
        return None

    async def post_generation(**kwargs):
        raise GeminiGenUpstreamError(
            status_code=400,
            message=f"GeminiGen upstream capacity is full ({GEMINIGEN_CAPACITY_ERROR_CODE})",
            error_code=GEMINIGEN_CAPACITY_ERROR_CODE,
            retryable_capacity=True,
        )

    service._set_capacity_cooldown = long_cooldown
    service._sleep_for_capacity_or_slot = no_sleep
    service._post_generation = post_generation

    try:
        asyncio.run(
            service._start_queued_task(
                "geminigen-test",
                images=[],
                options={},
                request_log_id=10,
                started_at=time.perf_counter(),
            )
        )
    except RuntimeError as exc:
        assert str(exc) == "GeminiGen capacity is still full; generation did not start before timeout"
    else:
        raise AssertionError("Expected capacity timeout")

    assert db.releases == 1
    assert db.task.error_message == "GeminiGen capacity is still full; generation did not start before timeout"
    assert "POST /api/generate_image" not in db.task.error_message
    assert any(1 in excluded for excluded in db.acquire_exclusions)


def test_geminigen_daily_limit_releases_account_and_reroutes_same_task():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            first_id = await db.create_geminigen_account(
                label="first",
                raw_cookie="",
                bearer_token="first-token",
                image_concurrency=1,
            )
            second_id = await db.create_geminigen_account(
                label="second",
                raw_cookie="",
                bearer_token="second-token",
                image_concurrency=1,
            )
            fresh_profile = datetime.now(timezone.utc).replace(tzinfo=None)
            await db.update_geminigen_account(first_id, profile_synced_at=fresh_profile)
            await db.update_geminigen_account(second_id, profile_synced_at=fresh_profile)
            await db.create_geminigen_task(
                GeminiGenTask(
                    job_id="geminigen-daily-reroute",
                    public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
                    kind="image",
                    endpoint_type="imagen",
                    prompt="A calm lake",
                )
            )
            service = GeminiGenService(db, None)
            attempted_accounts = []

            async def post_generation(**kwargs):
                account = kwargs["account"]
                attempted_accounts.append(account.id)
                if account.id == first_id:
                    raise GeminiGenService._extract_upstream_error(
                        422,
                        json.dumps(
                            {
                                "detail": {
                                    "error_code": GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                                    "error_message": "Daily limit reached. The daily limit resets at 00:00 UTC.",
                                }
                            }
                        ),
                    )
                return {"uuid": "rerouted-upstream-uuid"}

            service._post_generation = post_generation
            result = await service._start_queued_task(
                "geminigen-daily-reroute",
                images=[],
                options={},
            )
            first = await db.get_geminigen_account(first_id)
            second = await db.get_geminigen_account(second_id)
            return result, first, second, attempted_accounts
        finally:
            os.unlink(tmp.name)

    result, first, second, attempted_accounts = asyncio.run(run())

    assert attempted_accounts == [first.id, second.id]
    assert first.image_gen_daily_limit_reset_at is not None
    assert first.grok_image_daily_limit_reset_at is None
    assert first.image_in_flight == 0
    assert first.last_status == "daily_limited_image_gen"
    assert first.last_error == GEMINIGEN_DAILY_LIMIT_ERROR_CODE
    assert second.image_in_flight == 1
    assert result.account_id == second.id
    assert result.upstream_uuid == "rerouted-upstream-uuid"


def test_geminigen_grok_daily_limit_reroutes_without_blocking_imagen():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            first_id = await db.create_geminigen_account(
                label="first",
                raw_cookie="",
                bearer_token="first-token",
                image_concurrency=1,
            )
            second_id = await db.create_geminigen_account(
                label="second",
                raw_cookie="",
                bearer_token="second-token",
                image_concurrency=1,
            )
            fresh_profile = datetime.now(timezone.utc).replace(tzinfo=None)
            await db.update_geminigen_account(first_id, profile_synced_at=fresh_profile)
            await db.update_geminigen_account(second_id, profile_synced_at=fresh_profile)
            await db.create_geminigen_task(
                GeminiGenTask(
                    job_id="geminigen-grok-daily-reroute",
                    public_model_id="geminigen-grok-image-landscape-speed",
                    kind="image",
                    endpoint_type="grok-image",
                    prompt="A calm lake",
                )
            )
            service = GeminiGenService(db, None)

            async def post_generation(**kwargs):
                if kwargs["account"].id == first_id:
                    raise GeminiGenService._extract_upstream_error(
                        422,
                        json.dumps(
                            {
                                "detail": {
                                    "error_code": GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                                    "error_message": "Daily limit reached.",
                                }
                            }
                        ),
                    )
                return {"uuid": "grok-rerouted-uuid"}

            service._post_generation = post_generation
            result = await service._start_queued_task(
                "geminigen-grok-daily-reroute",
                images=[],
                options={},
            )
            first = await db.get_geminigen_account(first_id)
            imagen_available = await db.acquire_geminigen_account("image", endpoint_type="imagen")
            return result, first, imagen_available
        finally:
            os.unlink(tmp.name)

    result, first, imagen_available = asyncio.run(run())

    assert result.upstream_uuid == "grok-rerouted-uuid"
    assert first.grok_image_daily_limit_reset_at is not None
    assert first.image_gen_daily_limit_reset_at is None
    assert imagen_available is not None


def test_geminigen_all_daily_limited_accounts_fail_fast_with_retry_time():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            reset_at = GeminiGenService._next_utc_midnight()
            for index in range(2):
                account_id = await db.create_geminigen_account(
                    label=f"limited-{index}",
                    raw_cookie="",
                    bearer_token=f"token-{index}",
                )
                await db.update_geminigen_account(
                    account_id,
                    image_gen_daily_limit_reset_at=reset_at,
                    last_status="daily_limited_image_gen",
                    last_error=GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                )
            await db.create_geminigen_task(
                GeminiGenTask(
                    job_id="geminigen-all-daily-limited",
                    public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
                    kind="image",
                    endpoint_type="imagen",
                )
            )
            service = GeminiGenService(db, None)
            started = time.monotonic()
            result = await service._start_queued_task(
                "geminigen-all-daily-limited",
                images=[],
                options={},
            )
            return result, service.task_to_openai_payload(result), time.monotonic() - started
        finally:
            os.unlink(tmp.name)

    result, payload, elapsed = asyncio.run(run())

    assert elapsed < 1
    assert result.status == "failed"
    assert result.error_code == GEMINIGEN_DAILY_LIMIT_ERROR_CODE
    assert result.retry_at is not None
    assert payload["error"]["type"] == "rate_limit_error"
    assert payload["error"]["code"] == GEMINIGEN_DAILY_LIMIT_ERROR_CODE
    assert payload["error"]["status_code"] == 429
    assert payload["error"]["retry_at"].endswith("Z")


def test_geminigen_concurrent_daily_limit_failures_do_not_leak_slots():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            account_id = await db.create_geminigen_account(
                label="shared",
                raw_cookie="",
                bearer_token="token",
                image_concurrency=5,
            )
            await db.update_geminigen_account(
                account_id,
                profile_synced_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            for suffix in ("one", "two"):
                await db.create_geminigen_task(
                    GeminiGenTask(
                        job_id=f"geminigen-concurrent-daily-{suffix}",
                        public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
                        kind="image",
                        endpoint_type="imagen",
                    )
                )
            service = GeminiGenService(db, None)

            async def post_generation(**kwargs):
                await asyncio.sleep(0.01)
                raise GeminiGenService._extract_upstream_error(
                    422,
                    json.dumps(
                        {
                            "detail": {
                                "error_code": GEMINIGEN_DAILY_LIMIT_ERROR_CODE,
                                "error_message": "Daily limit reached.",
                            }
                        }
                    ),
                )

            service._post_generation = post_generation
            results = await asyncio.gather(
                service._start_queued_task("geminigen-concurrent-daily-one", images=[], options={}),
                service._start_queued_task("geminigen-concurrent-daily-two", images=[], options={}),
            )
            account = await db.get_geminigen_account(account_id)
            return results, account
        finally:
            os.unlink(tmp.name)

    results, account = asyncio.run(run())

    assert all(result.error_code == GEMINIGEN_DAILY_LIMIT_ERROR_CODE for result in results)
    assert account.image_in_flight == 0
    assert account.image_gen_daily_limit_reset_at is not None


def test_geminigen_public_status_dict_exposes_phase_and_terminal_urls():
    queued = GeminiGenTask(
        job_id="geminigen-queued",
        public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
        kind="image",
        endpoint_type="imagen",
        prompt="waiting",
        status="queued",
        progress=0,
    )
    completed = GeminiGenTask(
        job_id="geminigen-complete",
        public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
        kind="image",
        endpoint_type="imagen",
        prompt="done",
        status="completed",
        progress=100,
        raw_artifact_urls=["https://cdn.example/raw.png"],
        cached_artifact_urls=["https://flow.example/api/cache/blob/raw.png"],
        response_payload=json.dumps({"status": "SUCCESSFUL"}),
    )
    cancelled = completed.model_copy(
        update={
            "job_id": "geminigen-cancelled",
            "status": "cancelled",
            "progress": 5,
            "raw_artifact_urls": [],
            "cached_artifact_urls": [],
            "error_message": "User cancelled",
            "response_payload": json.dumps({"status": "cancelled"}),
        }
    )

    queued_public = GeminiGenService.task_to_public_dict(queued)
    completed_public = GeminiGenService.task_to_public_dict(completed)
    cancelled_public = GeminiGenService.task_to_public_dict(cancelled)

    assert queued_public["status"] == "queued"
    assert queued_public["job_phase"] == "queued"
    assert queued_public["upstream_status"] is None
    assert completed_public["status"] == "completed"
    assert completed_public["job_phase"] == "completed"
    assert completed_public["upstream_status"] == "SUCCESSFUL"
    assert completed_public["result_urls"] == ["https://flow.example/api/cache/blob/raw.png"]
    assert cancelled_public["status"] == "cancelled"
    assert cancelled_public["job_phase"] == "cancelled"
    assert cancelled_public["error_message"] == "User cancelled"


def test_geminigen_upstream_cancel_status_is_not_classified_as_failed():
    payload = {"status": "cancelled", "message": "Cancelled by user"}

    assert GeminiGenService._history_cancelled(payload, "cancelled") is True
    assert GeminiGenService._history_failed(payload, "cancelled") is False


def test_geminigen_poll_maps_upstream_cancel_to_cancelled_and_releases_account():
    db = FakeGeminiGenDatabase()
    db.task = db.task.model_copy(update={"status": "processing", "account_id": 1, "upstream_uuid": "upstream-uuid", "progress": 12})
    service = build_capacity_test_service(db)

    async def get_history(**kwargs):
        return {"status": "stopped", "message": "Stopped by upstream"}

    service._get_history = get_history

    result = asyncio.run(service.poll_task("geminigen-test", api_key_id=None, base_url="https://flow.example"))

    assert result.status == "cancelled"
    assert result.error_message == "Stopped by upstream"
    assert db.releases == 1
    assert any(update.get("status") == "cancelled" for update in db.task_updates)


def test_geminigen_poll_does_not_query_history_while_submission_is_in_flight():
    db = FakeGeminiGenDatabase()
    db.task = db.task.model_copy(update={"status": "processing", "account_id": 1, "upstream_uuid": None, "progress": 1})
    service = build_capacity_test_service(db)

    async def unexpected_history_call(**kwargs):
        raise AssertionError("history must not be queried before upstream_uuid exists")

    service._get_history = unexpected_history_call

    result = asyncio.run(service.poll_task("geminigen-test", api_key_id=None, base_url="https://flow.example"))

    assert result.status == "processing"
    assert result.upstream_uuid is None


def test_geminigen_passive_status_reads_do_not_poll_or_write(monkeypatch):
    task = GeminiGenTask(
        job_id="geminigen-passive",
        api_key_id=42,
        public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
        kind="image",
        endpoint_type="imagen",
        prompt="status",
        status="processing",
        progress=5,
        upstream_uuid="upstream-uuid",
    )

    class PassiveDb:
        def __init__(self):
            self.reads = 0
            self.writes = 0

        async def get_geminigen_task(self, job_id):
            self.reads += 1
            return task

    db = PassiveDb()
    poll_task = AsyncMock(side_effect=AssertionError("status reads must not poll GeminiGen"))
    service = SimpleNamespace(
        db=db,
        poll_task=poll_task,
        task_to_public_dict=GeminiGenService.task_to_public_dict,
    )
    container = SimpleNamespace(
        geminigen_service=service,
        runway_service=SimpleNamespace(),
        generation_handler=SimpleNamespace(),
    )
    auth = AuthContext(
        key_id=42,
        key_label="test",
        is_legacy=False,
        allowed_accounts=set(),
        scopes={"generate:geminigen"},
    )

    async def read_twenty():
        return await asyncio.gather(
            *(routes.get_job_status("geminigen-passive", None, auth, container) for _ in range(20))
        )

    results = asyncio.run(read_twenty())

    assert len(results) == 20
    assert all(result["status"] == "processing" for result in results)
    assert db.reads == 20
    assert db.writes == 0
    poll_task.assert_not_awaited()


def test_geminigen_unchanged_processing_poll_skips_database_and_request_log_writes():
    db = FakeGeminiGenDatabase()
    db.task = db.task.model_copy(
        update={
            "status": "processing",
            "account_id": 1,
            "upstream_uuid": "upstream-uuid",
            "progress": 5,
            "response_payload": json.dumps({"status": "1", "status_percentage": 5}),
        }
    )
    service = build_capacity_test_service(db)
    request_log_update = AsyncMock()
    service._update_request_log = request_log_update

    async def get_history(**kwargs):
        return {"status": "1", "status_percentage": 5}

    service._get_history = get_history
    result = asyncio.run(service.poll_task("geminigen-test", api_key_id=None, base_url="https://flow.example"))

    assert result.status == "processing"
    assert result.progress == 5
    assert db.task_updates == []
    request_log_update.assert_not_awaited()


def test_geminigen_background_completion_is_claimed_once():
    service = object.__new__(GeminiGenService)
    service._background_completion_jobs = set()
    service._background_completion_lock = asyncio.Lock()
    wait_for_task = AsyncMock()

    async def wait_once(*args, **kwargs):
        await asyncio.sleep(0.01)

    wait_for_task.side_effect = wait_once
    service.wait_for_task = wait_for_task

    async def run_duplicate_workers():
        await asyncio.gather(
            service.complete_task_in_background("geminigen-test", api_key_id=1, base_url=None),
            service.complete_task_in_background("geminigen-test", api_key_id=1, base_url=None),
        )

    asyncio.run(run_duplicate_workers())

    wait_for_task.assert_awaited_once()
    assert service._background_completion_jobs == set()


def test_geminigen_poll_respects_global_cache_kill_switch():
    db = FakeGeminiGenDatabase()
    db.task = db.task.model_copy(update={"status": "processing", "account_id": 1, "upstream_uuid": "upstream-uuid"})
    service = build_capacity_test_service(db)
    service.file_cache = SimpleNamespace(download_and_cache=AsyncMock(return_value="cached.png"))
    log_updates = []

    async def get_history(**kwargs):
        return {"status": "SUCCESSFUL", "file_download_url": "https://cdn.example/result.png"}

    async def update_request_log(*args, **kwargs):
        log_updates.append(kwargs)

    service._get_history = get_history
    service._update_request_log = update_request_log
    original_cache_enabled = config.cache_enabled
    config.set_cache_enabled(False)
    try:
        result = asyncio.run(service.poll_task("geminigen-test", api_key_id=None, base_url="https://flow.example"))
    finally:
        config.set_cache_enabled(original_cache_enabled)

    assert result.status == "completed"
    assert result.raw_artifact_urls == ["https://cdn.example/result.png"]
    assert result.cached_artifact_urls == []
    assert GeminiGenService.task_to_public_dict(result)["result_urls"] == ["https://cdn.example/result.png"]
    assert GeminiGenService.task_to_openai_payload(result)["result_urls"] == ["https://cdn.example/result.png"]
    assert "https://cdn.example/result.png" in GeminiGenService.task_to_openai_payload(result)["choices"][0]["message"]["content"]
    assert log_updates[-1]["response"]["result_urls"] == ["https://cdn.example/result.png"]
    service.file_cache.download_and_cache.assert_not_awaited()


def test_geminigen_poll_returns_cached_url_when_global_and_provider_cache_enabled():
    db = FakeGeminiGenDatabase()
    db.task = db.task.model_copy(update={"status": "processing", "account_id": 1, "upstream_uuid": "upstream-uuid"})
    service = build_capacity_test_service(db)
    service.file_cache = SimpleNamespace(download_and_cache=AsyncMock(return_value="cached.png"))

    async def get_history(**kwargs):
        return {"status": "SUCCESSFUL", "file_download_url": "https://cdn.example/result.png"}

    service._get_history = get_history
    original_cache_enabled = config.cache_enabled
    config.set_cache_enabled(True)
    try:
        result = asyncio.run(service.poll_task("geminigen-test", api_key_id=None, base_url="https://flow.example"))
    finally:
        config.set_cache_enabled(original_cache_enabled)

    cached_url = "https://flow.example/api/cache/blob/cached.png"
    assert result.status == "completed"
    assert result.raw_artifact_urls == ["https://cdn.example/result.png"]
    assert result.cached_artifact_urls == [cached_url]
    assert GeminiGenService.task_to_public_dict(result)["result_urls"] == [cached_url]
    assert GeminiGenService.task_to_openai_payload(result)["result_urls"] == [cached_url]
    service.file_cache.download_and_cache.assert_awaited_once_with(
        "https://cdn.example/result.png",
        media_type="image",
        api_key_id=None,
        token_id=None,
        flow_project_id=None,
    )


def test_geminigen_poll_returns_direct_url_when_provider_cache_disabled():
    db = FakeGeminiGenDatabase()
    db.config.cache_outputs = False
    db.task = db.task.model_copy(update={"status": "processing", "account_id": 1, "upstream_uuid": "upstream-uuid"})
    service = build_capacity_test_service(db)
    service.file_cache = SimpleNamespace(download_and_cache=AsyncMock(return_value="cached.png"))

    async def get_history(**kwargs):
        return {"status": "SUCCESSFUL", "file_download_url": "https://cdn.example/result.png"}

    service._get_history = get_history
    original_cache_enabled = config.cache_enabled
    config.set_cache_enabled(True)
    try:
        result = asyncio.run(service.poll_task("geminigen-test", api_key_id=None, base_url="https://flow.example"))
    finally:
        config.set_cache_enabled(original_cache_enabled)

    assert result.status == "completed"
    assert result.raw_artifact_urls == ["https://cdn.example/result.png"]
    assert result.cached_artifact_urls == []
    assert GeminiGenService.task_to_public_dict(result)["result_urls"] == ["https://cdn.example/result.png"]
    assert GeminiGenService.task_to_openai_payload(result)["result_urls"] == ["https://cdn.example/result.png"]
    service.file_cache.download_and_cache.assert_not_awaited()


def test_geminigen_concurrent_completion_caches_once():
    db = FakeGeminiGenDatabase()
    db.task = db.task.model_copy(update={"status": "processing", "account_id": 1, "upstream_uuid": "upstream-uuid"})
    service = build_capacity_test_service(db)
    history_calls = 0

    async def download_and_cache(*args, **kwargs):
        await asyncio.sleep(0.01)
        return "cached.png"

    async def get_history(**kwargs):
        nonlocal history_calls
        history_calls += 1
        await asyncio.sleep(0)
        return {
            "status": "SUCCESSFUL",
            "file_download_url": f"https://cdn.example/result.png?Signature={history_calls}",
        }

    async def run():
        original_cache_enabled = config.cache_enabled
        config.set_cache_enabled(True)
        try:
            return await asyncio.gather(
                service.poll_task("geminigen-test", api_key_id=None, base_url="https://flow.example"),
                service.poll_task("geminigen-test", api_key_id=None, base_url="https://flow.example"),
            )
        finally:
            config.set_cache_enabled(original_cache_enabled)

    service.file_cache = SimpleNamespace(download_and_cache=AsyncMock(side_effect=download_and_cache))
    service._get_history = get_history

    results = asyncio.run(run())

    cached_url = "https://flow.example/api/cache/blob/cached.png"
    assert all(result.status == "completed" for result in results)
    assert all(result.cached_artifact_urls == [cached_url] for result in results)
    service.file_cache.download_and_cache.assert_awaited_once()
    assert db.releases == 1


def test_geminigen_completed_without_artifact_urls_fails_clearly():
    db = FakeGeminiGenDatabase()
    db.task = db.task.model_copy(update={"status": "processing", "account_id": 1, "upstream_uuid": "upstream-uuid"})
    service = build_capacity_test_service(db)
    service.file_cache = SimpleNamespace(download_and_cache=AsyncMock(return_value="cached.png"))
    log_updates = []

    async def get_history(**kwargs):
        return {"status": "SUCCESSFUL"}

    async def update_request_log(*args, **kwargs):
        log_updates.append(kwargs)

    service._get_history = get_history
    service._update_request_log = update_request_log

    result = asyncio.run(service.poll_task("geminigen-test", api_key_id=None, base_url="https://flow.example"))

    assert result.status == "failed"
    assert result.error_message == "GeminiGen completed but did not return any artifact URLs"
    assert GeminiGenService.task_to_public_dict(result)["result_urls"] == []
    assert GeminiGenService.task_to_openai_payload(result)["result_urls"] == []
    assert log_updates[-1]["status_code"] == 502
    assert log_updates[-1]["response"]["result_urls"] == []
    service.file_cache.download_and_cache.assert_not_awaited()


class FakeRunwayDatabase:
    def __init__(self):
        self.task = RunwayTask(
            job_id="runway-test",
            upstream_task_id="upstream-task",
            account_id=1,
            api_key_id=5,
            public_model_id="runway-video",
            status="processing",
        )
        self.account = RunwayAccount(id=1, label="primary", raw_credential="token")
        self.model = RunwayModel(public_model_id="runway-video", display_name="Runway Video", kind="video", task_type="video")
        self.config = SimpleNamespace(base_url="https://api.runwayml.com/v1", cache_outputs=True)
        self.releases = 0

    async def get_runway_task(self, job_id):
        return self.task

    async def get_runway_config(self):
        return self.config

    async def get_runway_account(self, account_id):
        return self.account

    async def get_runway_model(self, public_model_id):
        return self.model

    async def update_runway_task(self, job_id, **kwargs):
        self.task = self.task.model_copy(update=kwargs)

    async def release_runway_account(self, account_id):
        self.releases += 1


def test_runway_poll_respects_global_cache_kill_switch():
    db = FakeRunwayDatabase()
    service = object.__new__(RunwayService)
    service.db = db
    service.file_cache = SimpleNamespace(download_and_cache=AsyncMock(return_value="cached.mp4"))
    service.proxy_manager = None

    async def get_upstream_task(**kwargs):
        return {"task": {"status": "SUCCEEDED", "artifacts": [{"url": "https://cdn.example/result.mp4"}]}}

    async def get_upstream_task_generation(**kwargs):
        return {}

    service.get_upstream_task = get_upstream_task
    service.get_upstream_task_generation = get_upstream_task_generation

    original_cache_enabled = config.cache_enabled
    config.set_cache_enabled(False)
    try:
        result = asyncio.run(service.poll_task("runway-test", api_key_id=5, base_url="https://flow.example"))
    finally:
        config.set_cache_enabled(original_cache_enabled)

    assert result.status == "completed"
    assert result.raw_artifact_urls == ["https://cdn.example/result.mp4"]
    assert result.cached_artifact_urls is None
    service.file_cache.download_and_cache.assert_not_awaited()


def test_geminigen_wait_for_task_exits_on_cancelled():
    service = object.__new__(GeminiGenService)
    cancelled = GeminiGenTask(
        job_id="geminigen-cancelled",
        public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
        kind="image",
        endpoint_type="imagen",
        prompt="cancel",
        status="cancelled",
        progress=1,
        error_message="Cancelled",
    )

    async def get_task(job_id):
        return cancelled

    async def get_config():
        return SimpleNamespace(timeout_image_sec=5, timeout_video_sec=5, poll_interval_image_sec=0.1, poll_interval_video_sec=0.1)

    async def poll_task(job_id, api_key_id=None, base_url=None):
        return cancelled

    service.db = SimpleNamespace(get_geminigen_task=get_task, get_geminigen_config=get_config)
    service.poll_task = poll_task

    result = asyncio.run(service.wait_for_task("geminigen-cancelled", api_key_id=None, base_url=None))

    assert result.status == "cancelled"


def test_geminigen_stream_exits_on_cancelled(monkeypatch):
    cancelled = GeminiGenTask(
        job_id="geminigen-cancelled",
        public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
        kind="image",
        endpoint_type="imagen",
        prompt="cancel",
        status="cancelled",
        progress=1,
        error_message="Cancelled by user",
    )

    class FakeService:
        is_geminigen_terminal_status = staticmethod(GeminiGenService.is_geminigen_terminal_status)
        task_to_openai_payload = staticmethod(GeminiGenService.task_to_openai_payload)

        async def poll_task(self, job_id, api_key_id=None, base_url=None):
            return cancelled

    async def start_task(*args, **kwargs):
        return cancelled

    monkeypatch.setattr(routes, "_start_geminigen_from_request", start_task)
    service = FakeService()

    async def collect():
        chunks = []
        async for chunk in routes._iterate_geminigen_openai_stream(
            None, None, api_key_id=None, base_url=None, service=service
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())

    assert any("geminigen_generation_cancelled" in chunk for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"


def test_veo_ingredient_manifest_only_exposes_supported_eight_second_duration():
    assert "geminigen-veo-3.1-fast-i2v-ingredient-landscape-720p-8s" in GEMINIGEN_MODEL_BY_ID
    assert "geminigen-veo-3.1-fast-i2v-ingredient-landscape-720p-4s" not in GEMINIGEN_MODEL_BY_ID
    assert "geminigen-veo-3.1-fast-i2v-ingredient-landscape-720p-6s" not in GEMINIGEN_MODEL_BY_ID


def test_studio_metadata_exposes_native_variant_and_geminigen_reference_mode():
    native = native_studio_metadata(
        "gemini-3.1-flash-image-square-4k",
        {
            "type": "image",
            "model_name": "NARWHAL",
            "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
            "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K",
        },
    )
    assert native["family_id"] == "native:gemini-3.1-flash-image"
    assert native["variant"] == {"resolution": "4K", "aspect_ratio": "1:1"}

    geminigen = next(item for item in GEMINIGEN_MODEL_MANIFEST if "i2v-frame" in item["id"])
    assert geminigen_studio_metadata(geminigen)["modes"] == ["image_to_video"]
