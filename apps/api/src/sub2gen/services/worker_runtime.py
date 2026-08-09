"""Connection hub and request/response bridge for canonical workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sub2gen_worker_protocol import ArtifactGrantStore, WorkerCoordinator
from sub2gen_worker_protocol.codec import encode_envelope, make_envelope
from sub2gen_worker_protocol.coordinator import TerminalWorkerResult
from sub2gen_worker_protocol.generated import (
    JobCancelPayload,
    JobDecisionPayload,
    JobErrorPayload,
    JobOfferPayload,
    JobProgressPayload,
    JobResultPayload,
    MessageType,
    WorkerHeartbeatPayload,
)

SendText = Callable[[str], Awaitable[None]]


class WorkerRuntimeError(RuntimeError):
    pass


class WorkerRuntime:
    def __init__(self, coordinator: WorkerCoordinator, grants: ArtifactGrantStore) -> None:
        self.coordinator = coordinator
        self.grants = grants
        self._connections: dict[str, SendText] = {}
        self._terminal: dict[str, asyncio.Future[TerminalWorkerResult]] = {}

    def is_connected(self, worker_id: str) -> bool:
        return worker_id in self._connections

    def connect(self, worker_id: str, send_text: SendText) -> None:
        self._connections[worker_id] = send_text

    def disconnect(self, worker_id: str) -> None:
        self._connections.pop(worker_id, None)
        expired = self.coordinator.disconnect(worker_id)
        for lease_id in expired:
            future = self._terminal.pop(lease_id, None)
            if future and not future.done():
                future.set_exception(WorkerRuntimeError("worker disconnected"))

    async def dispatch(
        self,
        *,
        worker_id: str,
        job_id: str,
        job_kind: str,
        attempt: int,
        capability: str,
        input: dict[str, Any],
        timeout_seconds: float,
        artifact_content_types: tuple[str, ...],
        artifact_max_bytes: int,
        upload_base_url: str,
        api_key_identity: str | None = None,
    ) -> TerminalWorkerResult:
        sender = self._connections.get(worker_id)
        if sender is None:
            raise WorkerRuntimeError("worker is not connected")
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        lease = self.coordinator.offer(
            worker_id=worker_id,
            job_id=job_id,
            job_kind=job_kind,
            attempt=attempt,
            capability=capability,
            deadline=deadline,
        )
        grant = self.grants.create(
            worker_id=worker_id,
            job_id=job_id,
            max_bytes=artifact_max_bytes,
            content_types=artifact_content_types,
            ttl_seconds=max(1, int(timeout_seconds) + 30),
        )
        offer = make_envelope(
            message_type=MessageType.JOB_OFFER,
            worker_id=worker_id,
            job_id=job_id,
            job_kind=job_kind,
            payload=JobOfferPayload(
                attempt=attempt,
                lease_id=lease.lease_id,
                capability=capability,
                deadline=deadline,
                input=input,
                artifact_upload_grants=(
                    {
                        "grant_id": grant.grant_id,
                        "upload_url": f"{upload_base_url.rstrip('/')}/api/workers/artifacts/{grant.grant_id}",
                        "expires_at": grant.expires_at.isoformat(),
                        "max_bytes": grant.max_bytes,
                        "content_types": grant.content_types,
                    },
                ),
                api_key_identity=api_key_identity,
            ),
        )
        future = asyncio.get_running_loop().create_future()
        self._terminal[lease.lease_id] = future
        await sender(encode_envelope(offer))
        try:
            return await asyncio.wait_for(future, timeout_seconds)
        except TimeoutError:
            await self._cancel(sender, offer.message_id, worker_id, job_id, job_kind, attempt, lease.lease_id, "deadline")
            raise WorkerRuntimeError("worker job timed out")
        except asyncio.CancelledError:
            await self._cancel(sender, offer.message_id, worker_id, job_id, job_kind, attempt, lease.lease_id, "cancelled")
            raise
        finally:
            self._terminal.pop(lease.lease_id, None)

    async def _cancel(self, sender, correlation_id, worker_id, job_id, job_kind, attempt, lease_id, reason):
        self.coordinator.cancel(lease_id)
        cancel = make_envelope(
            message_type=MessageType.JOB_CANCEL,
            worker_id=worker_id,
            job_id=job_id,
            job_kind=job_kind,
            correlation_id=correlation_id,
            payload=JobCancelPayload(attempt=attempt, lease_id=lease_id, reason=reason),
        )
        await sender(encode_envelope(cancel))

    async def handle(self, envelope: Any, payload: Any) -> None:
        worker_id = envelope.worker_id
        if envelope.message_type is MessageType.WORKER_HEARTBEAT and isinstance(payload, WorkerHeartbeatPayload):
            self.coordinator.heartbeat(worker_id, payload)
        elif envelope.message_type is MessageType.JOB_ACCEPT and isinstance(payload, JobDecisionPayload):
            self.coordinator.accept(payload.lease_id, worker_id=worker_id)
        elif envelope.message_type is MessageType.JOB_REJECT and isinstance(payload, JobDecisionPayload):
            self.coordinator.leases.reject(payload.lease_id, worker_id=worker_id)
            self._fail(payload.lease_id, payload.reason or "worker rejected job")
        elif envelope.message_type is MessageType.JOB_PROGRESS and isinstance(payload, JobProgressPayload):
            self.coordinator.progress(worker_id=worker_id, job_id=envelope.job_id or "", payload=payload)
        elif envelope.message_type is MessageType.JOB_RESULT and isinstance(payload, JobResultPayload):
            result = self.coordinator.result(worker_id=worker_id, job_id=envelope.job_id or "", payload=payload)
            self._finish(payload.lease_id, result)
        elif envelope.message_type is MessageType.JOB_ERROR and isinstance(payload, JobErrorPayload):
            result = self.coordinator.error(worker_id=worker_id, job_id=envelope.job_id or "", payload=payload)
            self._finish(payload.lease_id, result)
        else:
            raise WorkerRuntimeError("message type is invalid in registered worker state")

    def _finish(self, lease_id: str, result: TerminalWorkerResult) -> None:
        future = self._terminal.get(lease_id)
        if future and not future.done():
            future.set_result(result)

    def _fail(self, lease_id: str, detail: str) -> None:
        future = self._terminal.get(lease_id)
        if future and not future.done():
            future.set_exception(WorkerRuntimeError(detail))
