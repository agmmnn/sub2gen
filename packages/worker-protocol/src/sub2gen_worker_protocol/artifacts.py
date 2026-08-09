"""Job-scoped, single-use artifact upload grants."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4


class ArtifactGrantError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactUploadGrant:
    grant_id: str
    worker_id: str
    job_id: str
    expires_at: datetime
    max_bytes: int
    content_types: tuple[str, ...]
    expected_sha256: str | None = None
    single_use: bool = True


@dataclass(slots=True)
class _GrantState:
    grant: ArtifactUploadGrant
    consumed: bool = False


class ArtifactGrantStore:
    def __init__(self) -> None:
        self._grants: dict[str, _GrantState] = {}

    def create(
        self,
        *,
        worker_id: str,
        job_id: str,
        max_bytes: int,
        content_types: tuple[str, ...],
        ttl_seconds: int = 300,
        expected_sha256: str | None = None,
    ) -> ArtifactUploadGrant:
        normalized_types = tuple(sorted(set(content_types)))
        if not worker_id or not job_id or max_bytes < 1 or ttl_seconds < 1 or not normalized_types:
            raise ValueError("invalid artifact upload grant")
        if expected_sha256 is not None and (
            len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256.lower())
        ):
            raise ValueError("expected_sha256 must be a SHA-256 hex digest")
        grant = ArtifactUploadGrant(
            grant_id=f"upload_{uuid4().hex}",
            worker_id=worker_id,
            job_id=job_id,
            expires_at=_now() + timedelta(seconds=ttl_seconds),
            max_bytes=max_bytes,
            content_types=normalized_types,
            expected_sha256=expected_sha256.lower() if expected_sha256 else None,
        )
        self._grants[grant.grant_id] = _GrantState(grant)
        return grant

    def consume(
        self,
        grant_id: str,
        *,
        worker_id: str,
        job_id: str,
        content_type: str,
        body: bytes,
    ) -> ArtifactUploadGrant:
        state = self._grants.get(grant_id)
        if state is None or state.consumed:
            raise ArtifactGrantError("artifact grant is unavailable")
        grant = state.grant
        if grant.expires_at <= _now():
            del self._grants[grant_id]
            raise ArtifactGrantError("artifact grant expired")
        if grant.worker_id != worker_id or grant.job_id != job_id:
            raise ArtifactGrantError("artifact grant ownership mismatch")
        if content_type not in grant.content_types:
            raise ArtifactGrantError("artifact content type is not allowed")
        if len(body) > grant.max_bytes:
            raise ArtifactGrantError("artifact exceeds the grant size limit")
        digest = hashlib.sha256(body).hexdigest()
        if grant.expected_sha256 and digest != grant.expected_sha256:
            raise ArtifactGrantError("artifact digest mismatch")
        state.consumed = True
        return grant

    def cleanup(self) -> int:
        expired = [key for key, state in self._grants.items() if state.grant.expires_at <= _now()]
        for key in expired:
            del self._grants[key]
        return len(expired)


def _now() -> datetime:
    return datetime.now(timezone.utc)
