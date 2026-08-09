from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from sub2gen.core.config import config
from sub2gen.services.browser_captcha_extension import ExtensionCaptchaService


CONTRACT_PATH = Path(__file__).parent / "contracts" / "extension-worker-registration.json"
JOB_CONTRACT_PATH = Path(__file__).parent / "contracts" / "extension-worker-jobs.json"


class RecordingWebSocket:
    def __init__(
        self,
        query_params: dict[str, str],
        responses: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.query_params = query_params
        self.responses = responses or {}
        self.service: ExtensionCaptchaService | None = None
        self.accepted = False
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, data: str) -> None:
        message = json.loads(data)
        self.sent.append(message)
        response = self.responses.get(message["type"])
        if response is not None:
            assert self.service is not None
            await self.service.handle_message(
                self,
                json.dumps({"req_id": message["req_id"], **response}),
            )

    async def close(self, *args: Any, **kwargs: Any) -> None:
        self.closed = True


def test_extension_worker_registration_transcripts() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    async def run_scenario(scenario: dict[str, Any]) -> None:
        db = type(
            "CharacterizedDatabase",
            (),
            {"update_captcha_worker_key": AsyncMock()},
        )()
        service = ExtensionCaptchaService(db=db)
        websocket = RecordingWebSocket(scenario["query"])

        await service.connect(websocket, **scenario["connect"])
        await service.handle_message(websocket, json.dumps(scenario["inbound"]))

        assert websocket.accepted is True
        assert len(service.active_connections) == 1
        assert len(websocket.sent) == 1

        ack = websocket.sent[0]
        assert re.fullmatch(r"[0-9a-f]{32}", ack["worker_session_id"])
        ack["worker_session_id"] = "<dynamic>"
        assert ack == scenario["expected_ack"]

    for scenario in contract["scenarios"]:
        asyncio.run(run_scenario(scenario))


def without_dynamic_req_id(message: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(message)
    assert re.fullmatch(r"(?:req|gen_req)_[0-9a-f]{32}", normalized["req_id"])
    normalized["req_id"] = "<dynamic>"
    return normalized


def test_extension_worker_job_transcripts() -> None:
    contract = json.loads(JOB_CONTRACT_PATH.read_text(encoding="utf-8"))

    async def run() -> None:
        service = ExtensionCaptchaService(db=None)

        captcha_socket = RecordingWebSocket(
            {"client_label": "end-user", "instance_id": "captcha-contract"},
            {"get_token": contract["captcha"]["response"]},
        )
        captcha_socket.service = service
        await service.connect(captcha_socket, authenticated_managed_api_key_id=42)
        captcha_connection = service.active_connections[0]
        token, request_id = await service._extension_recaptcha_token_once(
            captcha_connection,
            project_id="project-contract",
            action="IMAGE_GENERATION",
            route_key="",
            managed_api_key_id=42,
            timeout=1,
        )
        assert token == "captcha-token-contract"
        assert request_id is not None
        assert service.consume_token_user_agent(request_id) == "Contract-UA/1"
        assert without_dynamic_req_id(captcha_socket.sent[0]) == contract["captcha"]["expected_request"]

        await service.notify_upstream_verdict(
            request_id,
            accepted=True,
            captcha_rejected=False,
            detail="accepted",
        )
        assert without_dynamic_req_id(captcha_socket.sent[1]) == contract["captcha"]["upstream_verdict"]

        refresh_socket = RecordingWebSocket(
            {"client_label": "refresh", "instance_id": "refresh-contract"},
            {"refresh_st": contract["refresh"]["response"]},
        )
        refresh_socket.service = service
        await service.connect(refresh_socket, refresh_token_id=7)
        refresh_result = await service.refresh_session_token(token_id=7, timeout=1)
        assert refresh_result.session_token == "session-token-contract"
        assert refresh_result.failure_code is None
        assert without_dynamic_req_id(refresh_socket.sent[0]) == contract["refresh"]["expected_request"]

        generation_socket = RecordingWebSocket(
            {"client_label": "generation", "instance_id": "generation-contract"},
            {"submit_generation": contract["generation"]["response"]},
        )
        generation_socket.service = service
        await service.connect(generation_socket, authenticated_managed_api_key_id=43)
        with patch.object(
            type(config),
            "extension_generation_large_upload_enabled",
            property(lambda _self: False),
        ):
            generation_result = await service.submit_generation_via_extension(
                url="https://flow.example/generate",
                headers={"x-contract": "characterized"},
                json_data={"prompt": "Characterization prompt"},
                timeout=1,
                managed_api_key_id=43,
            )
        assert generation_result == contract["generation"]["response"] | {
            "req_id": generation_socket.sent[0]["req_id"]
        }
        assert without_dynamic_req_id(generation_socket.sent[0]) == contract["generation"]["expected_request"]


    asyncio.run(run())


def test_reconnect_replaces_the_previous_worker_session() -> None:
    async def run() -> tuple[ExtensionCaptchaService, RecordingWebSocket, RecordingWebSocket]:
        service = ExtensionCaptchaService(db=None)
        first = RecordingWebSocket({"client_label": "first", "instance_id": "shared-instance"})
        second = RecordingWebSocket({"client_label": "second", "instance_id": "shared-instance"})
        await service.connect(first, authenticated_managed_api_key_id=42)
        await service.connect(second, authenticated_managed_api_key_id=42)
        return service, first, second

    service, first, second = asyncio.run(run())

    assert first.closed is True
    assert second.closed is False
    assert [connection.websocket for connection in service.active_connections] == [second]
