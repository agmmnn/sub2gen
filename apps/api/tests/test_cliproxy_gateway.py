import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from sub2gen.api.cliproxy_admin import router as cliproxy_router
from sub2gen.core.cliproxy_models import CLIProxyAlias, CLIProxyApiKeyImport
from sub2gen.services.cliproxy_client import (
    CLIProxyInferenceClient,
    CLIProxyManagementClient,
    CLIProxyUpstreamError,
    namespaced_model,
    prepare_credential_imports,
    redact_structure,
    redact_text,
    split_gateway_model,
)
from sub2gen.services.llm_provider_chain import LlmProviderChain


class _NoopManagement:
    def __init__(self):
        self.models = []

    async def ensure_namespaced_alias(self, model):
        self.models.append(model)


class _FakeAsyncClient:
    calls = []
    response = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        self.__class__.calls.append(("POST", url, kwargs))
        return self.__class__.response

    async def request(self, method, url, **kwargs):
        self.__class__.calls.append((method, url, kwargs))
        return self.__class__.response


class _StubManagement(CLIProxyManagementClient):
    def __init__(self, responses=None):
        super().__init__(base_url="http://gateway", management_key="management-secret")
        self.responses = responses or {}
        self.calls = []

    async def _request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        value = self.responses.get((method, endpoint), self.responses.get(endpoint, {}))
        return value() if callable(value) else value


class CLIProxyInferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_completion_uses_client_key_and_multimodal_payload(self):
        request = httpx.Request("POST", "http://gateway/v1/chat/completions")
        _FakeAsyncClient.calls = []
        _FakeAsyncClient.response = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "```json\n{\"title\": \"ok\"}\n```"}}]},
            request=request,
        )
        management = _NoopManagement()
        client = CLIProxyInferenceClient(
            base_url="http://gateway", api_key="client-secret", management=management
        )
        with patch("sub2gen.services.cliproxy_client.httpx.AsyncClient", _FakeAsyncClient):
            result = await client.chat_json(
                model="codex/gpt-5.6",
                prompt_text="return metadata",
                image_bytes=b"image-bytes",
                mime_type="image/png",
            )

        self.assertEqual(result, {"title": "ok"})
        self.assertEqual(management.models, ["codex/gpt-5.6"])
        _, url, kwargs = _FakeAsyncClient.calls[0]
        self.assertEqual(url, "http://gateway/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer client-secret")
        self.assertFalse(kwargs["json"]["stream"])
        self.assertNotIn("response_format", kwargs["json"])
        content = kwargs["json"]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    async def test_upstream_error_redacts_secrets(self):
        request = httpx.Request("POST", "http://gateway/v1/chat/completions")
        _FakeAsyncClient.response = httpx.Response(
            500,
            text="authorization=Bearer upstream-secret api_key=sk-sensitive",
            request=request,
        )
        client = CLIProxyInferenceClient(
            base_url="http://gateway", api_key="client-secret", management=_NoopManagement()
        )
        with patch("sub2gen.services.cliproxy_client.httpx.AsyncClient", _FakeAsyncClient):
            with self.assertRaises(HTTPException) as raised:
                await client.chat_json(model="codex/gpt", prompt_text="x")
        self.assertNotIn("upstream-secret", raised.exception.detail)
        self.assertNotIn("sk-sensitive", raised.exception.detail)
        self.assertIn("[REDACTED]", raised.exception.detail)


class CLIProxyManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_cockpit_antigravity_bundle_imports_refresh_tokens(self):
        client = _StubManagement()
        bundle = [
            {
                "email": "gemini-one@example.com",
                "refresh_token": "refresh-one",
                "tags": ["private"],
                "notes": "do-not-forward",
            },
            {
                "email": "gemini-two@example.com",
                "token": {
                    "access_token": "access-two",
                    "refresh_token": "refresh-two",
                    "expires_in": 3600,
                    "expiry_timestamp": 1_800_000_000,
                    "project_id": "project-two",
                },
            },
        ]

        result = await client.import_credential_file(
            platform="antigravity",
            filename="antigravity_accounts.json",
            content=json.dumps(bundle).encode(),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.imported, 2)
        upload_calls = [call for call in client.calls if call[:2] == ("POST", "auth-files")]
        self.assertEqual(len(upload_calls), 2)
        payloads = [json.loads(call[2]["content"]) for call in upload_calls]
        self.assertEqual(payloads[0]["type"], "antigravity")
        self.assertEqual(payloads[0]["access_token"], "")
        self.assertEqual(payloads[0]["refresh_token"], "refresh-one")
        self.assertEqual(payloads[1]["access_token"], "access-two")
        self.assertEqual(payloads[1]["project_id"], "project-two")
        rendered = json.dumps(payloads)
        self.assertNotIn("do-not-forward", rendered)
        self.assertNotIn("notes", rendered)
        self.assertNotIn("tags", rendered)

    async def test_cockpit_bundle_imports_all_accounts_and_strips_unrelated_secrets(self):
        client = _StubManagement()
        bundle = [
            {
                "type": "codex",
                "email": "owner@example.com",
                "access_token": "access-one",
                "refresh_token": "refresh-one",
                "id_token": "id-one",
                "account_id": "account-one",
                "account_password": "do-not-forward",
                "two_factor_secret": "do-not-forward-either",
            },
            {
                "id": "cockpit-account-two",
                "email": "owner@example.com",
                "account_id": "account-two",
                "tokens": {
                    "access_token": "access-two",
                    "refresh_token": "refresh-two",
                    "id_token": "id-two",
                },
            },
        ]

        result = await client.import_credential_file(
            platform="codex",
            filename="codex_accounts.json",
            content=json.dumps(bundle).encode(),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.total, 2)
        self.assertEqual(result.imported, 2)
        self.assertEqual(result.failed, 0)
        upload_calls = [call for call in client.calls if call[:2] == ("POST", "auth-files")]
        self.assertEqual(len(upload_calls), 2)
        names = [call[2]["params"]["name"] for call in upload_calls]
        self.assertEqual(
            names,
            ["codex-owner@example.com.json", "codex-owner@example.com-2.json"],
        )
        payloads = [json.loads(call[2]["content"]) for call in upload_calls]
        self.assertEqual(payloads[0]["type"], "codex")
        self.assertEqual(payloads[1]["access_token"], "access-two")
        rendered = json.dumps(payloads)
        self.assertNotIn("do-not-forward", rendered)
        self.assertNotIn("account_password", rendered)
        self.assertNotIn("two_factor_secret", rendered)

    async def test_cockpit_batch_reports_sanitized_partial_failures(self):
        class PartiallyFailingClient(_StubManagement):
            async def _request(self, method, endpoint, **kwargs):
                self.calls.append((method, endpoint, kwargs))
                if b"access-two" in kwargs.get("content", b""):
                    raise CLIProxyUpstreamError(
                        409,
                        "refresh_token=upstream-secret was rejected",
                    )
                return {}

        bundle = [
            {"type": "codex", "email": "one@example.com", "access_token": "access-one"},
            {"type": "codex", "email": "two@example.com", "access_token": "access-two"},
        ]
        result = await PartiallyFailingClient().import_credential_file(
            platform="codex",
            filename="codex_accounts.json",
            content=json.dumps(bundle).encode(),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.imported, 1)
        self.assertEqual(result.failed, 1)
        self.assertNotIn("upstream-secret", result.items[1].error)
        self.assertIn("[REDACTED]", result.items[1].error)

    async def test_accounts_are_normalized_without_paths_or_credentials(self):
        client = _StubManagement(
            {
                "auth-files": {
                    "files": [
                        {
                            "id": "account-1",
                            "auth_index": "stable-index",
                            "name": "owner@example.com.json",
                            "provider": "anthropic",
                            "label": "Production",
                            "email": "owner@example.com",
                            "status": "ready",
                            "path": "/data/auths/secret.json",
                            "access_token": "never-return-this",
                            "success": 7,
                            "failed": 2,
                        }
                    ]
                }
            }
        )
        accounts = await client.list_accounts()
        self.assertEqual(len(accounts), 1)
        account = accounts[0]
        self.assertEqual(account.platform, "claude")
        self.assertEqual(account.auth_index, "stable-index")
        self.assertEqual(account.success_count, 7)
        dumped = json.dumps(account.model_dump())
        self.assertNotIn("/data/auths", dumped)
        self.assertNotIn("never-return-this", dumped)

    async def test_account_oauth_routing_and_quota_map_to_allowlisted_operations(self):
        client = _StubManagement(
            {
                "codex-auth-url": {"status": "ok", "url": "https://login", "state": "codex-1"},
                "get-auth-status": {"status": "wait"},
                "routing/strategy": {"strategy": "round-robin"},
                "request-retry": {"request-retry": 2},
                "max-retry-interval": {"max-retry-interval": 15},
                "reset-quota": {"status": "ok", "auth_index": "stable-index"},
            }
        )
        await client.set_account_enabled("owner@example.com.json", False)
        await client.delete_account("owner@example.com.json")
        oauth = await client.start_oauth("codex")
        await client.oauth_status("codex-1", "codex")
        routing = await client.routing()
        await client.set_routing("fill-first", retry_count=3, max_retry_interval_seconds=20)
        await client.reset_quota("stable-index")

        self.assertEqual(oauth.state, "codex-1")
        self.assertEqual(routing.strategy, "round-robin")
        endpoints = [call[1] for call in client.calls]
        self.assertEqual(
            endpoints,
            [
                "auth-files/status",
                "auth-files",
                "codex-auth-url",
                "get-auth-status",
                "routing/strategy",
                "request-retry",
                "max-retry-interval",
                "routing/strategy",
                "request-retry",
                "max-retry-interval",
                "routing/strategy",
                "request-retry",
                "max-retry-interval",
                "reset-quota",
            ],
        )
        self.assertNotIn("api-call", endpoints)
        self.assertNotIn("config", endpoints)

    async def test_aliases_exclusions_logs_and_api_keys_are_sanitized(self):
        client = _StubManagement(
            {
                "oauth-model-alias": {
                    "oauth-model-alias": {
                        "codex": [{"name": "gpt", "alias": "codex/gpt"}]
                    }
                },
                "oauth-excluded-models": {"oauth-excluded-models": {"codex": ["old-gpt"]}},
                "logs": {
                    "lines": ["authorization: Bearer token-value api_key=secret-value"],
                    "next-cursor": "cursor-2",
                },
                "gemini-api-key": {"gemini-api-key": []},
            }
        )
        aliases = await client.list_aliases()
        exclusions = await client.exclusions()
        logs = await client.logs(limit=25)
        result = await client.import_api_key(
            CLIProxyApiKeyImport(provider="gemini", api_key="gemini-secret", models=["gemini-flash"])
        )

        self.assertEqual(aliases["codex"][0].alias, "codex/gpt")
        self.assertEqual(exclusions, {"codex": ["old-gpt"]})
        self.assertNotIn("token-value", logs.lines[0])
        self.assertNotIn("secret-value", logs.lines[0])
        self.assertNotIn("gemini-secret", json.dumps(result))
        put_call = next(call for call in client.calls if call[0] == "PUT")
        self.assertEqual(put_call[1], "gemini-api-key")
        self.assertEqual(put_call[2]["json_body"][0]["models"][0]["alias"], "gemini/gemini-flash")

    async def test_namespaced_alias_is_added_only_through_alias_endpoint(self):
        client = _StubManagement({"oauth-model-alias": {"oauth-model-alias": {}}})
        await client.ensure_namespaced_alias("codex/gpt-5.6")
        patch_calls = [call for call in client.calls if call[0] == "PATCH"]
        self.assertEqual(len(patch_calls), 1)
        self.assertEqual(patch_calls[0][1], "oauth-model-alias")
        self.assertEqual(patch_calls[0][2]["json_body"]["channel"], "codex")


class CLIProxyProviderChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_is_first_and_failure_falls_back_to_downstream_model(self):
        class Chain(LlmProviderChain):
            def __init__(self):
                self.calls = []

            async def _invoke_cliproxy(self, model, *args, **kwargs):
                self.calls.append(("cliproxy", model))
                raise HTTPException(status_code=502, detail="gateway unavailable")

            async def _invoke_openrouter(self, model, *args, **kwargs):
                self.calls.append(("openrouter", model))
                if model == "openai/gpt-5.6":
                    raise HTTPException(status_code=404, detail="model unavailable")
                return {"ok": True}

        chain = Chain()
        providers = chain.resolve_provider_chain(
            None,
            provider_order_csv="",
            enabled_providers_csv="cliproxy,openrouter",
            legacy_backend="openrouter",
            allowed_providers=["cliproxy", "openrouter"],
        )
        result = await chain.invoke_with_provider_chain(
            providers=providers,
            retry_count=0,
            model="codex/gpt-5.6",
            fallback_models=["moonshotai/kimi-k2.6"],
            prompt_text="json",
            image_bytes=None,
            mime_type="image/png",
        )
        self.assertEqual(providers, ["cliproxy", "openrouter"])
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            chain.calls,
            [
                ("cliproxy", "codex/gpt-5.6"),
                ("openrouter", "openai/gpt-5.6"),
                ("openrouter", "moonshotai/kimi-k2.6"),
            ],
        )


