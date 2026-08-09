"""Application integration for durable worker pairing and device authentication."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from sub2gen_worker_protocol.security import DeviceAuthError, PairingAuthority

from ..persistence.domain import WorkerDeviceRecord
from ..persistence.unified_repositories import WorkerDeviceRepository


class PersistentDevicePairing:
    def __init__(self, devices: WorkerDeviceRepository) -> None:
        self._devices = devices
        self._authority = PairingAuthority()

    def create_pairing_code(self, *, ttl_seconds: int = 300) -> str:
        return self._authority.create_pairing_code(ttl_seconds=ttl_seconds)

    async def pair(
        self,
        *,
        code: str,
        worker_id: str,
        kind: str,
        label: str,
        public_key_base64: str,
        approved_capabilities: tuple[str, ...],
        credential_ttl_seconds: int = 30 * 24 * 3600,
    ) -> WorkerDeviceRecord:
        try:
            public_key = base64.b64decode(public_key_base64, validate=True)
        except ValueError as exc:
            raise DeviceAuthError("invalid public-key encoding") from exc
        identity = self._authority.pair(
            code=code,
            worker_id=worker_id,
            public_key=public_key,
            credential_ttl_seconds=credential_ttl_seconds,
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=credential_ttl_seconds)
        return await self._devices.register(
            WorkerDeviceRecord(
                id=identity.worker_id,
                kind=kind,
                label=label,
                approved_capabilities=approved_capabilities,
                public_key=public_key_base64,
                credential_expires_at=expires_at.isoformat(),
            )
        )

    async def issue_challenge(self, worker_id: str) -> tuple[str, str, datetime]:
        await self._load_device(worker_id)
        return self._authority.issue_challenge(worker_id)

    def challenge_bytes(self, challenge_id: str) -> bytes:
        return self._authority.challenge_bytes(challenge_id)

    async def authenticate(self, *, worker_id: str, challenge_id: str, signature: str) -> None:
        await self._load_device(worker_id)
        self._authority.authenticate(
            worker_id=worker_id,
            challenge_id=challenge_id,
            signature=signature,
        )
        await self._devices.heartbeat(worker_id)

    async def revoke(self, worker_id: str) -> bool:
        if self._authority.has_active_device(worker_id):
            self._authority.revoke(worker_id)
        return await self._devices.revoke(worker_id)

    async def _load_device(self, worker_id: str) -> WorkerDeviceRecord:
        device = await self._devices.get_for_auth(worker_id)
        if (
            device is None
            or not device.enabled
            or device.revoked_at is not None
            or not device.public_key
            or not device.credential_expires_at
        ):
            raise DeviceAuthError("device credential is unavailable")
        if not self._authority.has_active_device(worker_id):
            try:
                public_key = base64.b64decode(device.public_key, validate=True)
                expires_at = datetime.fromisoformat(device.credential_expires_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise DeviceAuthError("stored device credential is invalid") from exc
            self._authority.trust_device(
                worker_id=worker_id,
                public_key=public_key,
                expires_at=expires_at,
            )
        return device
