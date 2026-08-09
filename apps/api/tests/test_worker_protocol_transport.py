from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.websockets import WebSocketDisconnect
from sub2gen_worker_protocol import WorkerCoordinator
from sub2gen_worker_protocol.codec import decode_envelope, encode_envelope, make_envelope
from sub2gen_worker_protocol.generated import (
    MessageType,
    WorkerHeartbeatPayload,
    WorkerHelloPayload,
    WorkerRegisterPayload,
)

from sub2gen.core.database import Database
from sub2gen.persistence import Repositories
from sub2gen.services.worker_protocol import PersistentDevicePairing
from sub2gen.transport.worker_protocol import worker_websocket_endpoint


class HandshakeSocket:
    def __init__(self, container: Any, pairing: PersistentDevicePairing, private_key: Ed25519PrivateKey):
        self.app = SimpleNamespace(state=SimpleNamespace(container=container))
        self.pairing = pairing
        self.private_key = private_key
        self.sent: list[str] = []
        self.accepted = False
        self.closed: tuple[int, str] | None = None
        self.receives = 0

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, value: str) -> None:
        self.sent.append(value)

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    async def receive_text(self) -> str:
        self.receives += 1
        if self.receives == 1:
            return encode_envelope(
                make_envelope(
                    message_type=MessageType.WORKER_HELLO,
                    worker_id="transport-worker",
                    payload=WorkerHelloPayload(
                        supported_versions=("1.0",),
                        worker_kind="image-worker",
                        instance_id="instance-1",
                    ),
                )
            )
        if self.receives == 2:
            challenge, payload = decode_envelope(self.sent[-1])
            signature = base64.b64encode(
                self.private_key.sign(self.pairing.challenge_bytes(payload.challenge_id))
            ).decode()
            return encode_envelope(
                make_envelope(
                    message_type=MessageType.WORKER_REGISTER,
                    worker_id="transport-worker",
                    correlation_id=challenge.message_id,
                    payload=WorkerRegisterPayload(
                        selected_version="1.0",
                        challenge_id=payload.challenge_id,
                        signature=signature,
                        capabilities=("image.generate:chatgpt-web",),
                        worker_session_id="session-1",
                    ),
                )
            )
        if self.receives == 3:
            return encode_envelope(
                make_envelope(
                    message_type=MessageType.WORKER_HEARTBEAT,
                    worker_id="transport-worker",
                    payload=WorkerHeartbeatPayload(
                        worker_session_id="session-1",
                        active_leases=(),
                        available_slots=1,
                    ),
                )
            )
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_v1_websocket_negotiates_authenticates_and_heartbeats(tmp_path) -> None:
    database = Database(str(tmp_path / "worker-transport.db"))
    await database.init_db()
    repositories = Repositories.from_database(database)
    pairing = PersistentDevicePairing(repositories.workers.devices)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    await pairing.pair(
        code=pairing.create_pairing_code(),
        worker_id="transport-worker",
        kind="image-worker",
        label="Transport worker",
        public_key_base64=base64.b64encode(public_key).decode(),
        approved_capabilities=("image.generate:chatgpt-web",),
    )
    container = SimpleNamespace(
        worker_pairing=pairing,
        worker_coordinator=WorkerCoordinator(),
        repositories=repositories,
    )
    socket = HandshakeSocket(container, pairing, private_key)

    await worker_websocket_endpoint(socket)  # type: ignore[arg-type]

    assert socket.accepted and socket.closed is None
    assert [decode_envelope(frame)[0].message_type for frame in socket.sent] == [
        MessageType.WORKER_CHALLENGE,
        MessageType.WORKER_REGISTERED,
    ]
    device = await repositories.workers.devices.get_for_auth("transport-worker")
    assert device is not None and device.last_seen_at is not None
