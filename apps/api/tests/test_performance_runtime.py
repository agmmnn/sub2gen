import asyncio
import gzip
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sub2gen.api import admin
from sub2gen.core.api_key_manager import ApiKeyManager
from sub2gen.core.database import Database
from sub2gen.core.models import GeminiGenTask, RequestLog
from sub2gen.services.failed_payload_store import FailedPayloadManager
from sub2gen.services.redis_runtime import (
    RedisEvent,
    RedisRuntime,
    RedisUnavailableError,
    is_new_protected_work,
)


class _AuthDb:
    async def get_client_api_key_by_hash(self, _key_hash):
        return {
            "id": 1,
            "label": "test",
            "is_active": True,
            "scopes": "*",
            "expires_at": None,
        }

    async def get_api_key_account_ids(self, _key_id):
        return [2]

    async def get_api_key_rate_limits(self, _key_id, _endpoint):
        return {}

    async def touch_api_key_usage(self, _key_id):
        return None


class _UnavailableRuntime:
    mode = "required"
    required = True
    ready = False

    def ensure_ready(self):
        raise RedisUnavailableError("redis_unavailable")


class TestRedisSafety(unittest.IsolatedAsyncioTestCase):
    async def test_probe_warms_sqlite_before_reporting_ready(self):
        class ProbeClient:
            async def ping(self):
                return True

            async def get(self, _key):
                return b"1"

            def register_script(self, _script):
                return object()

        runtime = RedisRuntime(url="redis://unused", mode="required")
        runtime.client = ProbeClient()
        runtime.db = object()
        warm_ready_states = []

        async def ensure_group():
            return None

        async def warm(db):
            self.assertIs(db, runtime.db)
            warm_ready_states.append(runtime.ready)
            return {"auth_records": 1, "active_tasks": 2}

        runtime._ensure_consumer_group = ensure_group
        runtime._warm_from_sqlite = warm
        await runtime._connect_and_probe()

        self.assertEqual(warm_ready_states, [False])
        self.assertTrue(runtime.ready)
        self.assertTrue(runtime.status_snapshot()["state_warmed"])

    async def test_required_mode_fails_before_new_work_auth_reads(self):
        manager = ApiKeyManager(_AuthDb(), lambda: "", redis_runtime=_UnavailableRuntime())
        with self.assertRaises(RedisUnavailableError):
            await manager.authenticate(
                "managed",
                endpoint="/v1/chat/completions",
                require_redis=True,
            )

    async def test_rate_limit_script_result_is_enforced(self):
        runtime = RedisRuntime(url="redis://unused", mode="required")
        runtime.ready = True

        async def rate_script(*, keys, args):
            self.assertEqual(len(keys), 2)
            self.assertEqual(args[:2], [1, 0])
            return [1, 2, 0]

        runtime._rate_script = rate_script
        with self.assertRaisesRegex(RuntimeError, "requests/min"):
            await runtime.enforce_rate_limits(
                key_id=7,
                endpoint="/v1/chat/completions",
                rpm=1,
                rph=0,
                now=120,
            )

    def test_new_work_classifier_keeps_polling_available(self):
        self.assertTrue(is_new_protected_work("POST", "/v1/chat/completions"))
        self.assertTrue(is_new_protected_work("POST", "/models/x:generateContent"))
        self.assertFalse(is_new_protected_work("GET", "/v1/jobs/job-1"))
        self.assertFalse(is_new_protected_work("POST", "/v1/runway/tasks/job-1/cancel"))
        self.assertFalse(is_new_protected_work("POST", "/api/client/presence"))


class _Body:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value

    def close(self):
        return None


class _SpacesClient:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = bytes(kwargs["Body"])
        return {}

    def get_object(self, **kwargs):
        return {"Body": _Body(self.objects[kwargs["Key"]])}


