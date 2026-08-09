"""Provider-neutral persistence records.

List-facing models intentionally omit credential locators and authentication hashes.
Resolver-facing models remain internal to the execution plane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4


def new_public_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class CredentialStorageKind(StrEnum):
    LEGACY_TABLE = "legacy_table"
    ENV = "env"
    WORKER_VAULT = "worker_vault"
    BROWSER_SESSION = "browser_session"


class GenerationJobStatus(StrEnum):
    QUEUED = "queued"
    OFFERED = "offered"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class GenerationAttemptStatus(StrEnum):
    OFFERED = "offered"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


def _frozen_metadata(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class ProviderAccountRecord:
    provider_key: str
    label: str
    id: str = field(default_factory=lambda: new_public_id("pa"))
    external_account_id: str | None = None
    enabled: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    legacy_source: str | None = None
    legacy_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class CredentialBindingRecord:
    provider_account_id: str
    binding_key: str
    credential_type: str
    storage_kind: CredentialStorageKind
    secret_ref: str = field(repr=False)
    id: str = field(default_factory=lambda: new_public_id("cb"))
    worker_id: str | None = None
    enabled: bool = True
    expires_at: str | None = None
    last_validated_at: str | None = None
    last_error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class CredentialBindingView:
    id: str
    provider_account_id: str
    binding_key: str
    credential_type: str
    storage_kind: CredentialStorageKind
    worker_id: str | None
    enabled: bool
    expires_at: str | None
    last_validated_at: str | None
    last_error: str | None
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class WorkerDeviceRecord:
    kind: str
    label: str
    approved_capabilities: tuple[str, ...]
    id: str = field(default_factory=lambda: new_public_id("worker"))
    enabled: bool = True
    auth_key_hash: str | None = field(default=None, repr=False)
    public_key: str | None = field(default=None, repr=False)
    credential_expires_at: str | None = None
    revoked_at: str | None = None
    last_seen_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "approved_capabilities", tuple(sorted(set(self.approved_capabilities))))
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class WorkerDeviceView:
    id: str
    kind: str
    label: str
    approved_capabilities: tuple[str, ...]
    enabled: bool
    credential_expires_at: str | None
    revoked_at: str | None
    last_seen_at: str | None
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "approved_capabilities", tuple(sorted(set(self.approved_capabilities))))
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class GenerationJobRecord:
    request_id: str
    job_kind: str
    requested_model: str
    id: str = field(default_factory=lambda: new_public_id("job"))
    idempotency_key: str | None = None
    api_key_id: int | None = None
    status: GenerationJobStatus = GenerationJobStatus.QUEUED
    provider_account_id: str | None = None
    worker_id: str | None = None
    resolved_execution: Mapping[str, Any] | None = None
    deadline_at: str | None = None
    terminal_at: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.resolved_execution is not None:
            object.__setattr__(self, "resolved_execution", _frozen_metadata(self.resolved_execution))


@dataclass(frozen=True, slots=True)
class GenerationAttemptRecord:
    job_id: str
    attempt: int
    status: GenerationAttemptStatus
    id: str = field(default_factory=lambda: new_public_id("attempt"))
    lease_id: str | None = None
    provider_job_id: str | None = None
    resolved_execution: Mapping[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None
    error_detail: str | None = None

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("attempt must be at least one")
        if self.resolved_execution is not None:
            object.__setattr__(self, "resolved_execution", _frozen_metadata(self.resolved_execution))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
