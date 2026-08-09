"""Gemini-compatible generation routes."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..api import routes as legacy
from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible
from ..core.models import GeminiGenerateContentRequest


router = APIRouter()


@router.post("/v1beta/models/{model}:generateContent")
@router.post("/models/{model}:generateContent")
async def generate_content(
    model: str,
    request: GeminiGenerateContentRequest,
    raw_request: Request,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Gemini official generateContent endpoint."""
    try:
        if auth_ctx.key_id is None:
            raise HTTPException(status_code=403, detail="Managed API key required for generation")
        base_allowed = legacy._resolve_allowed_token_ids(auth_ctx)
        normalized = await legacy._normalize_gemini_request(
            model,
            request,
            container.generation_handler,
            api_key_id=auth_ctx.key_id,
            allowed_token_ids=base_allowed,
        )
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = legacy._get_request_base_url(raw_request)
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
            payload = await legacy._geminigen_openai_non_stream(
                request,
                normalized,
                api_key_id=auth_ctx.key_id,
                base_url=request_base_url,
                service=container.geminigen_service,
            )
            if "error" in payload:
                return legacy._build_gemini_error_response_from_handler(payload)
            return JSONResponse(
                content=await legacy._build_gemini_success_payload(
                    payload,
                    normalized.model,
                    container.generation_handler,
                    api_key_id=auth_ctx.key_id,
                    allowed_token_ids=set(),
                    project_id=None,
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
        if "error" in payload:
            return legacy._build_gemini_error_response_from_handler(payload)

        return JSONResponse(
            content=await legacy._build_gemini_success_payload(
                payload,
                normalized.model,
                container.generation_handler,
                api_key_id=auth_ctx.key_id,
                allowed_token_ids=allowed_token_ids,
                project_id=selected_project_id,
            )
        )

    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=legacy._build_gemini_error_payload(exc.status_code, str(exc.detail)),
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content=legacy._build_gemini_error_payload(500, str(exc)),
        )


@router.post("/v1beta/models/{model}:streamGenerateContent")
@router.post("/models/{model}:streamGenerateContent")
async def stream_generate_content(
    model: str,
    request: GeminiGenerateContentRequest,
    raw_request: Request,
    alt: Optional[str] = Query(None),
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Gemini official streamGenerateContent endpoint."""
    try:
        if auth_ctx.key_id is None:
            raise HTTPException(status_code=403, detail="Managed API key required for generation")
        base_allowed = legacy._resolve_allowed_token_ids(auth_ctx)
        normalized = await legacy._normalize_gemini_request(
            model,
            request,
            container.generation_handler,
            api_key_id=auth_ctx.key_id,
            allowed_token_ids=base_allowed,
        )
        if not normalized.prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        request_base_url = legacy._get_request_base_url(raw_request)
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

        return StreamingResponse(
            legacy._iterate_gemini_stream(
                normalized,
                normalized.model,
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
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=legacy._build_gemini_error_payload(exc.status_code, str(exc.detail)),
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content=legacy._build_gemini_error_payload(500, str(exc)),
        )
