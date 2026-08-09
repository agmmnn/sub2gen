import asyncio
import base64
import json
import os
import uuid
from pathlib import Path

import pytest

from sub2gen.core.database_runtime import DatabaseSettings
from sub2gen.core.models import GeminiGenTask, Project, RequestLog, RunwayTask, Task, Token
from sub2gen.core.postgres_database import PostgresDatabase
from sub2gen.persistence.repositories import Repositories
from sub2gen.services.postgres_backup import (
    create_postgres_archive,
    database_row_counts,
    decrypt_and_extract_postgres_archive,
    restore_postgres_dump,
    verify_restored_row_counts,
)


POSTGRES_URL = os.environ.get("SUB2GEN_TEST_POSTGRES_URL") or os.environ.get("DATABASE_PUBLIC_URL")
REDIS_URL = os.environ.get("SUB2GEN_TEST_REDIS_URL") or os.environ.get("SUB2GEN_REDIS_URL")
STORAGE_CONTRACT = json.loads((Path(__file__).parent / "contracts" / "storage-state.json").read_text(encoding="utf-8"))


pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="SUB2GEN_TEST_POSTGRES_URL is not configured",
)


@pytest.mark.asyncio
async def test_postgres_storage_contract_roundtrip(monkeypatch):
    schema = f"sub2gen_test_{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv("SUB2GEN_DB_SCHEMA", schema)
    monkeypatch.setenv("SUB2GEN_REQUIRE_CUTOVER_MARKER", "false")
    settings = DatabaseSettings.from_env(backend="postgres", url=POSTGRES_URL)
    database = PostgresDatabase(settings=settings)
    await database.init_db()
    await database.cache_schema_capabilities()
    repositories = Repositories.from_database(database)
    try:
        token_id = await repositories.accounts.add_token(
            Token(st=f"st-{uuid.uuid4().hex}", email=STORAGE_CONTRACT["token"]["email"])
        )
        token = await repositories.accounts.get_token(token_id)
        assert token is not None
        assert token.email == STORAGE_CONTRACT["token"]["email"]
        assert token.is_active is True

        await repositories.accounts.update_token(
            token_id,
            is_active=STORAGE_CONTRACT["token"]["is_active"],
            credits=STORAGE_CONTRACT["token"]["credits"],
        )
        updated = await repositories.accounts.get_token(token_id)
        assert updated is not None
        assert updated.is_active is STORAGE_CONTRACT["token"]["is_active"]
        assert updated.credits == STORAGE_CONTRACT["token"]["credits"]

        project_id = f"project-{uuid.uuid4().hex}"
        await repositories.projects.add_project(
            Project(
                project_id=project_id,
                token_id=token_id,
                project_name=STORAGE_CONTRACT["project"]["name"],
            )
        )
        project = await repositories.projects.get_project_by_id(project_id)
        assert project is not None
        assert project.token_id == token_id
        assert project.project_name == STORAGE_CONTRACT["project"]["name"]

        task_id = f"task-{uuid.uuid4().hex}"
        await database.create_task(
            Task(
                task_id=task_id,
                token_id=token_id,
                model="test-model",
                prompt="contract test",
                status="processing",
            )
        )
        await database.update_task(task_id, progress=50, job_phase="generation")
        task = await database.get_task(task_id)
        assert task is not None
        assert task.progress == 50

        log_id = await repositories.request_logs.add_request_log(
            RequestLog(
                token_id=token_id,
                operation="postgres-contract",
                request_body='{"secret":"redacted-by-production-manager"}',
                response_body="ok",
                status_code=200,
                duration=0.01,
            )
        )
        detail = await repositories.request_logs.get_log_detail(log_id)
        assert detail is not None
        assert detail["operation"] == "postgres-contract"

        key_id = await repositories.api_keys.create_client_api_key(
            client_name="postgres-contract-client",
            label="contract-key",
            key_prefix="f2_contract",
            key_plaintext=None,
            key_hash=uuid.uuid4().hex,
            scopes="*",
            account_ids=[token_id],
            endpoint_limits={"/v1/chat/completions": {"rpm": 10, "rph": 100, "burst": 2}},
            expires_at=None,
        )
        key_detail = await repositories.api_keys.get_api_key_detail(key_id)
        assert key_detail is not None
        assert key_detail["is_active"] is STORAGE_CONTRACT["managed_key"]["is_active"]
        assert len(key_detail["account_ids"]) == STORAGE_CONTRACT["managed_key"]["account_count"]
        assert key_detail["account_ids"] == [token_id]

        assert await database.count_request_logs(search="postgres-contract") == 1
        logs = await database.get_logs(search="postgres-contract")
        assert logs and logs[0]["id"] == log_id
        dashboard = await database.get_dashboard_stats()
        assert dashboard["total_tokens"] == 1

        await database.increment_image_count(token_id)
        await database.increment_video_count(token_id)
        await database.increment_error_count(token_id)
        await database.reset_error_count(token_id)
        stats = await database.get_token_stats(token_id)
        assert stats is not None
        assert stats.image_count == 1
        assert stats.video_count == 1
        assert stats.error_count == 1

        await database.increment_operation_stat("postgres-operation", success=True)
        await database.increment_operation_stat("postgres-operation", success=False)
        operation_stats = await database.get_operation_stats("postgres-operation")
        assert operation_stats["success_count"] == 1
        assert operation_stats["error_count"] == 1

        await database.update_proxy_config(True, "http://127.0.0.1:8080", True, "http://127.0.0.1:8081")
        proxy = await database.get_proxy_config()
        assert proxy is not None and proxy.enabled is True and proxy.media_proxy_enabled is True
        await database.update_cache_config(
            enabled=True,
            timeout=3600,
            base_url="https://cache.example.com",
            provider="local",
            delivery_mode="proxy",
        )
        assert (await database.get_cache_config()).cache_enabled is True
        await database.update_debug_config(enabled=True, log_requests=False, log_responses=False, mask_token=True)
        debug = await database.get_debug_config()
        assert debug.enabled is True and debug.log_requests is False

        session_token = uuid.uuid4().hex
        await database.insert_admin_session(session_token, 4_000_000_000)
        assert await database.is_admin_session_valid(session_token) is True
        assert await database.is_admin_session_recent(session_token, 60) is True
        await database.delete_admin_session(session_token)
        assert await database.is_admin_session_valid(session_token) is False

        await repositories.cache.upsert_cache_file(
            filename="postgres-contract.bin",
            api_key_id=key_id,
            token_id=token_id,
            media_type=STORAGE_CONTRACT["cache"]["media_type"],
            source_url="https://example.com/source",
            flow_project_id=project_id,
            size_bytes=STORAGE_CONTRACT["cache"]["size_bytes"],
        )
        cache_file = await repositories.cache.get_cache_file_for_api_key("postgres-contract.bin", key_id)
        assert cache_file is not None
        assert cache_file["media_type"] == STORAGE_CONTRACT["cache"]["media_type"]
        assert cache_file["size_bytes"] == STORAGE_CONTRACT["cache"]["size_bytes"]

        await database.insert_api_key_audit_log(
            api_key_id=key_id,
            endpoint="/contract",
            account_id=token_id,
            status_code=200,
            detail="ok",
            ip="127.0.0.1",
            user_agent="pytest",
        )
        assert await database.count_api_key_audit_logs(key_id) == 1
        assert len(await database.list_api_key_audit_logs(key_id=key_id)) == 1

        await repositories.workers.upsert_extension_worker_binding("contract-route", key_id)
        binding = await repositories.workers.get_extension_worker_binding_for_route_key("contract-route")
        assert binding is not None and binding["api_key_id"] == key_id
        await repositories.workers.delete_extension_worker_binding("contract-route")

        worker_id = await database.create_captcha_worker_key(
            key_prefix="cwk_contract",
            key_hash=uuid.uuid4().hex,
            label="contract worker",
        )
        await database.update_captcha_worker_key(worker_id, is_active=False, mark_seen=True)
        worker = await database.get_captcha_worker_key(worker_id)
        assert worker is not None and worker["is_active"] is False

        runway_account_id = await database.create_runway_account(
            label="contract runway",
            raw_credential="credential",
            concurrency_limit=1,
        )
        runway_model_id = await database.upsert_runway_model(
            public_model_id="contract-runway-model",
            display_name="Contract Runway",
            kind="image",
            task_type="contract",
        )
        assert runway_model_id > 0
        runway_job = f"runway-{uuid.uuid4().hex}"
        await database.create_runway_task(
            RunwayTask(
                job_id=runway_job,
                account_id=runway_account_id,
                api_key_id=key_id,
                public_model_id="contract-runway-model",
            )
        )
        await database.update_runway_task(runway_job, progress=75, status="processing")
        runway_task = await database.get_runway_task(runway_job)
        assert runway_task is not None and runway_task.progress == 75
        await database.update_runway_config(enabled=True, cache_outputs=False)
        runway_config = await database.get_runway_config()
        assert runway_config.enabled is True and runway_config.cache_outputs is False
        runway_acquisitions = await asyncio.gather(*(database.acquire_runway_account() for _ in range(4)))
        reserved_runway = [account for account in runway_acquisitions if account is not None]
        assert len(reserved_runway) == 1
        await database.release_runway_account(reserved_runway[0].id)

        gemini_account_id = await database.create_geminigen_account(
            label="contract gemini",
            raw_cookie="cookie",
            bearer_token="bearer",
            image_concurrency=1,
        )
        gemini_job = f"gemini-{uuid.uuid4().hex}"
        await database.create_geminigen_task(
            GeminiGenTask(
                job_id=gemini_job,
                account_id=gemini_account_id,
                api_key_id=key_id,
                request_log_id=log_id,
                public_model_id="contract-gemini-model",
            )
        )
        await database.update_geminigen_task(gemini_job, progress=25, status="processing")
        gemini_task = await database.get_geminigen_task(gemini_job)
        assert gemini_task is not None and gemini_task.progress == 25
        await database.update_geminigen_config(enabled=True, cache_outputs=False)
        gemini_config = await database.get_geminigen_config()
        assert gemini_config.enabled is True and gemini_config.cache_outputs is False
        gemini_acquisitions = await asyncio.gather(*(database.acquire_geminigen_account("image") for _ in range(4)))
        reserved_gemini = [account for account in gemini_acquisitions if account is not None]
        assert len(reserved_gemini) == 1
        await database.release_geminigen_account(reserved_gemini[0].id, "image")

        await database.update_call_logic_config("polling")
        call_logic = await database.get_call_logic_config()
        assert call_logic.polling_mode_enabled is True

        applied = await database.apply_redis_event_batch(
            [
                {
                    "cursor": "1-0",
                    "type": "usage_touch",
                    "data": {"api_key_id": key_id},
                }
            ]
        )
        assert applied == 1
        assert (
            await database.apply_redis_event_batch(
                [{"cursor": "1-0", "type": "usage_touch", "data": {"api_key_id": key_id}}]
            )
            == 0
        )

        health = await database.health_snapshot()
        assert health["database_backend"] == "postgres"
        assert health["database_ready"] is True
        assert health["database_revision"] == "0002"
        row_counts = await database_row_counts(database)
        assert row_counts["tokens"] == 1
        assert row_counts["runway_tasks"] == 1
        assert row_counts["geminigen_tasks"] == 1

        await database.close_runtime_connections()
        await database.init_db()
        assert await database.get_token(token_id) is not None
    finally:
        await database.close_runtime_connections()
        from psycopg import AsyncConnection, sql

        cleanup = await AsyncConnection.connect(POSTGRES_URL)
        try:
            await cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            await cleanup.commit()
        finally:
            await cleanup.close()


