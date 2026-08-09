from __future__ import annotations

import hashlib

import pytest
from sub2gen_worker_protocol import ArtifactGrantError, ArtifactGrantStore

from sub2gen.services.worker_artifacts import WorkerArtifactInbox


def test_worker_artifact_inbox_validates_and_hands_off_local_bytes(tmp_path) -> None:
    body = b"image-bytes"
    grants = ArtifactGrantStore()
    inbox = WorkerArtifactInbox(grants, tmp_path)
    grant = grants.create(
        worker_id="worker-1",
        job_id="job-1",
        max_bytes=len(body),
        content_types=("image/png",),
        expected_sha256=hashlib.sha256(body).hexdigest(),
    )

    received = inbox.ingest(
        grant.grant_id,
        worker_id="worker-1",
        job_id="job-1",
        media_type="image/png",
        body=body,
    )
    assert received.path.read_bytes() == body
    assert inbox.take(grant.grant_id) == received
    with pytest.raises(ArtifactGrantError, match="unavailable"):
        inbox.ingest(
            grant.grant_id,
            worker_id="worker-1",
            job_id="job-1",
            media_type="image/png",
            body=body,
        )
