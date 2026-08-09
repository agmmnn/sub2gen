"""Local device pairing, proof-of-possession authentication, and policy checks."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .generated import WorkerPolicy


class DeviceAuthError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    worker_id: str
    public_key: bytes = field(repr=False)


@dataclass(slots=True)
class _PairingTicket:
    digest: str
    expires_at: datetime
    used: bool = False


@dataclass(slots=True)
class _Device:
    worker_id: str
    public_key: bytes
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(slots=True)
class _Challenge:
    challenge_id: str
    worker_id: str
    nonce: bytes
    expires_at: datetime
    used: bool = False

    @property
    def signed_bytes(self) -> bytes:
        return self.challenge_id.encode() + b"." + self.nonce


class PairingAuthority:
    def __init__(self) -> None:
        self._tickets: dict[str, _PairingTicket] = {}
        self._devices: dict[str, _Device] = {}
        self._challenges: dict[str, _Challenge] = {}

    def create_pairing_code(self, *, ttl_seconds: int = 300) -> str:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        code = secrets.token_urlsafe(24)
        digest = hashlib.sha256(code.encode()).hexdigest()
        self._tickets[digest] = _PairingTicket(digest, _now() + timedelta(seconds=ttl_seconds))
        return code

    def pair(
        self,
        *,
        code: str,
        worker_id: str,
        public_key: bytes,
        credential_ttl_seconds: int = 30 * 24 * 3600,
    ) -> DeviceIdentity:
        digest = hashlib.sha256(code.encode()).hexdigest()
        ticket = self._tickets.get(digest)
        if ticket is None or ticket.used or ticket.expires_at <= _now():
            raise DeviceAuthError("pairing code is invalid or expired")
        if not worker_id.strip() or credential_ttl_seconds < 1:
            raise DeviceAuthError("invalid device identity")
        try:
            Ed25519PublicKey.from_public_bytes(public_key)
        except ValueError as exc:
            raise DeviceAuthError("invalid Ed25519 public key") from exc
        ticket.used = True
        self._devices[worker_id] = _Device(
            worker_id=worker_id,
            public_key=bytes(public_key),
            expires_at=_now() + timedelta(seconds=credential_ttl_seconds),
        )
        return DeviceIdentity(worker_id=worker_id, public_key=bytes(public_key))

    def issue_challenge(self, worker_id: str, *, ttl_seconds: int = 30) -> tuple[str, str, datetime]:
        device = self._active_device(worker_id)
        del device
        challenge_id = f"challenge_{secrets.token_hex(16)}"
        challenge = _Challenge(
            challenge_id=challenge_id,
            worker_id=worker_id,
            nonce=secrets.token_bytes(32),
            expires_at=_now() + timedelta(seconds=ttl_seconds),
        )
        self._challenges[challenge_id] = challenge
        return challenge_id, base64.b64encode(challenge.nonce).decode(), challenge.expires_at

    def trust_device(self, *, worker_id: str, public_key: bytes, expires_at: datetime) -> None:
        """Load an already-paired durable device after a server restart."""

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        try:
            Ed25519PublicKey.from_public_bytes(public_key)
        except ValueError as exc:
            raise DeviceAuthError("invalid Ed25519 public key") from exc
        self._devices[worker_id] = _Device(worker_id, bytes(public_key), expires_at)

    def has_active_device(self, worker_id: str) -> bool:
        try:
            self._active_device(worker_id)
        except DeviceAuthError:
            return False
        return True

    def challenge_bytes(self, challenge_id: str) -> bytes:
        challenge = self._challenges.get(challenge_id)
        if challenge is None:
            raise DeviceAuthError("unknown challenge")
        return challenge.signed_bytes

    def authenticate(self, *, worker_id: str, challenge_id: str, signature: str) -> None:
        device = self._active_device(worker_id)
        challenge = self._challenges.get(challenge_id)
        if (
            challenge is None
            or challenge.worker_id != worker_id
            or challenge.used
            or challenge.expires_at <= _now()
        ):
            raise DeviceAuthError("challenge is invalid or expired")
        try:
            decoded = base64.b64decode(signature, validate=True)
            Ed25519PublicKey.from_public_bytes(device.public_key).verify(decoded, challenge.signed_bytes)
        except (ValueError, InvalidSignature) as exc:
            raise DeviceAuthError("device signature is invalid") from exc
        challenge.used = True

    def revoke(self, worker_id: str) -> None:
        device = self._active_device(worker_id)
        device.revoked_at = _now()

    def _active_device(self, worker_id: str) -> _Device:
        device = self._devices.get(worker_id)
        if device is None or device.revoked_at is not None or device.expires_at <= _now():
            raise DeviceAuthError("device credential is unavailable")
        return device


def authorize_job(
    policy: WorkerPolicy,
    *,
    server_id: str,
    capability: str,
    model: str | None,
    account_id: str | None,
    active_jobs: int,
    jobs_today: int,
) -> None:
    if server_id not in policy.allowed_servers:
        raise DeviceAuthError("server is not allowed")
    if capability not in policy.allowed_capabilities:
        raise DeviceAuthError("capability is not allowed")
    if policy.allowed_models and (model is None or model not in policy.allowed_models):
        raise DeviceAuthError("model is not allowed")
    if policy.allowed_accounts and (account_id is None or account_id not in policy.allowed_accounts):
        raise DeviceAuthError("provider account is not allowed")
    if active_jobs >= policy.max_concurrency:
        raise DeviceAuthError("worker concurrency is exhausted")
    if policy.daily_job_limit is not None and jobs_today >= policy.daily_job_limit:
        raise DeviceAuthError("worker daily job limit is exhausted")


def _now() -> datetime:
    return datetime.now(timezone.utc)
