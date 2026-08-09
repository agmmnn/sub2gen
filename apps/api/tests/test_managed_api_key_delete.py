import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from fastapi import HTTPException

from sub2gen.api import admin, routes
from sub2gen.core.database import Database
from sub2gen.core.models import GeminiGenTask, RequestLog, RunwayTask
from sub2gen.services.browser_captcha_extension import ExtensionCaptchaService


def test_database_delete_managed_api_key_detaches_history_and_removes_owned_rows():
    async def run():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        try:
            await db.init_db()
            key_id = await db.create_client_api_key(
                client_name="delete-test",
                label="delete-test",
                key_prefix="test-prefix",
                key_plaintext="test-secret",
                key_hash="test-hash",
                scopes="geminigen:generate",
                account_ids=[],
                endpoint_limits={"*": {"rpm": 10, "rph": 100, "burst": 2}},
                expires_at=None,
            )
            log_id = await db.add_request_log(
                RequestLog(
                    api_key_id=key_id,
                    operation="geminigen_image",
                    request_body="{}",
                    response_body="{}",
                    status_code=200,
                    duration=0.1,
                )
            )
            await db.create_runway_task(
                RunwayTask(
                    job_id="runway-key-delete",
                    api_key_id=key_id,
                    public_model_id="runway-test",
                    status="completed",
                )
            )
            await db.create_geminigen_task(
                GeminiGenTask(
                    job_id="geminigen-key-delete",
                    api_key_id=key_id,
                    public_model_id="geminigen-nano-banana-pro-image-landscape-1k",
                    kind="image",
                    endpoint_type="imagen",
                    status="completed",
                )
            )
            await db.insert_api_key_audit_log(
                api_key_id=key_id,
                endpoint="/v1/images/generations",
                account_id=None,
                status_code=200,
                detail="ok",
                ip="127.0.0.1",
                user_agent="pytest",
            )
            await db.upsert_cache_file(
                filename="owned.png",
                api_key_id=key_id,
                token_id=None,
                media_type="image/png",
            )
            await db.upsert_extension_worker_binding("delete-route", key_id)

            result = await db.delete_api_key(key_id)
            async with db._connect() as connection:
                connection.row_factory = aiosqlite.Row
                key_row = await (await connection.execute("SELECT id FROM api_keys WHERE id = ?", (key_id,))).fetchone()
                request_log = await (
                    await connection.execute("SELECT api_key_id FROM request_logs WHERE id = ?", (log_id,))
                ).fetchone()
                runway_task = await (
                    await connection.execute("SELECT api_key_id FROM runway_tasks WHERE job_id = 'runway-key-delete'")
                ).fetchone()
                geminigen_task = await (
                    await connection.execute("SELECT api_key_id FROM geminigen_tasks WHERE job_id = 'geminigen-key-delete'")
                ).fetchone()
                audit_row = await (
                    await connection.execute("SELECT api_key_id FROM api_key_audit_logs WHERE endpoint = '/v1/images/generations'")
                ).fetchone()
                cache_count = int(
                    (await (await connection.execute("SELECT COUNT(*) FROM cache_files WHERE api_key_id = ?", (key_id,))).fetchone())[0]
                )
                binding_count = int(
                    (await (await connection.execute("SELECT COUNT(*) FROM extension_worker_bindings WHERE api_key_id = ?", (key_id,))).fetchone())[0]
                )
                foreign_key_errors = await (await connection.execute("PRAGMA foreign_key_check")).fetchall()
            return {
                "result": result,
                "key_row": key_row,
                "request_log": request_log,
                "runway_task": runway_task,
                "geminigen_task": geminigen_task,
                "audit_row": audit_row,
                "cache_count": cache_count,
                "binding_count": binding_count,
                "foreign_key_errors": foreign_key_errors,
            }
        finally:
            os.unlink(tmp.name)

    state = asyncio.run(run())

    assert state["result"]["deleted"] is True
    assert state["result"]["historical_records_detached"] == 4
    assert state["result"]["cache_metadata_deleted"] == 1
    assert state["result"]["extension_bindings_deleted"] == 1
    assert state["key_row"] is None
    assert state["request_log"]["api_key_id"] is None
    assert state["runway_task"]["api_key_id"] is None
    assert state["geminigen_task"]["api_key_id"] is None
    assert state["audit_row"]["api_key_id"] is None
    assert state["cache_count"] == 0
    assert state["binding_count"] == 0
    assert state["foreign_key_errors"] == []


