import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sub2gen.api import routes
from sub2gen.core.api_key_manager import ApiKeyManager
from sub2gen.core.database import Database
from sub2gen.main import _path_allowed_on_api_only_host


class TestPresenceDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "sub2gen.db")
        self.db = Database(self.db_path)
        await self.db.init_db()
        async with self.db._connect(write=True) as conn:
            cursor = await conn.execute("INSERT INTO api_clients (name) VALUES ('Nexus')")
            client_id = int(cursor.lastrowid)
            await conn.execute(
                """
                INSERT INTO api_keys (client_id, label, key_prefix, key_hash, scopes)
                VALUES (?, 'Desktop user', 's2g_live_test', 'hash', 'models:read')
                """,
                (client_id,),
            )
            await conn.commit()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_presence_freshness_and_disabled_status(self):
        await self.db.touch_api_key_presence(1)
        rows = await self.db.list_api_keys()
        self.assertTrue(rows[0]["is_online"])
        self.assertIsNotNone(rows[0]["last_presence_at"])

        async with self.db._connect(write=True) as conn:
            await conn.execute(
                "UPDATE api_keys SET last_presence_at = datetime('now', '-91 seconds') WHERE id = 1"
            )
            await conn.commit()
        self.assertFalse((await self.db.list_api_keys())[0]["is_online"])

        await self.db.touch_api_key_presence(1)
        await self.db.update_api_key(1, is_active=False)
        self.assertFalse((await self.db.list_api_keys())[0]["is_online"])

    async def test_existing_database_migration_preserves_key(self):
        migrated_path = str(Path(self.tempdir.name) / "legacy.db")
        conn = sqlite3.connect(migrated_path)
        conn.executescript(
            """
            CREATE TABLE api_clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                key_plaintext TEXT,
                key_hash TEXT NOT NULL UNIQUE,
                scopes TEXT DEFAULT '*',
                is_active BOOLEAN DEFAULT 1,
                expires_at TIMESTAMP,
                last_used_at TIMESTAMP,
                adobe_cloning_enabled INTEGER DEFAULT 1,
                adobe_metadata_enabled INTEGER DEFAULT 1,
                adobe_tracker_enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO api_clients (name) VALUES ('Existing');
            INSERT INTO api_keys (client_id, label, key_prefix, key_hash)
            VALUES (1, 'Kept', 's2g_kept', 'kept-hash');
            """
        )
        conn.commit()
        conn.close()

        migrated = Database(migrated_path)
        await migrated.init_db()
        await migrated.check_and_migrate_db({})
        rows = await migrated.list_api_keys()
        self.assertEqual(rows[0]["label"], "Kept")
        self.assertIsNone(rows[0]["last_presence_at"])


class _PresenceAuthDatabase:
    def __init__(self, row):
        self.row = row
        self.presence_key_ids = []

    async def get_client_api_key_by_hash(self, _key_hash):
        return self.row

    async def get_api_key_account_ids(self, _key_id):
        return []

    async def get_api_key_rate_limits(self, _key_id, _endpoint):
        raise AssertionError("presence must not consume rate limits")

    async def touch_api_key_usage(self, _key_id):
        raise AssertionError("presence must not update last_used_at")

    async def insert_api_key_audit_log(self, **_kwargs):
        raise AssertionError("presence must not create audit rows")

    async def touch_api_key_presence(self, key_id):
        self.presence_key_ids.append(key_id)


class TestPresenceEndpoint(unittest.TestCase):
    def _client(self, row, legacy_key=""):
        database = _PresenceAuthDatabase(row)
        app = FastAPI()
        app.state.container = type(
            "TestContainer",
            (),
            {"api_key_manager": ApiKeyManager(database, lambda: legacy_key)},
        )()
        app.include_router(routes.router)
        return TestClient(app), database

    def test_presence_is_allowed_on_the_dedicated_api_host(self):
        self.assertTrue(_path_allowed_on_api_only_host("/api/client/presence"))

    def test_managed_key_reports_without_usage_limit_or_audit_side_effects(self):
        client, database = self._client({
            "id": 7,
            "label": "Nexus user",
            "is_active": True,
            "scopes": "models:read",
            "expires_at": None,
        })
        response = client.post(
            "/api/client/presence",
            headers={"Authorization": "Bearer s2g_live_test"},
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(database.presence_key_ids, [7])

    def test_legacy_invalid_disabled_and_expired_keys_are_rejected(self):
        client, _ = self._client(None, legacy_key="legacy")
        self.assertEqual(
            client.post("/api/client/presence", headers={"Authorization": "Bearer legacy"}).status_code,
            403,
        )
        self.assertEqual(
            client.post("/api/client/presence", headers={"Authorization": "Bearer wrong"}).status_code,
            401,
        )

        client, _ = self._client({"id": 2, "is_active": False, "scopes": "*"})
        self.assertEqual(
            client.post("/api/client/presence", headers={"Authorization": "Bearer disabled"}).status_code,
            401,
        )

        client, _ = self._client({
            "id": 3,
            "is_active": True,
            "scopes": "*",
            "expires_at": "past",
            "expires_unix": int(time.time()) - 1,
        })
        self.assertEqual(
            client.post("/api/client/presence", headers={"Authorization": "Bearer expired"}).status_code,
            401,
        )


if __name__ == "__main__":
    unittest.main()
