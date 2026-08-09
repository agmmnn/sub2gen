"""Unified provider-backed image execution used by public transports."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sub2gen_provider_chatgpt import ChatGPTWebProvider
from sub2gen_provider_sdk import (
    Artifact,
    CancellationToken,
    GenerationKind,
    GenerationRequest,
    ProviderError,
    ProviderErrorCode,
    ProviderExecutionContext,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderResult,
    ReferenceInput,
    ResolvedExecution,
)

from ..core.api_key_manager import AuthContext
from ..core.config import config
from ..generation.signals import RouteHealth, RouteSignal
from ..persistence import GenerationArtifactRecord, GenerationJobRecord, GenerationJobStatus
from .generation_routing import authenticated_caller_from_api_key, trusted_routing_config_from_app
from .provider_execution import ProviderExecutionOutcome
from .worker_runtime import WorkerRuntime, WorkerRuntimeError


@dataclass(frozen=True, slots=True)
class PreparedImageGeneration:
    job: GenerationJobRecord
    request: GenerationRequest | None
    resolved: ResolvedExecution | None
    base_url: str
    duplicate: bool = False


class WorkerChatGPTBackend:
    def __init__(self, runtime: WorkerRuntime, inbox, *, base_url: str) -> None:
        self.runtime = runtime
        self.inbox = inbox
        self.base_url = base_url

    async def health(self) -> ProviderHealth:
        return ProviderHealth("chatgpt-web", ProviderHealthStatus.READY)

    async def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> ProviderResult:
        if not context.resolved.worker_id:
            raise ProviderError(ProviderErrorCode.UNAVAILABLE, "ChatGPT execution has no worker")
        try:
            terminal = await self.runtime.dispatch(
                worker_id=context.resolved.worker_id,
                job_id=request.request_id,
                job_kind="image.generate",
                attempt=context.attempt,
                capability="image.generate:chatgpt-web",
                input={
                    "prompt": request.prompt,
                    "model": request.model,
                    "references": [
                        {
                            "media_type": reference.media_type,
                            "data_base64": base64.b64encode(reference.read_bytes()).decode("ascii"),
                        }
                        for reference in request.references
                    ],
                    "provider_options": dict(request.provider_options),
                },
                timeout_seconds=context.timeout_seconds,
                artifact_content_types=("image/png", "image/jpeg", "image/webp"),
                artifact_max_bytes=25 * 1024 * 1024,
                upload_base_url=self.base_url,
            )
        except asyncio.CancelledError:
            raise
        except WorkerRuntimeError as exc:
            code = ProviderErrorCode.TIMEOUT if "timed out" in str(exc).lower() else ProviderErrorCode.UNAVAILABLE
            raise ProviderError(code, str(exc), retryable=code is ProviderErrorCode.UNAVAILABLE) from exc
        if terminal.error is not None:
            raise ProviderError(
                ProviderErrorCode.TRANSIENT if terminal.error.retryable else ProviderErrorCode.INVALID_OUTPUT,
                terminal.error.message,
                retryable=terminal.error.retryable,
            )
        output = terminal.output or {}
        artifact_rows = output.get("artifacts")
        if not isinstance(artifact_rows, list) or not artifact_rows:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "worker returned no artifacts")
        artifacts: list[Artifact] = []
        for row in artifact_rows:
            grant_id = str(row.get("grant_id") or "") if isinstance(row, dict) else ""
            received = self.inbox.take(grant_id)
            if received is None:
                raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "worker artifact upload is missing")
            artifacts.append(
                Artifact(
                    media_type=received.media_type,
                    local_path=received.path,
                    sha256=str(row.get("sha256") or "") or None,
                )
            )
        return ProviderResult(tuple(artifacts), context.resolved)

    async def cancel(self, provider_job_id: str) -> None:
        del provider_job_id


class UnifiedImageService:
    def __init__(self, container) -> None:
        self.container = container
        self._tasks: dict[str, asyncio.Task[ProviderExecutionOutcome]] = {}

    async def prepare(
        self,
        *,
        prompt: str,
        model: str,
        references: tuple[ReferenceInput, ...],
        auth: AuthContext,
        base_url: str,
        idempotency_key: str | None,
        provider_options: dict[str, Any] | None = None,
    ) -> PreparedImageGeneration:
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "prompt": prompt,
                    "model": model,
                    "references": [hashlib.sha256(item.read_bytes()).hexdigest() for item in references],
                    "provider_options": provider_options or {},
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if idempotency_key:
            existing = await self.container.repositories.generation_jobs.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                existing_fingerprint = str((existing.resolved_execution or {}).get("request_fingerprint") or "")
                if (
                    existing.api_key_id != auth.key_id
                    or existing.requested_model != model
                    or existing_fingerprint != request_fingerprint
                ):
                    raise ValueError("idempotency key is already used by a different request")
                return PreparedImageGeneration(existing, None, None, base_url, duplicate=True)
        descriptor = self.container.model_registry.resolve(model)
        if descriptor.kind is not GenerationKind.IMAGE:
            raise ValueError("requested model does not generate images")
        caller = await authenticated_caller_from_api_key(auth, self.container.repositories)
        request_id = f"image-{uuid4().hex}"
        resolved = await self.container.persistent_generation_router.resolve(
            requested_model=model,
            request_id=request_id,
            config=trusted_routing_config_from_app(),
            caller=caller,
        )
        request = GenerationRequest(
            request_id=request_id,
            prompt=prompt,
            model=descriptor.model_id,
            kind=GenerationKind.IMAGE,
            references=references,
            provider_options=provider_options or {},
        )
        job = await self.container.generation_audit.queue(
            request_id=request_id,
            job_kind="image.generate",
            requested_model=model,
            api_key_id=auth.key_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        return PreparedImageGeneration(job, request, resolved, base_url)

    async def execute(self, prepared: PreparedImageGeneration) -> ProviderExecutionOutcome:
        if prepared.request is None or prepared.resolved is None:
            raise RuntimeError("an existing job cannot be executed again")
        if prepared.resolved.provider_id != "chatgpt-web":
            raise ValueError("provider is executed by the existing Flow pipeline")
        backend = WorkerChatGPTBackend(
            self.container.worker_runtime,
            self.container.worker_artifact_inbox,
            base_url=prepared.base_url,
        )
        provider = ChatGPTWebProvider(backend)
        context = ProviderExecutionContext(
            prepared.resolved,
            CancellationToken(),
            timeout_seconds=float(config.image_timeout),
        )

        async def operation() -> ProviderExecutionOutcome:
            outcome = await self.container.generation_handler.execute_provider(
                provider,
                prepared.request,
                context,
                api_key_id=prepared.job.api_key_id,
            )
            await self.container.repositories.generation_artifacts.replace_for_job(
                prepared.job.id,
                tuple(
                    GenerationArtifactRecord(
                        job_id=prepared.job.id,
                        position=index,
                        filename=artifact.filename,
                        media_type=artifact.media_type,
                        size_bytes=artifact.size_bytes,
                        sha256=artifact.sha256,
                    )
                    for index, artifact in enumerate(outcome.artifacts)
                ),
            )
            for artifact in outcome.result.artifacts:
                if artifact.local_path is not None:
                    Path(artifact.local_path).unlink(missing_ok=True)
            return outcome

        try:
            outcome = await self.container.generation_audit.run(
                job=prepared.job,
                resolved=prepared.resolved,
                operation=operation,
            )
        except ProviderError as exc:
            if exc.code in {ProviderErrorCode.QUOTA, ProviderErrorCode.UNAVAILABLE}:
                self.container.routing_signals.update(
                    provider_id=prepared.resolved.provider_id,
                    provider_account_id=prepared.resolved.provider_account_id,
                    worker_id=prepared.resolved.worker_id,
                    signal=RouteSignal(
                        health=(
                            RouteHealth.UNAVAILABLE
                            if exc.code is ProviderErrorCode.UNAVAILABLE
                            else RouteHealth.READY
                        ),
                        quota_remaining=0 if exc.code is ProviderErrorCode.QUOTA else None,
                    ),
                )
            raise
        self.container.routing_signals.update(
            provider_id=prepared.resolved.provider_id,
            provider_account_id=prepared.resolved.provider_account_id,
            worker_id=prepared.resolved.worker_id,
            signal=RouteSignal(health=RouteHealth.READY),
        )
        return outcome

    def start(self, prepared: PreparedImageGeneration) -> None:
        task = asyncio.create_task(self.execute(prepared))
        self._tasks[prepared.job.id] = task

        def done(completed: asyncio.Task) -> None:
            self._tasks.pop(prepared.job.id, None)
            try:
                completed.exception()
            except asyncio.CancelledError:
                pass

        task.add_done_callback(done)

    async def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def get_job(self, job_id: str, api_key_id: int):
        job = await self.container.repositories.generation_jobs.get(job_id)
        if job is None or job.api_key_id != api_key_id:
            return None
        artifacts = await self.container.repositories.generation_artifacts.list_for_job(job_id)
        return job, artifacts
