from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from sub2gen.workers.extension.legacy_codec import (
    LegacyExtensionCodecError,
    decode_legacy_extension_message,
    encode_legacy_extension_message,
)
from sub2gen_gateway.legacy_codec import (
    LegacyAgentGatewayCodecError,
    decode_legacy_agent_gateway_message,
    encode_legacy_agent_gateway_message,
)


FIXTURE_ROOT = Path(__file__).parent / "contracts" / "legacy-workers"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PLACEHOLDER = re.compile(r"^<[a-z][a-z0-9-]*>$")
ALLOWED_DIRECTIONS = {
    "worker_to_server",
    "server_to_worker",
    "worker_to_server_http",
}

REQUIRED_SCENARIOS = {
    "captcha_ws.legacy-unversioned": {
        "registration",
        "captcha_solve_success",
        "captcha_solve_error",
        "refresh_success",
        "refresh_error",
        "generation_submit_direct_success",
        "generation_poll_large_upload_success",
        "generation_poll_error",
        "heartbeat_ping",
        "client_shutdown",
    },
    "agent_gateway.legacy-unversioned": {
        "register_legacy_shared_secret",
        "register_keygen_agent",
        "solve_success",
        "solve_error",
        "heartbeat_ping_rejected",
    },
}

MESSAGE_REQUIRED_KEYS = {
    "register": {"type"},
    "register_ack": {
        "type",
        "worker_session_id",
        "instance_id",
        "binding_source",
        "allow_captcha",
        "allow_session_refresh",
        "allow_generation",
        "status",
    },
    "registered": {"type", "subject", "auth_method"},
    "error": {"type", "detail"},
    "get_token": {"type", "req_id", "action", "project_id", "managed_api_key_id"},
    "captcha_upstream_verdict": {"type", "req_id", "accepted", "captcha_rejected"},
    "refresh_st": {"type", "req_id", "token_id"},
    "submit_generation": {"type", "req_id", "url", "method", "headers", "json_data"},
    "submit_generation_result": {"type", "req_id", "status"},
    "poll_generation": {"type", "req_id", "url", "method", "headers", "json_data"},
    "poll_generation_result": {"type", "req_id", "status"},
    "ping": {"type"},
    "client_shutdown": {"type", "worker_session_id"},
    "solve_job": {"type", "job_id", "project_id", "action"},
    "solve_result": {"type", "job_id", "token", "session_id", "fingerprint"},
    "solve_error": {"type", "job_id", "error"},
}

SECRET_KEYS = {
    "agent_token",
    "agent_token_id",
    "authorization",
    "device_token",
    "key",
    "large_response_upload_id",
    "session_id",
    "session_token",
    "token",
    "upload_id",
    "upload_secret",
    "worker_session_id",
}
BROWSER_VALUE_KEYS = {
    "browser_profile",
    "cookie",
    "cookies",
    "fingerprint",
    "language",
    "profile_path",
    "user_agent",
    "user_data_dir",
}


def load_fixtures() -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURE_ROOT.glob("*.json"))]


def walk(value: Any, path: tuple[str, ...] = ()):
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, (*path, str(index)))


class GatewayRecordingWebSocket:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = [json.dumps(message) for message in messages]
        self.accepted = False
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if not self.messages:
            raise WebSocketDisconnect(code=1000)
        return self.messages.pop(0)

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self, *args: Any, **kwargs: Any) -> None:
        return None


class GatewayRecordingRegistry:
    def __init__(self) -> None:
        self.registered = False
        self.unregistered = False

    async def register_agent(self, websocket: Any, **identity: Any) -> None:
        self.registered = True

    async def unregister(self, websocket: Any) -> None:
        self.unregistered = True


