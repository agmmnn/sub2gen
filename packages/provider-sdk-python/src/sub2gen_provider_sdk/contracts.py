"""Provider-neutral generation contracts.

The contracts deliberately contain no FastAPI, database, browser, or provider-specific
credential types. Provider credentials are resolved before an execution context is
created and never enter a generation request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

_CREDENTIAL_OPTION_FRAGMENTS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "oauth",
    "password",
    "secret",
    "session_token",
)


class GenerationKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class ProviderEventKind(StrEnum):
    ACCEPTED = "accepted"
    PROGRESS = "progress"
    ARTIFACT = "artifact"
    WARNING = "warning"
    COMPLETED = "completed"
    FAILED = "failed"


class ProviderHealthStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ProviderErrorCode(StrEnum):
    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    POLICY = "policy"
    TRANSIENT = "transient"
    INVALID_INPUT = "invalid_input"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INVALID_OUTPUT = "invalid_output"


@dataclass(frozen=True, slots=True)
class ReferenceInput:
    """A caller-provided reference represented as bytes or a trusted local path."""

    media_type: str
    data: bytes | None = None
    local_path: Path | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if (self.data is None) == (self.local_path is None):
            raise ValueError("reference must contain exactly one of data or local_path")
        if self.data is not None and not self.data:
            raise ValueError("reference data must not be empty")
        if self.local_path is not None and not self.local_path.is_file():
            raise ValueError("reference local_path must point to a file")
        if not self.media_type.strip():
            raise ValueError("reference media_type must not be empty")

    def read_bytes(self) -> bytes:
        return self.data if self.data is not None else self.local_path.read_bytes()  # type: ignore[union-attr]


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    request_id: str
    prompt: str
    model: str
    kind: GenerationKind = GenerationKind.IMAGE
    references: tuple[ReferenceInput, ...] = ()
    count: int = 1
    provider_options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.count < 1:
            raise ValueError("count must be at least one")
        unsafe_keys = [
            str(key)
            for key in self.provider_options
            if any(fragment in str(key).strip().lower() for fragment in _CREDENTIAL_OPTION_FRAGMENTS)
        ]
        if unsafe_keys:
            raise ValueError("provider_options must not contain credentials")
        object.__setattr__(self, "provider_options", MappingProxyType(dict(self.provider_options)))


@dataclass(frozen=True, slots=True)
class ResolvedExecution:
    requested_model: str
    resolved_model: str
    provider_id: str
    billing_pool: str
    provider_account_id: str | None = None
    worker_id: str | None = None


class CancellationToken:
    """Cooperative cancellation shared by orchestrators and provider adapters."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


@dataclass(frozen=True, slots=True)
class ProviderExecutionContext:
    resolved: ResolvedExecution
    cancellation: CancellationToken
    timeout_seconds: float
    attempt: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if self.attempt < 1:
            raise ValueError("attempt must be at least one")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ProviderJob:
    provider_job_id: str
    resumable: bool = False


@dataclass(frozen=True, slots=True)
class Artifact:
    media_type: str
    data: bytes | None = None
    local_path: Path | None = None
    filename: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if (self.data is None) == (self.local_path is None):
            raise ValueError("artifact must contain exactly one of data or local_path")
        if self.data is not None and not self.data:
            raise ValueError("artifact data must not be empty")
        if self.local_path is not None and not self.local_path.is_file():
            raise ValueError("artifact local_path must point to a file")
        if not self.media_type.strip():
            raise ValueError("artifact media_type must not be empty")

    def read_bytes(self) -> bytes:
        return self.data if self.data is not None else self.local_path.read_bytes()  # type: ignore[union-attr]


@dataclass(frozen=True, slots=True)
class ProviderResult:
    artifacts: tuple[Artifact, ...]
    resolved: ResolvedExecution
    provider_job: ProviderJob | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifacts:
            raise ValueError("provider result must contain at least one artifact")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    kind: ProviderEventKind
    progress: float | None = None
    artifact: Artifact | None = None
    message: str | None = None
    result: ProviderResult | None = None
    provider_job: ProviderJob | None = None

    def __post_init__(self) -> None:
        if self.progress is not None and not 0.0 <= self.progress <= 1.0:
            raise ValueError("event progress must be between zero and one")
        if self.kind is ProviderEventKind.ARTIFACT and self.artifact is None:
            raise ValueError("artifact events require an artifact")
        if self.kind is ProviderEventKind.COMPLETED and self.result is None:
            raise ValueError("completed events require a result")


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider_id: str
    generation_kinds: frozenset[GenerationKind]
    supports_references: bool
    supports_streaming: bool
    supports_cancellation: bool
    supports_resumability: bool
    max_references: int | None = None
    max_concurrency: int | None = None
    execution_location: str = "server"
    credential_kinds: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider_id: str
    status: ProviderHealthStatus
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: ProviderErrorCode,
        detail: str,
        *,
        retryable: bool = False,
        provider_job_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.provider_job_id = provider_job_id
