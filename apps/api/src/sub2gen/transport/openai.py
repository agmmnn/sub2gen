"""OpenAI-compatible generation and async job routes."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import replace
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sub2gen_provider_sdk import ReferenceInput

from ..api import routes as legacy
from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible
from ..core.models import ChatCompletionRequest, Task
from ..persistence import GenerationJobStatus
from ..services.reference_inputs import ReferenceInputError, load_remote_reference


router = APIRouter()


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    prompt: str = Field(min_length=1)
    model: str
    n: int = Field(default=1, ge=1, le=10)
    size: str | None = None
    response_format: Literal["url", "b64_json"] = "url"
    user: str | None = None
    project_id: str | None = None
    reference_images: list[str] = Field(default_factory=list)
    image: str | list[str] | None = None
    async_mode: bool = Field(default=False, alias="async")


async def _reference_inputs_from_uris(
    uris: list[str],
    *,
    container: AppContainer,
    auth: AuthContext,
) -> tuple[ReferenceInput, ...]:
    references: list[ReferenceInput] = []
    allowed = legacy._resolve_allowed_token_ids(auth)
    for index, uri in enumerate(uris):
        if uri.startswith("data:image"):
            media_type, content = legacy._decode_data_url(uri)
        elif legacy._extract_cache_filename(uri):
            content = await legacy._load_image_bytes_from_uri(
                uri,
                container.generation_handler,
                api_key_id=auth.key_id,
                allowed_token_ids=allowed,
            )
            media_type = legacy._detect_image_mime_type(content)
        else:
            try:
                media_type, content = await load_remote_reference(uri)
            except ReferenceInputError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        references.append(ReferenceInput(media_type, data=content, name=f"reference-{index + 1}"))
    return tuple(references)


def _image_uris(request: ImageGenerationRequest) -> list[str]:
    values = list(request.reference_images)
    if isinstance(request.image, str):
        values.append(request.image)
    elif isinstance(request.image, list):
        values.extend(request.image)
    return values


async def _provider_image_payload(
    container: AppContainer,
    job,
    *,
    base_url: str,
    response_format: str = "url",
):
    artifacts = await container.repositories.generation_artifacts.list_for_job(job.id)
    data = []
    for artifact in artifacts:
        if response_format == "b64_json":
            content = await container.generation_handler.file_cache.read_bytes(artifact.filename)
            data.append({"b64_json": base64.b64encode(content).decode("ascii")})
        else:
            data.append({"url": container.generation_handler.file_cache.build_url(artifact.filename, base_url)})
    return {
        "created": int(time.time()),
        "data": data,
        "model": job.requested_model,
        "job_id": job.id,
        "status": job.status.value,
        "resolved_execution": dict(job.resolved_execution or {}),
    }


async def _execute_provider_image(
    image_request: ImageGenerationRequest,
    raw_request: Request,
    auth: AuthContext,
    container: AppContainer,
    idempotency_key: str | None,
):
    if auth.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required for generation")
    if image_request.n != 1:
        raise HTTPException(status_code=400, detail="This provider currently supports n=1")
    references = await _reference_inputs_from_uris(_image_uris(image_request), container=container, auth=auth)
    base_url = legacy._get_request_base_url(raw_request)
    try:
        prepared = await container.unified_images.prepare(
            prompt=image_request.prompt,
            model=image_request.model,
            references=references,
            auth=auth,
            base_url=base_url,
            idempotency_key=idempotency_key,
            provider_options={
                key: value
                for key, value in {"size": image_request.size, "project": image_request.project_id}.items()
                if value is not None
            },
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if prepared.duplicate:
        if prepared.job.status is GenerationJobStatus.SUCCEEDED:
            return await _provider_image_payload(
                container,
                prepared.job,
                base_url=base_url,
                response_format=image_request.response_format,
            )
        if prepared.job.status in {GenerationJobStatus.QUEUED, GenerationJobStatus.OFFERED, GenerationJobStatus.RUNNING}:
            return JSONResponse(status_code=202, content={"job_id": prepared.job.id, "status": prepared.job.status.value})
        raise HTTPException(status_code=409, detail=prepared.job.error_detail or "idempotent job did not succeed")
    if image_request.async_mode or "respond-async" in raw_request.headers.get("prefer", "").lower():
        container.unified_images.start(prepared)
        return JSONResponse(status_code=202, content={"job_id": prepared.job.id, "status": "queued"})
    try:
        await container.unified_images.execute(prepared)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    stored = await container.repositories.generation_jobs.get(prepared.job.id)
    return await _provider_image_payload(
        container,
        stored,
        base_url=base_url,
        response_format=image_request.response_format,
    )


def _is_worker_image_model(model: str, container: AppContainer) -> bool:
    if not hasattr(container, "model_registry"):
        return False
    try:
        return container.model_registry.resolve(model).provider_id in {"chatgpt-web", "chatgpt-codex"}
    except KeyError:
        return False


async def _prepare_chat_image(normalized, raw_request, auth, container):
    references = tuple(
        ReferenceInput(legacy._detect_image_mime_type(content), data=content, name=f"reference-{index + 1}")
        for index, content in enumerate(normalized.images)
    )
    return await container.unified_images.prepare(
        prompt=normalized.prompt,
        model=normalized.model,
        references=references,
        auth=auth,
        base_url=legacy._get_request_base_url(raw_request),
        idempotency_key=raw_request.headers.get("idempotency-key"),
        provider_options={"project": normalized.project_id} if normalized.project_id else {},
    )


async def _chat_image_payload(prepared, container, base_url):
    job = await container.repositories.generation_jobs.get(prepared.job.id)
    image_payload = await _provider_image_payload(container, job, base_url=base_url)
    content = "\n".join(f"![Generated Image]({item['url']})" for item in image_payload["data"])
    return {
        "id": f"chatcmpl-{job.id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": job.requested_model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "resolved_execution": dict(job.resolved_execution or {}),
    }


async def _stream_chat_image(prepared, container, base_url):
    try:
        if not prepared.duplicate:
            await container.unified_images.execute(prepared)
        payload = await _chat_image_payload(prepared, container, base_url)
        chunk = {
            "id": payload["id"],
            "object": "chat.completion.chunk",
            "created": payload["created"],
            "model": payload["model"],
            "choices": [{"index": 0, "delta": payload["choices"][0]["message"], "finish_reason": "stop"}],
            "resolved_execution": payload["resolved_execution"],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
    except Exception as exc:
        yield f"data: {json.dumps({'error': {'message': str(exc)}})}\n\n"
    yield "data: [DONE]\n\n"


@router.post("/v1/images/generations")
async def create_image_generation(
    image_request: ImageGenerationRequest,
    raw_request: Request,
    auth: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    descriptor = container.model_registry.resolve(image_request.model)
    if descriptor.provider_id == "google-flow":
        normalized = legacy.NormalizedGenerationRequest(
            model=descriptor.resolved_model,
            prompt=image_request.prompt,
            images=[item.read_bytes() for item in await _reference_inputs_from_uris(_image_uris(image_request), container=container, auth=auth)],
            project_id=image_request.project_id,
        )
        allowed, project_id = await legacy._select_generation_target(
            auth, normalized.model, normalized.project_id, container.generation_handler
        )
        normalized = replace(normalized, project_id=project_id)
        payload = legacy._enrich_payload_with_direct_url(
            legacy._parse_handler_result(
                await legacy._collect_non_stream_result(
                    normalized,
                    container.generation_handler,
                    legacy._get_request_base_url(raw_request),
                    allowed,
                    legacy._build_selection_context(auth, allowed, project_id),
                    api_key_id=auth.key_id,
                )
            )
        )
        fields = legacy._extract_async_delivery_fields(payload, descriptor.resolved_model)
        urls = fields.get("delivery_urls") or legacy.MARKDOWN_IMAGE_RE.findall(
            legacy._extract_openai_message_content(payload)
        )
        return {"created": int(time.time()), "data": [{"url": url} for url in urls], "model": image_request.model}
    return await _execute_provider_image(image_request, raw_request, auth, container, idempotency_key)


@router.post("/v1/images/edits")
async def create_image_edit(
    raw_request: Request,
    image: Annotated[list[UploadFile], File()],
    prompt: Annotated[str, Form()],
    model: Annotated[str, Form()],
    n: Annotated[int, Form(ge=1, le=10)] = 1,
    response_format: Annotated[Literal["url", "b64_json"], Form()] = "url",
    project_id: Annotated[str | None, Form()] = None,
    async_mode: Annotated[bool, Form(alias="async")] = False,
    auth: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    references = []
    for upload in image:
        content = await upload.read()
        if not content or len(content) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Each edit image must be between 1 byte and 20 MiB")
        media_type = upload.content_type or legacy._detect_image_mime_type(content)
        references.append(f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}")
    request = ImageGenerationRequest.model_validate(
        {
            "prompt": prompt,
            "model": model,
            "n": n,
            "response_format": response_format,
            "project_id": project_id,
            "reference_images": references,
            "async": async_mode,
        }
    )
    return await create_image_generation(request, raw_request, auth, container, idempotency_key)


@router.post("/v1/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    raw_request: Request,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """OpenAI-compatible unified generation endpoint."""
    try:
        if auth_ctx.key_id is None:
            raise HTTPException(status_code=403, detail="Managed API key required for generation")
        base_allowed = legacy._resolve_allowed_token_ids(auth_ctx)
        normalized = await legacy._normalize_openai_request(
            request,
            container.generation_handler,
            api_key_id=auth_ctx.key_id,
            allowed_token_ids=base_allowed,
        )
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = legacy._get_request_base_url(raw_request)
        if _is_worker_image_model(normalized.model, container):
            prepared = await _prepare_chat_image(normalized, raw_request, auth_ctx, container)
            if request.stream:
                return StreamingResponse(
                    _stream_chat_image(prepared, container, request_base_url),
                    media_type="text/event-stream",
                )
            if not prepared.duplicate:
                await container.unified_images.execute(prepared)
            elif prepared.job.status is not GenerationJobStatus.SUCCEEDED:
                raise HTTPException(status_code=409, detail="Idempotent generation is still running or failed")
            return legacy._build_openai_json_response(
                await _chat_image_payload(prepared, container, request_base_url)
            )

        if legacy._is_runway_model(normalized.model):
            if normalized.project_id:
                raise HTTPException(
                    status_code=400,
                    detail="project_id is only supported for native Flow models",
                )
            legacy._require_runway_scope(auth_ctx)
            if request.stream:
                return StreamingResponse(
                    legacy._iterate_runway_openai_stream(
                        request,
                        normalized,
                        api_key_id=auth_ctx.key_id,
                        base_url=request_base_url,
                        service=container.runway_service,
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            return legacy._build_openai_json_response(
                await legacy._runway_openai_non_stream(
                    request,
                    normalized,
                    api_key_id=auth_ctx.key_id,
                    base_url=request_base_url,
                    service=container.runway_service,
                )
            )

        if legacy._is_geminigen_model(normalized.model):
            if normalized.project_id:
                raise HTTPException(
                    status_code=400,
                    detail="project_id is only supported for native Flow models",
                )
            legacy._require_geminigen_scope(auth_ctx)
            await legacy._require_geminigen_model_enabled(
                normalized.model, container.geminigen_service
            )
            if request.stream:
                return StreamingResponse(
                    legacy._iterate_geminigen_openai_stream(
                        request,
                        normalized,
                        api_key_id=auth_ctx.key_id,
                        base_url=request_base_url,
                        service=container.geminigen_service,
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            return legacy._build_openai_json_response(
                await legacy._geminigen_openai_non_stream(
                    request,
                    normalized,
                    api_key_id=auth_ctx.key_id,
                    base_url=request_base_url,
                    service=container.geminigen_service,
                )
            )

        allowed_token_ids, selected_project_id = await legacy._select_generation_target(
            auth_ctx,
            normalized.model,
            normalized.project_id,
            container.generation_handler,
        )
        normalized = replace(normalized, project_id=selected_project_id)
        selection_context = legacy._build_selection_context(
            auth_ctx,
            allowed_token_ids,
            selected_project_id,
        )

        if request.stream:
            return StreamingResponse(
                legacy._iterate_openai_stream(
                    normalized,
                    container.generation_handler,
                    request_base_url,
                    allowed_token_ids,
                    selection_context,
                    api_key_id=auth_ctx.key_id,
                    project_id=selected_project_id,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        payload = legacy._enrich_payload_with_direct_url(
            legacy._parse_handler_result(
                await legacy._collect_non_stream_result(
                    normalized,
                    container.generation_handler,
                    request_base_url,
                    allowed_token_ids,
                    selection_context,
                    api_key_id=auth_ctx.key_id,
                )
            )
        )
        payload = legacy._with_projectid(payload, selected_project_id)
        return legacy._build_openai_json_response(payload)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/v1/async/chat/completions")
async def create_chat_completion_async(
    request: ChatCompletionRequest,
    raw_request: Request,
    background_tasks: BackgroundTasks,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """OpenAI-compatible async generation endpoint with polling support."""
    try:
        if auth_ctx.key_id is None:
            raise HTTPException(status_code=403, detail="Managed API key required for generation")
        base_allowed = legacy._resolve_allowed_token_ids(auth_ctx)
        normalized = await legacy._normalize_openai_request(
            request,
            container.generation_handler,
            api_key_id=auth_ctx.key_id,
            allowed_token_ids=base_allowed,
        )
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = legacy._get_request_base_url(raw_request)
        if _is_worker_image_model(normalized.model, container):
            prepared = await _prepare_chat_image(normalized, raw_request, auth_ctx, container)
            if not prepared.duplicate:
                container.unified_images.start(prepared)
            return JSONResponse(
                status_code=202,
                content={"job_id": prepared.job.id, "status": prepared.job.status.value},
            )

        if legacy._is_runway_model(normalized.model):
            if normalized.project_id:
                raise HTTPException(
                    status_code=400,
                    detail="project_id is only supported for native Flow models",
                )
            legacy._require_runway_scope(auth_ctx)
            task = await legacy._start_runway_from_openai_request(
                request,
                normalized,
                api_key_id=auth_ctx.key_id,
                service=container.runway_service,
            )
            return JSONResponse(
                status_code=202,
                content={
                    "job_id": task.job_id,
                    "status": task.status,
                    "model": task.public_model_id,
                    "upstream_task_id": task.upstream_task_id,
                },
            )

        if legacy._is_geminigen_model(normalized.model):
            if normalized.project_id:
                raise HTTPException(
                    status_code=400,
                    detail="project_id is only supported for native Flow models",
                )
            legacy._require_geminigen_scope(auth_ctx)
            await legacy._require_geminigen_model_enabled(
                normalized.model, container.geminigen_service
            )
            task = await legacy._enqueue_geminigen_from_request(
                request,
                normalized,
                api_key_id=auth_ctx.key_id,
                service=container.geminigen_service,
            )
            geminigen_service = container.geminigen_service
            background_tasks.add_task(
                geminigen_service.start_and_complete_queued_task_in_background,
                task.job_id,
                images=normalized.images,
                options=legacy._geminigen_options_from_request(request),
                api_key_id=auth_ctx.key_id,
                base_url=request_base_url,
            )
            return JSONResponse(
                status_code=202,
                content=geminigen_service.task_to_public_dict(task),
            )

        allowed_token_ids, selected_project_id = await legacy._select_generation_target(
            auth_ctx,
            normalized.model,
            normalized.project_id,
            container.generation_handler,
        )
        normalized = replace(normalized, project_id=selected_project_id)
        selection_context = legacy._build_selection_context(
            auth_ctx,
            allowed_token_ids,
            selected_project_id,
        )

        handler = container.generation_handler
        selected_token_id = min(allowed_token_ids) if allowed_token_ids else 0
        new_task_id = legacy._new_async_job_id()
        await handler.db.create_task(
            Task(
                task_id=new_task_id,
                token_id=selected_token_id,
                api_key_id=auth_ctx.key_id,
                project_id=selected_project_id,
                model=normalized.model,
                prompt=normalized.prompt,
                status="processing",
                progress=0,
                requested_resolution=legacy._infer_requested_resolution(normalized.model),
                upscale_status="pending" if legacy._infer_requested_resolution(normalized.model) else "not_requested",
                job_phase="queued",
                captcha_status="pending",
            )
        )

        background_tasks.add_task(
            legacy._run_async_generation_task,
            task_id=new_task_id,
            normalized=normalized,
            base_url_override=request_base_url,
            allowed_token_ids=allowed_token_ids,
            selection_context=selection_context,
            api_key_id=auth_ctx.key_id,
            handler=handler,
        )
        return JSONResponse(
            status_code=202,
            content={
                "job_id": new_task_id,
                "status": "processing",
                "project_id": selected_project_id,
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/v1/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    raw_request: Request,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Read persisted async generation status without duplicating provider polling."""
    repositories = getattr(container, "repositories", None)
    unified_job = (
        await repositories.generation_jobs.get(job_id)
        if repositories is not None
        else None
    )
    if unified_job is not None:
        if auth_ctx.key_id is None or unified_job.api_key_id != auth_ctx.key_id:
            raise HTTPException(status_code=403, detail="Not authorized to view this job")
        payload = await _provider_image_payload(
            container,
            unified_job,
            base_url=legacy._get_request_base_url(raw_request),
        )
        payload.update(
            {
                "error_code": unified_job.error_code,
                "error_message": unified_job.error_detail,
                "created_at": unified_job.created_at,
                "completed_at": unified_job.terminal_at,
            }
        )
        return payload

    geminigen_service = container.geminigen_service
    runway_service = container.runway_service
    if job_id.startswith("geminigen-"):
        geminigen_task = await geminigen_service.db.get_geminigen_task(job_id)
        if geminigen_task:
            if auth_ctx.key_id is None or geminigen_task.api_key_id != auth_ctx.key_id:
                raise HTTPException(status_code=403, detail="Not authorized to view this job")
            return geminigen_service.task_to_public_dict(geminigen_task)

    if job_id.startswith("runway-"):
        runway_task = await runway_service.db.get_runway_task(job_id)
        if runway_task:
            if auth_ctx.key_id is None or runway_task.api_key_id != auth_ctx.key_id:
                raise HTTPException(status_code=403, detail="Not authorized to view this job")
            runway_task = await runway_service.poll_task(
                job_id,
                api_key_id=auth_ctx.key_id,
                base_url=legacy._get_request_base_url(raw_request),
            )
            return runway_service.task_to_public_dict(runway_task)

    handler = container.generation_handler
    task = await handler.db.get_task(job_id)
    if not task:
        if runway_service is not None:
            runway_task = await runway_service.db.get_runway_task(job_id)
            if runway_task:
                if auth_ctx.key_id is None or runway_task.api_key_id != auth_ctx.key_id:
                    raise HTTPException(status_code=403, detail="Not authorized to view this job")
                runway_task = await runway_service.poll_task(
                    job_id,
                    api_key_id=auth_ctx.key_id,
                    base_url=legacy._get_request_base_url(raw_request),
                )
                return runway_service.task_to_public_dict(runway_task)
        if geminigen_service is not None:
            geminigen_task = await geminigen_service.db.get_geminigen_task(job_id)
            if geminigen_task:
                if auth_ctx.key_id is None or geminigen_task.api_key_id != auth_ctx.key_id:
                    raise HTTPException(status_code=403, detail="Not authorized to view this job")
                return geminigen_service.task_to_public_dict(geminigen_task)
        raise HTTPException(status_code=404, detail="Job not found")

    if auth_ctx.key_id is None or task.api_key_id != auth_ctx.key_id:
        raise HTTPException(status_code=403, detail="Not authorized to view this job")

    return {
        "job_id": task.task_id,
        "status": task.status,
        "progress": task.progress,
        "model": task.model,
        "project_id": task.project_id,
        "result_urls": task.result_urls,
        "base_result_urls": task.base_result_urls,
        "delivery_urls": task.delivery_urls,
        "requested_resolution": task.requested_resolution,
        "output_resolution": task.output_resolution,
        "upscale_status": task.upscale_status,
        "upscale_error_message": task.upscale_error_message,
        "error_message": task.error_message,
        "job_phase": getattr(task, "job_phase", None),
        "captcha_status": getattr(task, "captcha_status", None),
        "captcha_detail": getattr(task, "captcha_detail", None),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


@router.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    job = await container.repositories.generation_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.api_key_id != auth_ctx.key_id:
        raise HTTPException(status_code=403, detail="Not authorized to cancel this job")
    if await container.unified_images.cancel(job_id):
        return {"job_id": job_id, "status": "cancelled"}
    if job.status in {
        GenerationJobStatus.SUCCEEDED,
        GenerationJobStatus.FAILED,
        GenerationJobStatus.CANCELLED,
        GenerationJobStatus.TIMED_OUT,
    }:
        return {"job_id": job_id, "status": job.status.value}
    raise HTTPException(status_code=409, detail="Job is not active in this API process")