@pytest.mark.parametrize("fixture", load_fixtures(), ids=lambda item: item["dialect"])
def test_legacy_worker_fixture_envelope(fixture: dict[str, Any]) -> None:
    assert fixture["fixture_version"] == 1
    assert fixture["dialect"] in REQUIRED_SCENARIOS
    assert fixture["sources"]
    assert all(Path(source).suffix in {".py", ".ts"} for source in fixture["sources"])
    assert all((REPOSITORY_ROOT / source).is_file() for source in fixture["sources"])

    codec = fixture["codec"]
    assert codec["encoding"] == "utf-8 JSON text frames"
    assert codec["protocol_version_field"] is None
    assert codec["correlation_field"] in {"req_id", "job_id"}
    assert codec["first_worker_message"] == "register"

    scenario_names = {scenario["name"] for scenario in fixture["scenarios"]}
    assert scenario_names == REQUIRED_SCENARIOS[fixture["dialect"]]


@pytest.mark.parametrize("fixture", load_fixtures(), ids=lambda item: item["dialect"])
def test_legacy_worker_frames_have_characterized_shapes(fixture: dict[str, Any]) -> None:
    for scenario in fixture["scenarios"]:
        assert scenario["frames"], scenario["name"]
        for frame in scenario["frames"]:
            assert frame["direction"] in ALLOWED_DIRECTIONS
            if frame["direction"] == "worker_to_server_http":
                request = frame["request"]
                assert request["method"] == "POST"
                assert request["path"] == "/api/extension/generation-upload"
                assert set(request["query"]) == {"upload_id", "upload_secret"}
                continue

            message = frame["message"]
            assert isinstance(message, dict)
            message_type = message.get("type")
            if message_type is None:
                # Legacy CAPTCHA and refresh results are correlated solely by req_id.
                assert fixture["dialect"] == "captcha_ws.legacy-unversioned"
                assert {"req_id", "status"} <= set(message)
                continue
            assert message_type in MESSAGE_REQUIRED_KEYS
            assert MESSAGE_REQUIRED_KEYS[message_type] <= set(message)


def test_generation_fixtures_freeze_large_upload_metadata() -> None:
    fixture = next(item for item in load_fixtures() if item["dialect"] == "captcha_ws.legacy-unversioned")
    scenarios = {item["name"]: item for item in fixture["scenarios"]}

    for name in ("generation_submit_direct_success", "generation_poll_large_upload_success"):
        request = scenarios[name]["frames"][0]["message"]
        assert set(request["large_response_upload"]) == {
            "upload_id",
            "upload_secret",
            "upload_path",
            "threshold_bytes",
            "force_http_upload",
        }
        # The current dispatcher does not propagate the timeout to the extension.
        assert "timeout_ms" not in request

    poll_frames = scenarios["generation_poll_large_upload_success"]["frames"]
    assert [frame["direction"] for frame in poll_frames] == [
        "server_to_worker",
        "worker_to_server_http",
        "worker_to_server",
    ]
    assert poll_frames[-1]["message"]["large_response_upload_id"] == "<upload-id>"


@pytest.mark.parametrize("fixture", load_fixtures(), ids=lambda item: item["dialect"])
def test_legacy_worker_frames_round_trip_through_executable_codecs(
    fixture: dict[str, Any],
) -> None:
    for scenario in fixture["scenarios"]:
        for frame in scenario["frames"]:
            direction = frame["direction"]
            if direction == "worker_to_server_http":
                continue
            message = frame["message"]
            if fixture["dialect"] == "captcha_ws.legacy-unversioned":
                encoded = encode_legacy_extension_message(message, direction=direction)
                decoded = decode_legacy_extension_message(encoded, direction=direction)
            else:
                encoded = encode_legacy_agent_gateway_message(message, direction=direction)
                decoded = decode_legacy_agent_gateway_message(encoded, direction=direction)
            assert decoded == message, scenario["name"]


def test_legacy_codecs_fail_closed_for_unknown_or_ambiguous_frames() -> None:
    with pytest.raises(LegacyExtensionCodecError):
        decode_legacy_extension_message(
            '{"type":"arbitrary_browser_eval"}',
            direction="server_to_worker",
        )
    with pytest.raises(LegacyExtensionCodecError):
        decode_legacy_extension_message('{"status":"success"}', direction="worker_to_server")
    with pytest.raises(LegacyAgentGatewayCodecError):
        decode_legacy_agent_gateway_message('{"status":"success"}', direction="worker_to_server")