@pytest.mark.asyncio
async def test_postgres_16_encrypted_dump_restore_roundtrip(monkeypatch, tmp_path):
    schema = f"sub2gen_backup_test_{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv("SUB2GEN_DB_SCHEMA", schema)
    monkeypatch.setenv("SUB2GEN_REQUIRE_CUTOVER_MARKER", "false")
    monkeypatch.setenv("SUB2GEN_BACKUP_ACTIVE_KEY_ID", "ci-test")
    monkeypatch.setenv(
        "SUB2GEN_BACKUP_KEYS_JSON",
        json.dumps({"ci-test": base64.b64encode(os.urandom(32)).decode("ascii")}),
    )
    settings = DatabaseSettings.from_env(backend="postgres", url=POSTGRES_URL)
    database = PostgresDatabase(settings=settings)
    await database.init_db()
    try:
        token_id = await database.add_token(Token(st=f"backup-{uuid.uuid4().hex}", email="backup-contract@example.com"))
        profiles = tmp_path / "profiles"
        (profiles / "token-1" / "Default").mkdir(parents=True)
        (profiles / "token-1" / "Default" / "Cookies").write_bytes(b"profile-data")
        working = tmp_path / "create"
        encrypted = tmp_path / "backup.s2g"
        manifest = await create_postgres_archive(
            database,
            profiles,
            working,
            encrypted,
            backup_id="ci-roundtrip",
            backup_type="manual",
            app_version="test",
        )

        await database.delete_token(token_id)
        assert await database.get_token(token_id) is None

        restored_manifest, extracted = await decrypt_and_extract_postgres_archive(
            encrypted,
            tmp_path / "restore",
        )
        await restore_postgres_dump(database, extracted / "database" / "sub2gen.dump")
        await verify_restored_row_counts(database, restored_manifest["row_counts"])
        restored = await database.get_token(token_id)
        assert restored is not None
        assert restored.email == "backup-contract@example.com"
        assert manifest["row_counts"] == restored_manifest["row_counts"]
        assert (extracted / "browser_profiles" / "token-1" / "Default" / "Cookies").read_bytes() == b"profile-data"
    finally:
        await database.close_runtime_connections()
        from psycopg import AsyncConnection, sql

        cleanup = await AsyncConnection.connect(POSTGRES_URL)
        try:
            await cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            await cleanup.commit()
        finally:
            await cleanup.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not REDIS_URL, reason="SUB2GEN_TEST_REDIS_URL is not configured")
async def test_real_redis_atomic_limits_and_maintenance_marker():
    from sub2gen.services.redis_runtime import RedisRuntime

    runtime = RedisRuntime(url=REDIS_URL, mode="required")
    await runtime.initialize_state(force=True)
    assert runtime.ready is True
    try:
        await runtime.client.flushdb()
        await runtime.initialize_state(force=True)
        await runtime.enforce_rate_limits(key_id=1, endpoint="/contract", rpm=2, rph=10, now=1_800_000_000)
        await runtime.enforce_rate_limits(key_id=1, endpoint="/contract", rpm=2, rph=10, now=1_800_000_000)
        with pytest.raises(RuntimeError, match="Rate limit exceeded"):
            await runtime.enforce_rate_limits(key_id=1, endpoint="/contract", rpm=2, rph=10, now=1_800_000_000)

        await runtime.touch_presence(1)
        assert await runtime.is_present(1) is True
        maintenance = await runtime.set_maintenance(True, reason="contract_test", owner="pytest")
        assert maintenance["active"] is True
        assert maintenance["reason"] == "contract_test"
        maintenance = await runtime.set_maintenance(False)
        assert maintenance["active"] is False
    finally:
        await runtime.client.flushdb()
        await runtime.stop()
