from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sub2gen_worker_protocol.artifacts import ArtifactGrantError, ArtifactGrantStore
from sub2gen_worker_protocol.codec import ProtocolCodecError, decode_envelope, encode_envelope
from sub2gen_worker_protocol.coordinator import WorkerCoordinator, WorkerProtocolError
from sub2gen_worker_protocol.generated import (
    JobProgressPayload,
    JobResultPayload,
    MessageType,
    WorkerHeartbeatPayload,
    WorkerPolicy,
)
from sub2gen_worker_protocol.leases import LeaseError, LeaseRegistry, LeaseState
from sub2gen_worker_protocol.negotiation import ProtocolNegotiationError, negotiate_protocol
from sub2gen_worker_protocol.security import DeviceAuthError, PairingAuthority, authorize_job

TRANSCRIPTS = json.loads(
    (Path(__file__).parents[1] / "schema" / "golden-transcripts.json").read_text(encoding="utf-8")
)


def test_python_codec_round_trips_shared_golden_transcripts() -> None:
    for transcript in TRANSCRIPTS.values():
        envelope, _payload = decode_envelope(json.dumps(transcript))
        assert json.loads(encode_envelope(envelope)) == transcript

    invalid = {**TRANSCRIPTS["offer"], "job_id": None}
    with pytest.raises(ProtocolCodecError):
        decode_envelope(json.dumps(invalid))


def test_protocol_negotiation_never_sends_v1_to_unversioned_clients() -> None:
    with pytest.raises(ProtocolNegotiationError):
        negotiate_protocol(None)
    negotiated = negotiate_protocol(["0.9", "1.0"])
    assert negotiated.version == "1.0"
    with pytest.raises(ProtocolNegotiationError):
        negotiate_protocol(["2.0"])


def test_device_pairing_challenge_replay_revocation_and_policy() -> None:
    authority = PairingAuthority()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    code = authority.create_pairing_code()
    identity = authority.pair(code=code, worker_id="worker-1", public_key=public_key)
    assert identity.worker_id == "worker-1"
    assert public_key.hex() not in repr(identity)
    with pytest.raises(DeviceAuthError):
        authority.pair(code=code, worker_id="worker-2", public_key=public_key)

    challenge_id, _nonce, _expires = authority.issue_challenge("worker-1")
    signature = base64.b64encode(private_key.sign(authority.challenge_bytes(challenge_id))).decode()
    authority.authenticate(worker_id="worker-1", challenge_id=challenge_id, signature=signature)
    with pytest.raises(DeviceAuthError, match="challenge"):
        authority.authenticate(worker_id="worker-1", challenge_id=challenge_id, signature=signature)

    policy = WorkerPolicy(
        allowed_servers=("server-1",),
        allowed_capabilities=("image.generate:chatgpt-web",),
        allowed_models=("chatgpt/gpt-image-web",),
        allowed_accounts=("account-1",),
        max_concurrency=1,
        daily_job_limit=10,
    )
    authorize_job(
        policy,
        server_id="server-1",
        capability="image.generate:chatgpt-web",
        model="chatgpt/gpt-image-web",
        account_id="account-1",
        active_jobs=0,
        jobs_today=0,
    )
    with pytest.raises(DeviceAuthError, match="capability"):
        authorize_job(
            policy,
            server_id="server-1",
            capability="shell.execute",
            model="chatgpt/gpt-image-web",
            account_id="account-1",
            active_jobs=0,
            jobs_today=0,
        )

    authority.revoke("worker-1")
    with pytest.raises(DeviceAuthError, match="credential"):
        authority.issue_challenge("worker-1")


def test_leases_deduplicate_reject_stale_results_and_expire_on_disconnect() -> None:
    registry = LeaseRegistry()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=1)
    first = registry.offer(
        job_id="job-1",
        job_kind="image.generate",
        attempt=1,
        worker_id="worker-1",
        capability="image.generate:chatgpt-web",
        deadline=deadline,
    )
    replay = registry.offer(
        job_id="job-1",
        job_kind="image.generate",
        attempt=1,
        worker_id="worker-1",
        capability="image.generate:chatgpt-web",
        deadline=deadline,
    )
    assert replay.lease_id == first.lease_id
    with pytest.raises(LeaseError, match="capability"):
        registry.accept(first.lease_id, worker_id="worker-1", capabilities=set())

    second = registry.offer(
        job_id="job-1",
        job_kind="image.generate",
        attempt=2,
        worker_id="worker-1",
        capability="image.generate:chatgpt-web",
        deadline=deadline,
    )
    registry.accept(second.lease_id, worker_id="worker-1", capabilities={"image.generate:chatgpt-web"})
    with pytest.raises(LeaseError, match="stale"):
        registry.assert_active(second.lease_id, worker_id="worker-1", job_id="job-1", attempt=1)
    assert registry.disconnect("worker-1") == (second.lease_id,)
    assert second.state is LeaseState.EXPIRED


def test_artifact_grants_enforce_ownership_type_size_digest_and_single_use() -> None:
    body = b"generated-image"
    store = ArtifactGrantStore()
    grant = store.create(
        worker_id="worker-1",
        job_id="job-1",
        max_bytes=len(body),
        content_types=("image/png",),
        expected_sha256=hashlib.sha256(body).hexdigest(),
    )
    with pytest.raises(ArtifactGrantError, match="ownership"):
        store.consume(
            grant.grant_id,
            worker_id="worker-2",
            job_id="job-1",
            content_type="image/png",
            body=body,
        )
    store.consume(
        grant.grant_id,
        worker_id="worker-1",
        job_id="job-1",
        content_type="image/png",
        body=body,
    )
    with pytest.raises(ArtifactGrantError, match="unavailable"):
        store.consume(
            grant.grant_id,
            worker_id="worker-1",
            job_id="job-1",
            content_type="image/png",
            body=body,
        )


def test_coordinator_covers_registration_heartbeat_progress_result_and_disconnect() -> None:
    coordinator = WorkerCoordinator()
    with pytest.raises(WorkerProtocolError, match="unapproved"):
        coordinator.register(
            worker_id="worker-1",
            worker_session_id="session-1",
            capabilities=("shell.execute",),
            approved_capabilities={"image.generate:chatgpt-web"},
            available_slots=1,
        )
    coordinator.register(
        worker_id="worker-1",
        worker_session_id="session-1",
        capabilities=("image.generate:chatgpt-web",),
        approved_capabilities={"image.generate:chatgpt-web"},
        available_slots=1,
    )
    lease = coordinator.offer(
        worker_id="worker-1",
        job_id="job-coordinator",
        job_kind="image.generate",
        attempt=1,
        capability="image.generate:chatgpt-web",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    coordinator.accept(lease.lease_id, worker_id="worker-1")
    coordinator.heartbeat(
        "worker-1",
        WorkerHeartbeatPayload(
            worker_session_id="session-1",
            active_leases=(lease.lease_id,),
            available_slots=0,
        ),
    )
    coordinator.progress(
        worker_id="worker-1",
        job_id="job-coordinator",
        payload=JobProgressPayload(attempt=1, lease_id=lease.lease_id, progress=0.5),
    )
    result = coordinator.result(
        worker_id="worker-1",
        job_id="job-coordinator",
        payload=JobResultPayload(attempt=1, lease_id=lease.lease_id, output={"ok": True}),
    )
    assert result.output == {"ok": True}
    assert coordinator.disconnect("worker-1") == ()