def test_admin_delete_removes_cache_objects_and_managed_worker_sessions(monkeypatch):
    fake_db = SimpleNamespace(
        get_api_key_detail=AsyncMock(return_value={"id": 10}),
        list_cache_files_for_api_key_cleanup=AsyncMock(
            return_value=[{"filename": "one.png"}, {"filename": "missing.mp4"}]
        ),
        delete_api_key=AsyncMock(
            return_value={
                "deleted": True,
                "historical_records_detached": 3,
                "cache_metadata_deleted": 2,
                "extension_bindings_deleted": 1,
            }
        ),
    )
    backend = SimpleNamespace(delete=AsyncMock(side_effect=[True, False]))
    extension_service = SimpleNamespace(kill_managed_api_key_sessions=AsyncMock(return_value=2))
    monkeypatch.setattr(ExtensionCaptchaService, "get_instance", AsyncMock(return_value=extension_service))

    container = SimpleNamespace(
        db=fake_db,
        generation_handler=SimpleNamespace(file_cache=SimpleNamespace(backend=backend)),
        api_key_manager=SimpleNamespace(invalidate=AsyncMock()),
    )
    result = asyncio.run(
        admin.delete_managed_api_key(10, token="admin-token", container=container)
    )

    assert result["success"] is True
    assert result["cache_objects_deleted"] == 1
    assert result["worker_sessions_terminated"] == 2
    assert result["historical_records_detached"] == 3
    assert backend.delete.await_args_list[0].args == ("one.png",)
    assert backend.delete.await_args_list[1].args == ("missing.mp4",)
    fake_db.delete_api_key.assert_awaited_once_with(10)
    extension_service.kill_managed_api_key_sessions.assert_awaited_once_with(10)


def test_admin_delete_keeps_key_when_cache_cleanup_fails(monkeypatch):
    fake_db = SimpleNamespace(
        get_api_key_detail=AsyncMock(return_value={"id": 10}),
        list_cache_files_for_api_key_cleanup=AsyncMock(return_value=[{"filename": "owned.png"}]),
        delete_api_key=AsyncMock(),
    )
    backend = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("storage unavailable")))
    container = SimpleNamespace(
        db=fake_db,
        generation_handler=SimpleNamespace(file_cache=SimpleNamespace(backend=backend)),
        api_key_manager=SimpleNamespace(invalidate=AsyncMock()),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            admin.delete_managed_api_key(10, token="admin-token", container=container)
        )

    assert exc_info.value.status_code == 503
    assert "not deleted" in str(exc_info.value.detail)
    fake_db.delete_api_key.assert_not_awaited()


class FakeExtensionWebSocket:
    def __init__(self, instance_id: str):
        self.query_params = {"instance_id": instance_id, "client_label": instance_id}
        self.closed = False

    async def accept(self):
        return None

    async def close(self, *args, **kwargs):
        self.closed = True


def test_deleted_managed_api_key_sessions_are_terminated():
    async def run():
        service = ExtensionCaptchaService(db=None)
        matching = FakeExtensionWebSocket("managed-delete")
        other = FakeExtensionWebSocket("managed-keep")
        await service.connect(matching, authenticated_managed_api_key_id=10)
        await service.connect(other, authenticated_managed_api_key_id=11)
        killed = await service.kill_managed_api_key_sessions(10)
        return service, matching, other, killed

    service, matching, other, killed = asyncio.run(run())

    assert killed == 1
    assert matching.closed is True
    assert other.closed is False
    assert len(service.active_connections) == 1
    assert service.active_connections[0].websocket is other