class TestLogStorageAndRetention(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tempdir.name) / "sub2gen.db"))
        await self.db.init_db()
        await self.db.cache_schema_capabilities()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_success_is_summary_only_and_failure_is_redacted_gzip(self):
        manager = FailedPayloadManager()
        await manager.start(self.db, enabled=False)
        manager.enabled = True
        manager.configured = True
        manager.last_error = ""
        manager._client = _SpacesClient()
        self.db.set_log_payload_manager(manager)
        try:
            success_id = await self.db.add_request_log(
                RequestLog(
                    operation="success",
                    request_body=json.dumps({"prompt": "x" * 5000}),
                    response_body=json.dumps({"ok": True}),
                    status_code=200,
                    duration=0.1,
                    status_text="completed",
                    progress=100,
                )
            )
            success = await self.db.get_log_detail(success_id)
            self.assertLessEqual(len(success["request_body"]), 1024)
            self.assertGreater(success["request_size_bytes"], 5000)
            self.assertFalse(success["payload_available"])

            failure_id = await self.db.add_request_log(
                RequestLog(
                    operation="failure",
                    request_body=json.dumps({"api_key": "super-secret", "prompt": "bad"}),
                    response_body=json.dumps({"error": "upstream failed"}),
                    status_code=500,
                    duration=0.2,
                    status_text="failed",
                    progress=0,
                )
            )
            await asyncio.wait_for(manager._queue.join(), timeout=3)
            failure = await self.db.get_log_detail(failure_id)
            self.assertTrue(failure["payload_available"])
            compressed = manager._client.objects[failure["payload_object_key"]]
            stored = json.loads(gzip.decompress(compressed).decode("utf-8"))
            self.assertEqual(stored["request_body"]["api_key"], "<redacted>")
            self.assertEqual(stored["response_body"]["error"], "upstream failed")
        finally:
            await manager.stop()

    async def test_retention_is_bounded_and_preserves_active_tasks(self):
        old_log = await self.db.add_request_log(
            RequestLog(operation="old", status_code=200, duration=0.1, status_text="completed")
        )
        await self.db.insert_api_key_audit_log(
            api_key_id=None,
            endpoint="/old",
            account_id=None,
            status_code=200,
            detail="old",
            ip="127.0.0.1",
            user_agent="test",
        )
        await self.db.create_geminigen_task(
            GeminiGenTask(
                job_id="old-terminal",
                public_model_id="model",
                kind="image",
                endpoint_type="imagen",
                status="completed",
                completed_at=datetime.utcnow(),
            )
        )
        await self.db.create_geminigen_task(
            GeminiGenTask(
                job_id="old-active",
                public_model_id="model",
                kind="image",
                endpoint_type="imagen",
                status="processing",
            )
        )
        async with self.db._connect(write=True) as connection:
            cursor = await connection.execute(
                """
                INSERT INTO request_logs (
                    operation, request_body, response_body, status_code, duration,
                    status_text, request_excerpt, response_excerpt
                ) VALUES (?, ?, ?, ?, ?, ?, '', '')
                """,
                (
                    "legacy-recent",
                    json.dumps({"api_key": "legacy-secret", "prompt": "keep summary"}),
                    json.dumps({"authorization": "Bearer old-secret", "ok": True}),
                    200,
                    0.1,
                    "completed",
                ),
            )
            recent_log = int(cursor.lastrowid)
            await connection.commit()
        async with self.db._connect(write=True) as connection:
            await connection.execute(
                "UPDATE request_logs SET created_at=datetime('now','-8 days') WHERE id=?",
                (old_log,),
            )
            await connection.execute("UPDATE api_key_audit_logs SET created_at=datetime('now','-8 days')")
            await connection.execute(
                "UPDATE geminigen_tasks SET created_at=datetime('now','-8 days'), updated_at=datetime('now','-8 days'), completed_at=datetime('now','-8 days') WHERE job_id='old-terminal'"
            )
            await connection.execute(
                "UPDATE geminigen_tasks SET created_at=datetime('now','-8 days'), updated_at=datetime('now','-8 days') WHERE job_id='old-active'"
            )
            await connection.commit()

        counts = await self.db.cleanup_retention_batch(days=7, batch_size=1)
        self.assertLessEqual(counts["request_logs"], 1)
        self.assertLessEqual(counts["api_key_audit_logs"], 1)
        self.assertLessEqual(counts["geminigen_tasks"], 1)
        self.assertIsNone(await self.db.get_geminigen_task("old-terminal"))
        self.assertIsNotNone(await self.db.get_geminigen_task("old-active"))
        recent = await self.db.get_log_detail(recent_log)
        self.assertNotIn("legacy-secret", recent["request_body"])
        self.assertNotIn("old-secret", recent["response_body"])
        self.assertIn("<redacted>", recent["request_body"])


class _WebSocketDb:
    async def is_admin_session_valid(self, token):
        return token == "valid"


class _WebSocketRuntime:
    def __init__(self):
        self.ready = True
        self.websocket_clients = 0
        self.calls = 0

    async def cursor_was_trimmed(self, _cursor):
        return False

    def status_snapshot(self):
        return {"redis_ready": True, "event_consumer_ready": True}

    async def read_events(self, _cursor, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return [RedisEvent("10-0", "request_summary_created", {"id": 8}, "now")]
        raise RedisUnavailableError("redis_unavailable")


class TestAdminWebSocket(unittest.TestCase):
    def test_cookie_auth_replay_and_1013_outage(self):
        original_runtime = admin.redis_runtime
        runtime = _WebSocketRuntime()
        admin.redis_runtime = runtime
        app = FastAPI()
        app.state.container = SimpleNamespace(db=_WebSocketDb())
        app.include_router(admin.router)
        try:
            client = TestClient(app)
            with client.websocket_connect(
                "/api/admin/events/ws?cursor=9-0",
                cookies={admin.ADMIN_SESSION_COOKIE_NAME: "valid"},
                headers={"origin": "http://testserver"},
            ) as websocket:
                self.assertEqual(websocket.receive_json()["type"], "redis_state")
                event = websocket.receive_json()
                self.assertEqual(event["cursor"], "10-0")
                self.assertEqual(event["data"]["id"], 8)
                with self.assertRaises(WebSocketDisconnect) as disconnected:
                    websocket.receive_json()
                self.assertEqual(disconnected.exception.code, 1013)
            self.assertEqual(runtime.websocket_clients, 0)
        finally:
            admin.redis_runtime = original_runtime

    def test_cookie_is_required(self):
        app = FastAPI()
        app.state.container = SimpleNamespace(db=_WebSocketDb())
        app.include_router(admin.router)
        client = TestClient(app)
        with self.assertRaises(WebSocketDisconnect) as disconnected:
            with client.websocket_connect(
                "/api/admin/events/ws",
                headers={"origin": "http://testserver"},
            ):
                pass
        self.assertEqual(disconnected.exception.code, 1008)

    def test_cross_origin_is_rejected(self):
        app = FastAPI()
        app.state.container = SimpleNamespace(db=_WebSocketDb())
        app.include_router(admin.router)
        client = TestClient(app)
        with self.assertRaises(WebSocketDisconnect) as disconnected:
            with client.websocket_connect(
                "/api/admin/events/ws",
                cookies={admin.ADMIN_SESSION_COOKIE_NAME: "valid"},
                headers={"origin": "https://attacker.example"},
            ):
                pass
        self.assertEqual(disconnected.exception.code, 1008)


if __name__ == "__main__":
    unittest.main()
