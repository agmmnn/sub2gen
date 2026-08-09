from __future__ import annotations

import asyncio

import pytest
from sub2gen_worker_protocol import ArtifactGrantStore, WorkerCoordinator
from sub2gen_worker_protocol.codec import decode_envelope, make_envelope
from sub2gen_worker_protocol.generated import JobDecisionPayload, JobResultPayload, MessageType

from sub2gen.services.worker_runtime import WorkerRuntime


@pytest.mark.asyncio
async def test_worker_runtime_dispatches_offer_and_correlates_terminal_result() -> None:
    coordinator = WorkerCoordinator()
    coordinator.register(
        worker_id="worker-1",
        worker_session_id="session-1",
        capabilities=("image.generate:chatgpt-web",),
        approved_capabilities={"image.generate:chatgpt-web"},
        available_slots=1,
    )
    runtime = WorkerRuntime(coordinator, ArtifactGrantStore())
    sent: asyncio.Queue[str] = asyncio.Queue()
    runtime.connect("worker-1", sent.put)

    dispatch = asyncio.create_task(
        runtime.dispatch(
            worker_id="worker-1",
            job_id="job-1",
            job_kind="image.generate",
            attempt=1,
            capability="image.generate:chatgpt-web",
            input={"prompt": "draw", "model": "chatgpt/gpt-image-web"},
            timeout_seconds=2,
            artifact_content_types=("image/png",),
            artifact_max_bytes=1024,
            upload_base_url="http://127.0.0.1:8000",
        )
    )
    offer, payload = decode_envelope(await sent.get())
    await runtime.handle(
        make_envelope(
            message_type=MessageType.JOB_ACCEPT,
            worker_id="worker-1",
            job_id="job-1",
            job_kind="image.generate",
            payload=JobDecisionPayload(attempt=1, lease_id=payload.lease_id),
        ),
        JobDecisionPayload(attempt=1, lease_id=payload.lease_id),
    )
    result_payload = JobResultPayload(
        attempt=1,
        lease_id=payload.lease_id,
        output={"artifacts": [{"grant_id": payload.artifact_upload_grants[0]["grant_id"]}]},
    )
    await runtime.handle(
        make_envelope(
            message_type=MessageType.JOB_RESULT,
            worker_id="worker-1",
            job_id="job-1",
            job_kind="image.generate",
            correlation_id=offer.message_id,
            payload=result_payload,
        ),
        result_payload,
    )

    terminal = await dispatch
    assert terminal.output == result_payload.output
