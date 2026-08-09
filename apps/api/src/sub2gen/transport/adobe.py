"""Adobe metadata, cloning, and task-tracker HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..api import routes as legacy
from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible
from ..core.logger import debug_logger
from ..core.models import (
    GenerateCloningPromptsRequest,
    GenerateCloningVideoPromptRequest,
    GenerateMetadataRequest,
    TaskTrackerContributorFetchRequest,
    TaskTrackerKeywordSearchRequest,
)


router = APIRouter()


@router.post("/api/generate-cloning-prompts")
async def generate_cloning_prompts(
    request: GenerateCloningPromptsRequest,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Generate cloning image prompts for one or more images."""
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    legacy._require_managed_scope(auth_ctx, "adobe:cloning")

    request_payload = request.model_dump()

    async def _run():
        image_items = []
        for item in request.images:
            image_items.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "image_url": item.image_url,
                    "image_base64": item.image_base64,
                    "mimeType": item.mimeType,
                }
            )
        return await legacy.cloning_metadata_service.generate_cloning_prompts(
            images=image_items,
            provider=request.provider,
            model=request.model,
            fallback_models=request.fallbackModels,
        )

    return await legacy._logged_managed_adobe_call(
        auth_ctx,
        legacy.LOG_OP_ADOBE_CLONING_PROMPTS,
        request_payload,
        _run,
        container.db,
    )


@router.post("/api/generate-cloning-video-prompt")
async def generate_cloning_video_prompt(
    request: GenerateCloningVideoPromptRequest,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Generate a video cloning prompt JSON string from image clone JSON."""
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    legacy._require_managed_scope(auth_ctx, "adobe:cloning")

    extras = request.model_extra or {}
    request_payload = {**request.model_dump(), **(extras or {})}

    async def _run():
        return await legacy.cloning_metadata_service.generate_cloning_video_prompt(
            payload={
                "imageClonePrompt": request.imageClonePrompt,
                "cameraMotion": request.cameraMotion,
                "duration": request.duration,
                "negativePrompt": request.negativePrompt or "",
                "title": request.title or "",
                "image_base64": request.image_base64,
                "mimeType": request.mimeType,
            },
            provider=extras.get("provider"),
            model=extras.get("model"),
            fallback_models=extras.get("fallbackModels"),
        )

    return await legacy._logged_managed_adobe_call(
        auth_ctx,
        legacy.LOG_OP_ADOBE_CLONING_VIDEO,
        request_payload,
        _run,
        container.db,
    )


@router.post("/api/generate-metadata")
async def generate_metadata(
    request: GenerateMetadataRequest,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Generate stock metadata using request-provided metadata settings."""
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    legacy._require_managed_scope(auth_ctx, "adobe:metadata")

    inner = {
        "image_url": request.image_url,
        "image_base64": request.image_base64,
        "metadataSettings": request.metadataSettings.model_dump(),
        "dnaNoBgWorkflowActive": request.dnaNoBgWorkflowActive,
        "backend": request.backend,
        "model": request.model,
        "fallbackModels": request.fallbackModels or [],
    }
    request_payload = request.model_dump()

    async def _run():
        return await legacy.cloning_metadata_service.generate_metadata(inner)

    return await legacy._logged_managed_adobe_call(
        auth_ctx,
        legacy.LOG_OP_ADOBE_METADATA,
        request_payload,
        _run,
        container.db,
    )


@router.post("/api/tracker/contributor")
async def fetch_task_tracker_contributor_assets(
    request: TaskTrackerContributorFetchRequest,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Fetch TAS contributor-search results via direct HTTPS to tastracker.com (curl-cffi)."""
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    legacy._require_managed_scope(auth_ctx, "adobe:tracker")

    request_payload = request.model_dump()

    async def _run():
        try:
            return await legacy.task_tracker_service.fetch_contributor_assets(
                search_id=request.search_id,
                order=request.order or "creation",
                content_type=request.content_type or "all",
                pages=request.pages,
                title_filter=request.title_filter or "",
                generative_ai=request.generative_ai or "all",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            debug_logger.log_error(f"Tracker contributor fetch failed: {exc}")
            raise HTTPException(status_code=500, detail=f"Internal Error: {str(exc)}")

    return await legacy._logged_managed_adobe_call(
        auth_ctx,
        legacy.LOG_OP_ADOBE_TRACKER_CONTRIBUTOR,
        request_payload,
        _run,
        container.db,
    )


@router.post("/api/tracker/keyword")
async def fetch_task_tracker_keyword_search(
    request: TaskTrackerKeywordSearchRequest,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Proxy TAS keyword search (GET /api/search); returns upstream JSON (e.g. images array)."""
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    legacy._require_managed_scope(auth_ctx, "adobe:tracker")

    request_payload = request.model_dump()

    async def _run():
        try:
            return await legacy.task_tracker_service.fetch_keyword_search(
                q=request.q,
                order=request.order or "relevance",
                content_type=request.content_type or "all",
                pages=request.pages,
                generative_ai=request.generative_ai or "all",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            debug_logger.log_error(f"Tracker keyword search failed: {exc}")
            raise HTTPException(status_code=500, detail=f"Internal Error: {str(exc)}")

    return await legacy._logged_managed_adobe_call(
        auth_ctx,
        legacy.LOG_OP_ADOBE_TRACKER_KEYWORD,
        request_payload,
        _run,
        container.db,
    )
