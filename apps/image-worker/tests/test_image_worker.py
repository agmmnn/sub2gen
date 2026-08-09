from __future__ import annotations

import os

from sub2gen_image_worker.config import WorkerConfig
from sub2gen_image_worker.identity import DeviceIdentity


def test_worker_config_roundtrip_and_websocket_url(tmp_path) -> None:
    path = tmp_path / "worker.json"
    config = WorkerConfig(
        server_url="https://sub2gen.example.test",
        worker_id="worker-1",
        capabilities=("image.generate:chatgpt-web",),
    )
    config.save(path)

    restored = WorkerConfig.load(path)
    assert restored == config
    assert restored.websocket_url == "wss://sub2gen.example.test/worker_ws"
    assert "private" not in path.read_text()


def test_device_identity_is_stable_and_private_file_is_restricted(tmp_path) -> None:
    path = tmp_path / "identity.json"
    first = DeviceIdentity.load_or_create(path)
    second = DeviceIdentity.load_or_create(path)

    assert first.public_key_base64 == second.public_key_base64
    assert first.sign(b"challenge") == second.sign(b"challenge")
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
