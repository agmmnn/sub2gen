"""Runway model, upload, estimate, and task routes."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..api import routes as legacy
from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible
from ..services.runway_service import RunwayService


router = APIRouter()


class RunwayMediaInput(BaseModel):
    role: Optional[str] = None
    url: Optional[str] = None
    data_url: Optional[str] = None
    uri: Optional[str] = None
    asset_id: Optional[str] = None
    assetId: Optional[str] = None
    tag: Optional[str] = None
    name: Optional[str] = None
    content_type: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunwayTaskCreateRequest(BaseModel):
    model: str
    prompt: str = ""
    mode: Optional[str] = None
    media: List[RunwayMediaInput] = Field(default_factory=list)
    aspect_ratio: Optional[str] = None
    orientation: Optional[str] = None
    duration: Optional[int] = None
    resolution: Optional[str] = None
    image_size: Optional[str] = None
    num_outputs: Optional[int] = None
    seed: Optional[int] = None
    sound: Optional[bool] = None
    fps: Optional[int] = None
    voice_id: Optional[str] = None
    multi_shot: Optional[List[Dict[str, Any]]] = None
    upscale: Optional[Dict[str, Any]] = None
    options: Dict[str, Any] = Field(default_factory=dict)


class RunwayEstimateRequest(RunwayTaskCreateRequest):
    pass


@router.get("/v1/runway/models")
async def list_runway_models(
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    legacy._require_runway_scope(auth_ctx)
    service = container.runway_service
    config = await service.db.get_runway_config()
    models = await service.db.list_runway_models(enabled_only=False)

    def load_json(raw: Any, fallback: Any) -> Any:
        parsed = RunwayService._json_loads(raw, fallback)
        return parsed if isinstance(parsed, type(fallback)) else fallback

    return {
        "success": True,
        "enabled": bool(config.enabled),
        "models": [
            {
                "id": model.public_model_id,
                "display_name": model.display_name,
                "kind": model.kind,
                "task_type": model.task_type,
                "builder_key": model.builder_key,
                "is_enabled": bool(model.is_enabled),
                "live_available": bool(model.live_available),
                "available": bool(config.enabled and model.is_enabled and model.live_available),
                "disabled_reason": model.disabled_reason or "",
                "modes": load_json(model.supported_modes, []),
                "media_roles": load_json(model.media_roles, []),
                "limits": load_json(model.limits, {}),
                "option_schema": load_json(model.capability_schema, {}),
                "feature_flags": load_json(model.feature_flags, []),
                "cost_feature": model.cost_feature or "",
                "source_version": model.source_version or "",
                "last_synced_at": model.last_synced_at.isoformat() if model.last_synced_at else None,
            }
            for model in models
        ],
    }


@router.get("/v1/runway/voices")
async def list_runway_voices(
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    legacy._require_runway_scope(auth_ctx)
    service = container.runway_service
    try:
        return {"success": True, "voices": await service.get_voices()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/v1/runway/uploads")
async def upload_runway_media(
    raw_request: Request,
    file: UploadFile = File(...),
    role: str = Form("reference_image"),
    asset_group_id: Optional[str] = Form(None),
    metadata_json: str = Form("{}"),
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    legacy._require_runway_scope(auth_ctx)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    metadata: Dict[str, Any] = {}
    try:
        parsed = json.loads(metadata_json or "{}")
        if isinstance(parsed, dict):
            metadata = parsed
    except Exception as exc:
        raise HTTPException(status_code=400, detail="metadata_json must be a JSON object") from exc
    service = container.runway_service
    try:
        return await service.upload_media(
            filename=Path(file.filename or "upload.bin").name,
            content=content,
            content_type=file.content_type
            or mimetypes.guess_type(file.filename or "")[0]
            or "application/octet-stream",
            api_key_id=auth_ctx.key_id,
            base_url=legacy._get_request_base_url(raw_request),
            media_role=role,
            asset_group_id=asset_group_id,
            metadata=metadata,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/v1/runway/estimate")
async def estimate_runway_task(
    request: RunwayEstimateRequest,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    legacy._require_runway_scope(auth_ctx)
    service = container.runway_service
    try:
        estimate = await service.estimate_task(
            public_model_id=request.model,
            prompt=request.prompt,
            media=[item.model_dump(exclude_none=True) for item in request.media],
            mode=request.mode,
            aspect_ratio=request.aspect_ratio,
            orientation=request.orientation,
            duration=request.duration,
            resolution=request.resolution,
            image_size=request.image_size,
            num_outputs=request.num_outputs,
            seed=request.seed,
            sound=request.sound,
            fps=request.fps,
            voice_id=request.voice_id,
            multi_shot=request.multi_shot,
            upscale=request.upscale,
            options=request.options,
        )
        return {"success": True, "estimate": estimate}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/v1/runway/tasks")
async def create_runway_task(
    request: RunwayTaskCreateRequest,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    legacy._require_runway_scope(auth_ctx)
    service = container.runway_service
    try:
        task = await service.start_task(
            public_model_id=request.model,
            prompt=request.prompt,
            media=[item.model_dump(exclude_none=True) for item in request.media],
            mode=request.mode,
            aspect_ratio=request.aspect_ratio,
            orientation=request.orientation,
            duration=request.duration,
            resolution=request.resolution,
            image_size=request.image_size,
            num_outputs=request.num_outputs,
            seed=request.seed,
            sound=request.sound,
            fps=request.fps,
            voice_id=request.voice_id,
            multi_shot=request.multi_shot,
            upscale=request.upscale,
            options=request.options,
            api_key_id=auth_ctx.key_id,
        )
        return JSONResponse(status_code=202, content=service.task_to_public_dict(task))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/v1/runway/tasks/{job_id}")
async def get_runway_task(
    job_id: str,
    raw_request: Request,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    legacy._require_runway_scope(auth_ctx)
    service = container.runway_service
    try:
        task = await service.poll_task(
            job_id,
            api_key_id=auth_ctx.key_id,
            base_url=legacy._get_request_base_url(raw_request),
        )
        return service.task_to_public_dict(task)
    except KeyError:
        raise HTTPException(status_code=404, detail="Runway job not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/v1/runway/tasks/{job_id}/cancel")
async def cancel_runway_task(
    job_id: str,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    legacy._require_runway_scope(auth_ctx)
    service = container.runway_service
    try:
        task = await service.cancel_task(job_id, api_key_id=auth_ctx.key_id)
        return service.task_to_public_dict(task)
    except KeyError:
        raise HTTPException(status_code=404, detail="Runway job not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
