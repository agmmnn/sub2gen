"""OpenAI-compatible generation and async job routes."""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..api import routes as legacy
from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible
from ..core.models import ChatCompletionRequest, Task


router = APIRouter()


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
