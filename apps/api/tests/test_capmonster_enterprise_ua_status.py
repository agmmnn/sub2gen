import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sub2gen.api import admin
from sub2gen.core.config import config
from sub2gen.core.database import Database
from sub2gen.services.flow_client import FlowClient
from sub2gen.services.generation_handler import GenerationHandler


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(self.responses.pop(0))


class _RequestLogDatabase:
    def __init__(self):
        self.updates = []

    async def update_request_log(self, log_id, **kwargs):
        self.updates.append((log_id, kwargs))


class CaptchaConfigTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_captcha = copy.deepcopy(config._config.get("captcha", {}))
        config._config["captcha"] = copy.deepcopy(self.original_captcha)

    async def asyncTearDown(self):
        config._config["captcha"] = self.original_captcha

    def _set_captcha(self, **values):
        config._config.setdefault("captcha", {}).update(values)

    async def test_capmonster_runtime_uses_enterprise_task_without_standard_fallback(self):
        self._set_captcha(
            capmonster_api_key="test-key",
            capmonster_base_url="https://capmonster.test",
        )
        session = _FakeSession(
            [
                {"errorId": 0, "taskId": "task-1"},
                {
                    "errorId": 0,
                    "status": "ready",
                    "solution": {"gRecaptchaResponse": "captcha-token"},
                },
            ]
        )
        client = FlowClient(proxy_manager=None, db=None)

        with patch("sub2gen.services.flow_client.AsyncSession", return_value=session):
            result = await client._get_api_captcha_token("capmonster", "project-1")

        self.assertEqual(result, ("captcha-token", None))
        task = session.calls[0][1]["json"]["task"]
        self.assertEqual(task["type"], "RecaptchaV3EnterpriseTask")
        self.assertEqual(task["minScore"], 0.9)
        self.assertNotIn("userAgent", task)
        self.assertNotEqual(task["type"], "RecaptchaV3TaskProxyless")

        failed_session = _FakeSession([{"errorId": 1, "errorDescription": "unsupported"}])
        with patch("sub2gen.services.flow_client.AsyncSession", return_value=failed_session):
            failed = await client._get_api_captcha_token("capmonster", "project-1")
        self.assertIsNone(failed)
        self.assertEqual(len(failed_session.calls), 1)
        self.assertEqual(
            failed_session.calls[0][1]["json"]["task"]["type"],
            "RecaptchaV3EnterpriseTask",
        )

    async def test_yescaptcha_configured_task_type_remains_unchanged(self):
        self._set_captcha(
            yescaptcha_api_key="test-key",
            yescaptcha_base_url="https://yescaptcha.test",
            yescaptcha_task_type="RecaptchaV3TaskProxylessM1S9",
        )
        session = _FakeSession(
            [
                {"errorId": 0, "taskId": "task-2"},
                {
                    "errorId": 0,
                    "status": "ready",
                    "solution": {"gRecaptchaResponse": "captcha-token"},
                },
            ]
        )
        client = FlowClient(proxy_manager=None, db=None)

        with patch("sub2gen.services.flow_client.AsyncSession", return_value=session):
            await client._get_api_captcha_token("yescaptcha", "project-2")

        task = session.calls[0][1]["json"]["task"]
        self.assertEqual(task["type"], "RecaptchaV3TaskProxylessM1S9")
        self.assertEqual(task["minScore"], 0.9)

    async def test_admin_capmonster_helper_uses_enterprise_task(self):
        self._set_captcha(
            capmonster_api_key="test-key",
            capmonster_base_url="https://capmonster.test",
            capmonster_min_score=0.6,
        )
        session = _FakeSession(
            [
                {"errorId": 0, "taskId": "task-3"},
                {
                    "errorId": 0,
                    "status": "ready",
                    "solution": {"gRecaptchaResponse": "captcha-token"},
                },
            ]
        )
        with patch("sub2gen.api.admin.AsyncSession", return_value=session):
            token = await admin._solve_recaptcha_with_api_service(
                "capmonster",
                "https://labs.google/fx/tools/flow/project/project-3",
                "site-key",
                "IMAGE_GENERATION",
                enterprise=True,
                proxy_manager=None,
            )

        self.assertEqual(token, "captcha-token")
        self.assertEqual(
            session.calls[0][1]["json"]["task"]["type"],
            "RecaptchaV3EnterpriseTask",
        )
        self.assertEqual(session.calls[0][1]["json"]["task"]["minScore"], 0.6)

    async def test_capmonster_minimum_score_persists_in_database(self):
        with tempfile.TemporaryDirectory() as tempdir:
            database = Database(str(Path(tempdir) / "sub2gen.db"))
            await database.init_db()
            await database.check_and_migrate_db({})
            await database.update_captcha_config(capmonster_min_score=0.7)
            stored = await database.get_captcha_config()
            self.assertEqual(stored.capmonster_min_score, 0.7)
            await database.reload_config_to_memory()
            self.assertEqual(config.capmonster_min_score, 0.7)

            await database.update_captcha_config(capmonster_min_score=2.0)
            clamped = await database.get_captcha_config()
            self.assertEqual(clamped.capmonster_min_score, 0.9)

    async def test_admin_rejects_capmonster_minimum_score_outside_range(self):
        below = await admin.update_captcha_config(
            {"capmonster_min_score": 0.09},
            token="test-admin",
        )
        above = await admin.update_captcha_config(
            {"capmonster_min_score": 0.91},
            token="test-admin",
        )
        invalid = await admin.update_captcha_config(
            {"capmonster_min_score": "not-a-number"},
            token="test-admin",
        )
        self.assertFalse(below["success"])
        self.assertFalse(above["success"])
        self.assertFalse(invalid["success"])

    async def test_provider_user_agent_is_applied_and_emitted_without_raw_value(self):
        self._set_captcha(captcha_method="capmonster")
        client = FlowClient(proxy_manager=None, db=None)
        client._get_api_captcha_token = AsyncMock(
            return_value=("captcha-token", "Provider-UA/123")
        )
        events = []

        async def progress_hook(payload):
            events.append(payload)

        token, browser_id = await client._get_recaptcha_token(
            "project-4",
            poll_task_progress=progress_hook,
        )

        self.assertEqual(token, "captcha-token")
        self.assertIsNone(browser_id)
        self.assertEqual(client.get_request_fingerprint()["user_agent"], "Provider-UA/123")
        self.assertEqual(
            events,
            [
                {
                    "captcha_status": "user_agent_set",
                    "captcha_user_agent_set": True,
                    "captcha_provider": "capmonster",
                }
            ],
        )
        self.assertNotIn("Provider-UA/123", json.dumps(events))

    async def test_missing_provider_user_agent_does_not_emit_set_ua(self):
        self._set_captcha(captcha_method="capmonster")
        client = FlowClient(proxy_manager=None, db=None)
        client._get_api_captcha_token = AsyncMock(return_value=("captcha-token", None))
        events = []

        async def progress_hook(payload):
            events.append(payload)

        token, _ = await client._get_recaptcha_token(
            "project-5",
            poll_task_progress=progress_hook,
        )

        self.assertEqual(token, "captcha-token")
        self.assertEqual(events, [])


class RequestLogUserAgentStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_ua_metadata_persists_through_progress_and_completion(self):
        database = _RequestLogDatabase()
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.db = database
        state = {
            "id": 11,
            "progress": 38,
            "api_key_id": 4,
            "request_id": "gen-test",
            "captcha_user_agent_set": False,
            "captcha_provider": None,
        }
        hook = handler._build_poll_task_progress_hook(
            None,
            request_log_state=state,
            token_id=7,
        )

        await hook(
            {
                "captcha_status": "user_agent_set",
                "captcha_user_agent_set": True,
                "captcha_provider": "capmonster",
            }
        )
        await handler._update_request_log_progress(
            state,
            token_id=7,
            status_text="submitting_image",
            progress=48,
        )
        await handler._log_request(
            token_id=7,
            api_key_id=4,
            operation="generate_image",
            request_data={"model": "test"},
            response_data={"status": "success"},
            status_code=200,
            duration=1.0,
            log_id=11,
            status_text="completed",
            progress=100,
            request_log_state=state,
        )

        self.assertEqual(database.updates[0][1]["status_text"], "captcha_user_agent_set")
        self.assertEqual(database.updates[0][1]["progress"], 38)
        for _, update in database.updates:
            payload = json.loads(update["response_body"])
            self.assertTrue(payload["captcha_user_agent_set"])
            self.assertEqual(payload["captcha_provider"], "capmonster")
            self.assertNotIn("userAgent", update["response_body"])

    def test_admin_metadata_extraction_handles_truncated_json(self):
        payload = (
            '{"captcha_user_agent_set": true, "captcha_provider": "capmonster", '
            '"performance": {"large": "truncated'
        )
        self.assertEqual(
            admin._extract_captcha_user_agent_metadata(payload),
            {
                "captcha_user_agent_set": True,
                "captcha_provider": "capmonster",
            },
        )


if __name__ == "__main__":
    unittest.main()
