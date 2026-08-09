from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class DeviceIdentity:
    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self.private_key = private_key

    @property
    def public_key_base64(self) -> str:
        raw = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode()

    def sign(self, value: bytes) -> str:
        return base64.b64encode(self.private_key.sign(value)).decode()

    @classmethod
    def load_or_create(cls, path: Path) -> "DeviceIdentity":
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            return cls(Ed25519PrivateKey.from_private_bytes(base64.b64decode(value["private_key"])))
        identity = cls(Ed25519PrivateKey.generate())
        raw = identity.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"private_key": base64.b64encode(raw).decode()}) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return identity
