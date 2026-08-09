"""Managed client presence and account visibility routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible, verify_managed_presence_key
from ..services.redis_runtime import RedisUnavailableError


router = APIRouter()


@router.post("/api/client/presence", status_code=204)
async def report_client_presence(
    auth_ctx: AuthContext = Depends(verify_managed_presence_key),
    container: AppContainer = Depends(get_container),
):
    """Record a lightweight heartbeat for a managed desktop client."""
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=503, detail="API key manager not initialized")
    api_key_manager = container.api_key_manager
    runtime = api_key_manager.redis_runtime
    if runtime is not None and runtime.ready:
        try:
            await runtime.touch_presence(auth_ctx.key_id)
        except RedisUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail="redis_unavailable",
                headers={"Retry-After": "5"},
            ) from exc
        if not runtime.required:
            await api_key_manager.db.touch_api_key_presence(auth_ctx.key_id)
    elif runtime is not None and runtime.required:
        raise HTTPException(
            status_code=503,
            detail="redis_unavailable",
            headers={"Retry-After": "5"},
        )
    else:
        await api_key_manager.db.touch_api_key_presence(auth_ctx.key_id)
    return Response(status_code=204)


@router.get("/v1/api-key/allowed-tokens")
async def get_allowed_tokens(
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Get the allowed tokens (accounts) and their credits for the current API key."""
    database = container.db

    tokens_info = []
    for token_id in auth_ctx.allowed_accounts:
        token = await database.get_token(token_id)
        if token and token.is_active:
            tokens_info.append(
                {
                    "id": token.id,
                    "email": token.email,
                    "label": token.remark or token.name or "default",
                    "credits": token.credits,
                    "user_paygate_tier": token.user_paygate_tier,
                    "is_active": token.is_active,
                }
            )

    return {
        "success": True,
        "api_key_label": auth_ctx.key_label,
        "allowed_tokens": tokens_info,
    }