def test_agent_gateway_ping_rejection_matches_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    auth_stub = ModuleType("sub2gen_gateway.auth_keygen")

    async def verify_agent_token(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("legacy fixture must not call Keygen authentication")

    auth_stub.verify_agent_token = verify_agent_token  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sub2gen_gateway.auth_keygen", auth_stub)
    gateway_module = importlib.import_module("sub2gen_gateway.ws_agents")

    fixture = next(
        item for item in load_fixtures() if item["dialect"] == "agent_gateway.legacy-unversioned"
    )
    scenarios = {item["name"]: item for item in fixture["scenarios"]}
    register = scenarios["register_legacy_shared_secret"]["frames"]
    heartbeat = scenarios["heartbeat_ping_rejected"]["frames"]
    websocket = GatewayRecordingWebSocket([register[0]["message"], heartbeat[0]["message"]])
    registry = GatewayRecordingRegistry()

    monkeypatch.setattr(
        gateway_module,
        "load_settings",
        lambda: SimpleNamespace(
            agent_auth_mode="legacy",
            agent_device_token="<device-token>",
        ),
    )
    monkeypatch.setattr(gateway_module, "registry", registry)

    asyncio.run(gateway_module.ws_agents(websocket))

    assert websocket.accepted is True
    assert registry.registered is True
    assert registry.unregistered is True
    assert websocket.sent == [register[1]["message"], heartbeat[1]["message"]]


@pytest.mark.parametrize("fixture", load_fixtures(), ids=lambda item: item["dialect"])
def test_required_placeholders_are_declared_and_used(fixture: dict[str, Any]) -> None:
    required = fixture["required_placeholders"]
    assert required
    assert len(required) == len(set(required))
    assert all(PLACEHOLDER.fullmatch(value) for value in required)

    scalars = {value for _path, value in walk(fixture) if isinstance(value, str)}
    assert set(required) <= scalars


@pytest.mark.parametrize("fixture", load_fixtures(), ids=lambda item: item["dialect"])
def test_legacy_worker_fixtures_contain_no_credentials_or_browser_data(fixture: dict[str, Any]) -> None:
    serialized = json.dumps(fixture, sort_keys=True)
    forbidden_fragments = (
        "s2g_live_",
        "__Secure-",
        "SAPISID",
        "SID=",
        "/Users/",
        "User Data/",
        "Profile 1",
        "Google Chrome",
        "eyJ",
    )
    assert not any(fragment in serialized for fragment in forbidden_fragments)

    for path, value in walk(fixture):
        if not path or not isinstance(value, str):
            continue
        key = path[-1].lower()
        if key in SECRET_KEYS:
            assert PLACEHOLDER.fullmatch(value), ".".join(path)
        if key in BROWSER_VALUE_KEYS and key != "fingerprint":
            assert PLACEHOLDER.fullmatch(value), ".".join(path)


def test_legacy_correlation_and_lifecycle_defects_remain_explicit() -> None:
    fixtures = {item["dialect"]: item for item in load_fixtures()}
    captcha = fixtures["captcha_ws.legacy-unversioned"]
    gateway = fixtures["agent_gateway.legacy-unversioned"]

    assert captcha["codec"]["correlation_field"] == "req_id"
    assert captcha["codec"]["result_type_required"] is False
    assert gateway["codec"]["correlation_field"] == "job_id"
    assert gateway["codec"]["result_type_required"] is True

    captcha_scenarios = {item["name"]: item for item in captcha["scenarios"]}
    gateway_scenarios = {item["name"]: item for item in gateway["scenarios"]}
    assert captcha_scenarios["heartbeat_ping"]["outcome"]["response_message"] is None
    assert captcha_scenarios["client_shutdown"]["outcome"]["websocket_close"]["code"] == 1000
    assert gateway_scenarios["heartbeat_ping_rejected"]["frames"][-1]["message"] == {
        "type": "error",
        "detail": "unknown type 'ping'",
    }
