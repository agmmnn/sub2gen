"""Provider-neutral execution and artifact commit boundary."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from sub2gen_provider_sdk import (
    Artifact,
    GenerationProvider,
    GenerationRequest,
    ProviderExecutionContext,
    ProviderResult,
)

from .file_cache import FileCache


@dataclass(frozen=True, slots=True)
class CommittedArtifact:
    filename: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ProviderExecutionOutcome:
    result: ProviderResult
    artifacts: tuple[CommittedArtifact, ...]


class ProviderArtifactCommitter:
    """The only boundary that turns provider artifacts into API-owned files."""

    def __init__(self, file_cache: FileCache) -> None:
        self.file_cache = file_cache

    async def commit(
        self,
        artifact: Artifact,
        *,
        request_id: str,
        index: int,
        api_key_id: int | None = None,
        token_id: int | None = None,
        flow_project_id: str | None = None,
    ) -> CommittedArtifact:
        content = artifact.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if artifact.sha256 is not None and artifact.sha256.lower() != digest:
            raise ValueError("provider artifact checksum does not match its contents")
        suffix = self._suffix(artifact)
        safe_request_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in request_id).strip("-")
        filename = f"{safe_request_id or 'generation'}-{index + 1}-{digest[:12]}{suffix}"
        media_kind = "video" if artifact.media_type.startswith("video/") else "image"
        await self.file_cache.store_bytes(
            filename,
            content,
            artifact.media_type,
            api_key_id=api_key_id,
            token_id=token_id,
            flow_project_id=flow_project_id,
            media_type=media_kind,
        )
        return CommittedArtifact(filename, artifact.media_type, len(content), digest)

    @staticmethod
    def _suffix(artifact: Artifact) -> str:
        supplied = Path(artifact.filename or "").suffix.lower()
        if supplied and len(supplied) <= 10 and supplied[1:].isalnum():
            return supplied
        return mimetypes.guess_extension(artifact.media_type, strict=False) or ".bin"


class ProviderExecutionService:
    """Execute an already-resolved provider; routing stays outside this service."""

    def __init__(self, artifacts: ProviderArtifactCommitter) -> None:
        self.artifacts = artifacts

    async def execute(
        self,
        provider: GenerationProvider,
        request: GenerationRequest,
        context: ProviderExecutionContext,
        *,
        api_key_id: int | None = None,
        token_id: int | None = None,
        flow_project_id: str | None = None,
    ) -> ProviderExecutionOutcome:
        result = await provider.generate(request, context)
        committed = tuple(
            [
                await self.artifacts.commit(
                    artifact,
                    request_id=request.request_id,
                    index=index,
                    api_key_id=api_key_id,
                    token_id=token_id,
                    flow_project_id=flow_project_id,
                )
                for index, artifact in enumerate(result.artifacts)
            ]
        )
        return ProviderExecutionOutcome(result=result, artifacts=committed)
