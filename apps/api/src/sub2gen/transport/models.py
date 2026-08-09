"""OpenAI- and Gemini-compatible model catalog routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..api import routes as legacy
from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible


router = APIRouter()


@router.get("/v1/styles")
async def list_styles(
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """List locally available and explicitly enabled style presets."""
    del auth_ctx
    return {
        "object": "list",
        "data": [
            {
                "id": preset.style_id,
                "name": preset.name,
                "source": preset.source,
                "reference_count": len(preset.references),
            }
            for preset in container.style_registry.list()
        ],
    }


@router.get("/v1/models")
async def list_models(
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """List available models."""
    models = [
        {
            "id": model["id"],
            "object": "model",
            "owned_by": "sub2gen",
            "description": model["description"],
            **({"studio": model["studio"]} if model.get("studio") else {}),
        }
        for model in await legacy._get_openai_model_catalog(container.db)
    ]
    models.extend(
        {
            "id": model["id"],
            "object": "model",
            "owned_by": "runway",
            "description": model["description"],
            **({"studio": model["studio"]} if model.get("studio") else {}),
        }
        for model in await legacy._get_runway_openai_model_catalog(container.runway_service)
    )
    models.extend(
        {
            "id": model["id"],
            "object": "model",
            "owned_by": "geminigen",
            "description": model["description"],
            **({"studio": model["studio"]} if model.get("studio") else {}),
        }
        for model in await legacy._get_geminigen_openai_model_catalog(container.geminigen_service)
    )

    return {"object": "list", "data": models}


@router.get("/v1/generation-capacity")
async def get_generation_capacity(
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Return aggregate provider thread capacity without exposing account details."""
    legacy._require_geminigen_scope(auth_ctx)
    capacity = await container.db.get_geminigen_generation_capacity()
    return {
        "object": "generation_capacity",
        "providers": {
            "geminigen": {
                "image_threads": capacity["image_threads"],
                "video_threads": capacity["video_threads"],
            }
        },
    }


@router.get("/v1/models/aliases")
async def list_model_aliases(
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """List simplified model aliases for generationConfig-based resolution."""
    active_tokens = await legacy._get_active_native_tokens(container.db)
    aliases = (
        legacy.get_base_model_aliases(include_4k=legacy._has_active_native_ultra_account(active_tokens))
        if active_tokens
        else {}
    )
    alias_models = []
    for alias_id, description in aliases.items():
        alias_models.append(
            {
                "id": alias_id,
                "object": "model",
                "owned_by": "sub2gen",
                "description": description,
                "is_alias": True,
            }
        )
    return {"object": "list", "data": alias_models}


@router.get("/v1beta/models")
@router.get("/models")
async def list_gemini_models(
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """List available models using Gemini-compatible response shape."""
    catalog = await legacy._get_gemini_model_catalog(container.db, container.geminigen_service)
    return {
        "models": [
            legacy._build_gemini_model_resource(model_id, description) for model_id, description in catalog.items()
        ]
    }


@router.get("/v1beta/models/{model}")
@router.get("/models/{model}")
async def get_gemini_model(
    model: str,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Return a single model using Gemini-compatible response shape."""
    catalog = await legacy._get_gemini_model_catalog(container.db, container.geminigen_service)
    description = catalog.get(model)
    if not description:
        return JSONResponse(
            status_code=404,
            content=legacy._build_gemini_error_payload(404, f"Model not found: {model}"),
        )

    return legacy._build_gemini_model_resource(model, description)