class CLIProxySecurityTests(unittest.TestCase):
    def test_cockpit_bundle_rejects_api_key_accounts_and_non_object_items(self):
        with self.assertRaises(HTTPException) as api_key_error:
            prepare_credential_imports(
                platform="codex",
                filename="accounts.json",
                content=json.dumps([{"auth_mode": "apikey", "OPENAI_API_KEY": "secret"}]).encode(),
            )
        self.assertEqual(api_key_error.exception.status_code, 400)
        self.assertNotIn("secret", api_key_error.exception.detail)

        with self.assertRaises(HTTPException) as shape_error:
            prepare_credential_imports(
                platform="codex",
                filename="accounts.json",
                content=b'["not-an-account"]',
            )
        self.assertEqual(shape_error.exception.status_code, 400)

        with self.assertRaises(HTTPException) as unsupported_bundle:
            prepare_credential_imports(
                platform="gemini",
                filename="accounts.json",
                content=json.dumps([{"refresh_token": "secret"}]).encode(),
            )
        self.assertEqual(unsupported_bundle.exception.status_code, 400)
        self.assertNotIn("secret", unsupported_bundle.exception.detail)

    def test_model_namespaces_and_secret_redaction(self):
        self.assertEqual(namespaced_model("anthropic", "sonnet"), "claude/sonnet")
        self.assertEqual(split_gateway_model("xai/grok-4.5"), ("xai", "grok-4.5"))
        self.assertIsNone(split_gateway_model("moonshotai/kimi-k2"))
        redacted = redact_structure(
            {"access_token": "secret", "nested": {"cookie": "session", "message": "api_key=abc"}}
        )
        rendered = json.dumps(redacted)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("session", rendered)
        self.assertNotIn("abc", rendered)
        self.assertNotIn("raw-secret", redact_text("Bearer raw-secret"))

    def test_admin_router_has_no_arbitrary_forwarding_or_secret_download_routes(self):
        paths = {route.path for route in cliproxy_router.routes}
        self.assertNotIn("/api-call", paths)
        self.assertNotIn("/config", paths)
        self.assertFalse(any("download" in path for path in paths))
        self.assertIn("/accounts/{name}/status", paths)
        self.assertIn("/routing/reset-quota", paths)


if __name__ == "__main__":
    unittest.main()
