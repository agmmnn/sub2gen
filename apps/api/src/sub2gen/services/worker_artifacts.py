"""Validated worker artifact inbox before FileCache commit."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sub2gen_worker_protocol import ArtifactGrantStore, ArtifactUploadGrant


@dataclass(frozen=True, slots=True)
class ReceivedWorkerArtifact:
    grant: ArtifactUploadGrant
    media_type: str
    path: Path


class WorkerArtifactInbox:
    def __init__(self, grants: ArtifactGrantStore, root: Path) -> None:
        self.grants = grants
        self.root = root
        self._received: dict[str, ReceivedWorkerArtifact] = {}

    def ingest(
        self,
        grant_id: str,
        *,
        worker_id: str,
        job_id: str,
        media_type: str,
        body: bytes,
    ) -> ReceivedWorkerArtifact:
        grant = self.grants.consume(
            grant_id,
            worker_id=worker_id,
            job_id=job_id,
            content_type=media_type,
            body=body,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{grant.grant_id}.artifact"
        partial = path.with_suffix(".part")
        partial.write_bytes(body)
        os.replace(partial, path)
        artifact = ReceivedWorkerArtifact(grant, media_type, path)
        self._received[grant_id] = artifact
        return artifact

    def take(self, grant_id: str) -> ReceivedWorkerArtifact | None:
        return self._received.pop(grant_id, None)

    def discard(self, grant_id: str) -> None:
        artifact = self._received.pop(grant_id, None)
        if artifact is not None:
            artifact.path.unlink(missing_ok=True)
