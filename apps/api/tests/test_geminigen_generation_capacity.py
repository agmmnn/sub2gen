import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sub2gen.api import routes
from sub2gen.core.api_key_manager import ApiKeyManager
from sub2gen.core.database import Database


class GeminiGenCapacityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tempdir.name) / "sub2gen.db"))
        await self.db.init_db()

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def _create_account(
        self,
        label,
        *,
        bearer_token="credential",
        is_active=True,
        image_concurrency=5,
        video_concurrency=5,
    ):
        account_id = await self.db.create_geminigen_account(
            label=label,
            raw_cookie="",
            bearer_token=bearer_token,
            is_active=is_active,
            image_concurrency=image_concurrency,
            video_concurrency=video_concurrency,
        )
        # create_geminigen_account keeps its legacy zero-to-default behavior, while an
        # explicit admin update may disable one lane with zero.
        if image_concurrency == 0 or video_concurrency == 0:
            await self.db.update_geminigen_account(
                account_id,
                image_concurrency=image_concurrency,
                video_concurrency=video_concurrency,
            )
        return account_id

    async def test_four_default_accounts_return_twenty_threads_per_lane(self):
        for index in range(4):
            await self._create_account(f"Account {index + 1}")

        self.assertEqual(
            await self.db.get_geminigen_generation_capacity(),
            {"image_threads": 20, "video_threads": 20},
        )

    async def test_capacity_uses_active_credentialed_accounts_and_finite_reporting_rules(self):
        daily_limited_id = await self._create_account("Daily limited")
        await self.db.update_geminigen_account(
            daily_limited_id,
            image_gen_daily_limit_reset_at="2099-01-01 00:00:00",
            video_daily_limit_reset_at="2099-01-01 00:00:00",
        )
        await self._create_account("Custom", image_concurrency=3, video_concurrency=7)
        unlimited_id = await self._create_account("Unlimited", image_concurrency=-1, video_concurrency=-1)
        await self._create_account("Disabled lanes", image_concurrency=0, video_concurrency=0)
        await self._create_account("Inactive", is_active=False, image_concurrency=100, video_concurrency=100)
        await self._create_account("Missing credential", bearer_token="", image_concurrency=100, video_concurrency=100)

        self.assertEqual(
            await self.db.get_geminigen_generation_capacity(),
            {"image_threads": 13, "video_threads": 17},
        )
        unlimited = await self.db.get_geminigen_account(unlimited_id)
        self.assertEqual(unlimited.image_concurrency, -1)
        self.assertEqual(unlimited.video_concurrency, -1)


class _CapacityAuthDatabase:
    def __init__(self, scopes):
        self.scopes = scopes

    async def get_client_api_key_by_hash(self, _key_hash):
        return {
            "id": 7,
            "label": "Nexus",
            "is_active": True,
            "scopes": self.scopes,
            "expires_at": None,
        }

    async def get_api_key_account_ids(self, _key_id):
        return []

    async def get_api_key_rate_limits(self, _key_id, _endpoint):
        return []

    async def touch_api_key_usage(self, _key_id):
        return None

    async def insert_api_key_audit_log(self, **_kwargs):
        return None


class _CapacityDatabase:
    async def get_geminigen_generation_capacity(self):
        return {"image_threads": 20, "video_threads": 15}


class GeminiGenCapacityEndpointTests(unittest.TestCase):
    def _client(self, scopes):
        api_key_manager = ApiKeyManager(_CapacityAuthDatabase(scopes), lambda: "")
        app = FastAPI()
        app.state.container = SimpleNamespace(
            api_key_manager=api_key_manager,
            db=_CapacityDatabase(),
        )
        app.include_router(routes.router)
        return TestClient(app)

    def test_endpoint_returns_aggregate_totals_only(self):
        response = self._client("geminigen:generate").get(
            "/v1/generation-capacity",
            headers={"Authorization": "Bearer s2g_live_test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "object": "generation_capacity",
                "providers": {
                    "geminigen": {"image_threads": 20, "video_threads": 15}
                },
            },
        )
        serialized = json.dumps(response.json()).lower()
        self.assertNotIn("account", serialized)
        self.assertNotIn("credential", serialized)
        self.assertNotIn("token", serialized)

    def test_endpoint_requires_geminigen_scope(self):
        response = self._client("models:read").get(
            "/v1/generation-capacity",
            headers={"Authorization": "Bearer s2g_live_test"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Missing scope: geminigen:generate")


if __name__ == "__main__":
    unittest.main()
