"""Protocol lifecycle coordinator independent of FastAPI and WebSocket libraries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .generated import (
    JobErrorPayload,
    JobProgressPayload,
    JobResultPayload,
    StructuredError,
    WorkerHeartbeatPayload,
)
from .leases import JobLease, LeaseError, LeaseRegistry


class WorkerProtocolError(RuntimeError):
    pass


@dataclass(slots=True)
class WorkerSession:
    worker_id: str
    worker_session_id: str
    capabilities: frozenset[str]
    available_slots: int
    last_heartbeat_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class TerminalWorkerResult:
    job_id: str
    attempt: int
    lease_id: str
    output: dict[str, Any] | None = None
    error: StructuredError | None = None


class WorkerCoordinator:
    def __init__(self) -> None:
        self.leases = LeaseRegistry()
        self._sessions: dict[str, WorkerSession] = {}

    def register(
        self,
        *,
        worker_id: str,
        worker_session_id: str,
        capabilities: tuple[str, ...],
        approved_capabilities: set[str],
        available_slots: int,
    ) -> WorkerSession:
        advertised = frozenset(capabilities)
        if not advertised or not advertised <= approved_capabilities:
            raise WorkerProtocolError("worker advertised an unapproved capability")
        if available_slots < 0:
            raise WorkerProtocolError("available_slots must not be negative")
        previous = self._sessions.get(worker_id)
        if previous is not None and previous.worker_session_id != worker_session_id:
            self.disconnect(worker_id)
        session = WorkerSession(worker_id, worker_session_id, advertised, available_slots)
        self._sessions[worker_id] = session
        return session

    def heartbeat(self, worker_id: str, payload: WorkerHeartbeatPayload) -> WorkerSession:
        session = self._session(worker_id)
        if payload.worker_session_id != session.worker_session_id:
            raise WorkerProtocolError("stale worker session")
        for lease_id in payload.active_leases:
            lease = self.leases.get(lease_id)
            if lease is None:
                raise WorkerProtocolError("heartbeat contains an invalid lease")
            try:
                self.leases.assert_active(
                    lease_id,
                    worker_id=worker_id,
                    job_id=lease.job_id,
                    attempt=lease.attempt,
                )
            except LeaseError as exc:
                raise WorkerProtocolError("heartbeat contains an invalid lease") from exc
        session.available_slots = payload.available_slots
        session.last_heartbeat_at = datetime.now(timezone.utc)
        return session

    def offer(
        self,
        *,
        worker_id: str,
        job_id: str,
        job_kind: str,
        attempt: int,
        capability: str,
        deadline: datetime,
    ) -> JobLease:
        session = self._session(worker_id)
        if capability not in session.capabilities:
            raise WorkerProtocolError("worker lacks required capability")
        if session.available_slots < 1:
            raise WorkerProtocolError("worker has no available slots")
        return self.leases.offer(
            job_id=job_id,
            job_kind=job_kind,
            attempt=attempt,
            worker_id=worker_id,
            capability=capability,
            deadline=deadline,
        )

    def accept(self, lease_id: str, *, worker_id: str) -> JobLease:
        session = self._session(worker_id)
        lease = self.leases.accept(lease_id, worker_id=worker_id, capabilities=set(session.capabilities))
        session.available_slots = max(0, session.available_slots - 1)
        return lease

    def progress(self, *, worker_id: str, job_id: str, payload: JobProgressPayload) -> None:
        self.leases.assert_active(
            payload.lease_id,
            worker_id=worker_id,
            job_id=job_id,
            attempt=payload.attempt,
        )

    def result(self, *, worker_id: str, job_id: str, payload: JobResultPayload) -> TerminalWorkerResult:
        self.leases.complete(
            payload.lease_id,
            worker_id=worker_id,
            job_id=job_id,
            attempt=payload.attempt,
        )
        self._session(worker_id).available_slots += 1
        return TerminalWorkerResult(job_id, payload.attempt, payload.lease_id, output=payload.output)

    def error(self, *, worker_id: str, job_id: str, payload: JobErrorPayload) -> TerminalWorkerResult:
        self.leases.complete(
            payload.lease_id,
            worker_id=worker_id,
            job_id=job_id,
            attempt=payload.attempt,
        )
        self._session(worker_id).available_slots += 1
        return TerminalWorkerResult(job_id, payload.attempt, payload.lease_id, error=payload.error)

    def cancel(self, lease_id: str) -> None:
        self.leases.cancel(lease_id)

    def disconnect(self, worker_id: str) -> tuple[str, ...]:
        self._sessions.pop(worker_id, None)
        return self.leases.disconnect(worker_id)

    def _session(self, worker_id: str) -> WorkerSession:
        session = self._sessions.get(worker_id)
        if session is None:
            raise WorkerProtocolError("worker is not registered")
        return session
