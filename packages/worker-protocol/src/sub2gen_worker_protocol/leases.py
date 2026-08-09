"""Ephemeral at-least-once offer and lease lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4


class LeaseError(RuntimeError):
    pass


class LeaseState(StrEnum):
    OFFERED = "offered"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass(slots=True)
class JobLease:
    job_id: str
    job_kind: str
    attempt: int
    worker_id: str
    capability: str
    deadline: datetime
    lease_id: str
    state: LeaseState = LeaseState.OFFERED


class LeaseRegistry:
    """Connection-local lease state; durable attempt audit lives in the API repository."""

    def __init__(self) -> None:
        self._leases: dict[str, JobLease] = {}
        self._attempts: dict[tuple[str, int], str] = {}

    def offer(
        self,
        *,
        job_id: str,
        job_kind: str,
        attempt: int,
        worker_id: str,
        capability: str,
        deadline: datetime,
    ) -> JobLease:
        if attempt < 1 or deadline <= _now():
            raise LeaseError("invalid or expired job offer")
        existing_id = self._attempts.get((job_id, attempt))
        if existing_id:
            return self._leases[existing_id]
        for lease in self._leases.values():
            if lease.job_id == job_id and lease.state in {LeaseState.OFFERED, LeaseState.ACTIVE}:
                lease.state = LeaseState.EXPIRED
        lease = JobLease(
            job_id=job_id,
            job_kind=job_kind,
            attempt=attempt,
            worker_id=worker_id,
            capability=capability,
            deadline=deadline,
            lease_id=f"lease_{uuid4().hex}",
        )
        self._leases[lease.lease_id] = lease
        self._attempts[(job_id, attempt)] = lease.lease_id
        return lease

    def accept(self, lease_id: str, *, worker_id: str, capabilities: set[str]) -> JobLease:
        lease = self._owned(lease_id, worker_id)
        if lease.state is not LeaseState.OFFERED or lease.deadline <= _now():
            lease.state = LeaseState.EXPIRED
            raise LeaseError("job offer is no longer active")
        if lease.capability not in capabilities:
            lease.state = LeaseState.REJECTED
            raise LeaseError("worker did not advertise the required capability")
        lease.state = LeaseState.ACTIVE
        return lease

    def reject(self, lease_id: str, *, worker_id: str) -> None:
        lease = self._owned(lease_id, worker_id)
        if lease.state is not LeaseState.OFFERED:
            raise LeaseError("only offered leases can be rejected")
        lease.state = LeaseState.REJECTED

    def assert_active(self, lease_id: str, *, worker_id: str, job_id: str, attempt: int) -> JobLease:
        lease = self._owned(lease_id, worker_id)
        if lease.job_id != job_id or lease.attempt != attempt or lease.state is not LeaseState.ACTIVE:
            raise LeaseError("stale or inactive lease")
        if lease.deadline <= _now():
            lease.state = LeaseState.EXPIRED
            raise LeaseError("lease deadline expired")
        return lease

    def complete(self, lease_id: str, *, worker_id: str, job_id: str, attempt: int) -> None:
        lease = self.assert_active(lease_id, worker_id=worker_id, job_id=job_id, attempt=attempt)
        lease.state = LeaseState.COMPLETED

    def cancel(self, lease_id: str) -> None:
        lease = self._leases.get(lease_id)
        if lease is None or lease.state not in {LeaseState.OFFERED, LeaseState.ACTIVE}:
            raise LeaseError("lease cannot be cancelled")
        lease.state = LeaseState.CANCELLED

    def disconnect(self, worker_id: str) -> tuple[str, ...]:
        expired: list[str] = []
        for lease in self._leases.values():
            if lease.worker_id == worker_id and lease.state in {LeaseState.OFFERED, LeaseState.ACTIVE}:
                lease.state = LeaseState.EXPIRED
                expired.append(lease.lease_id)
        return tuple(expired)

    def get(self, lease_id: str) -> JobLease | None:
        return self._leases.get(lease_id)

    def _owned(self, lease_id: str, worker_id: str) -> JobLease:
        lease = self._leases.get(lease_id)
        if lease is None or lease.worker_id != worker_id:
            raise LeaseError("lease is not owned by this worker")
        return lease


def _now() -> datetime:
    return datetime.now(timezone.utc)
