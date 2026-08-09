"""Chrome extension HTTP routes."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ..api import routes as legacy
from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible
from ..core.config import config as app_config
from ..core.logger import debug_logger
from ..services.browser_captcha_extension import ExtensionCaptchaService
from ..persistence import ProviderAccountRecord


router = APIRouter()


class ExtensionAccountImportRequest(BaseModel):
    """Credentials captured from the currently signed-in Chrome profile."""

    session_token: str = Field(min_length=1, max_length=16_384)
    google_cookies: str = Field(min_length=2, max_length=262_144)
    refresh_interval_minutes: int = Field(default=120, ge=5, le=1_440)
    worker_id: str | None = Field(default=None, max_length=160)


def _require_token_import_scope(auth_ctx: AuthContext) -> None:
    """Restrict browser credential imports to explicitly trusted API keys."""
    if auth_ctx.is_legacy or "*" in auth_ctx.scopes or "tokens:import" in auth_ctx.scopes:
        return
    raise HTTPException(status_code=403, detail="Missing scope: tokens:import")


@router.post("/api/extension/import-current-account")
async def extension_import_current_account(
    body: ExtensionAccountImportRequest,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Add or update the Google account signed in to the extension's Chrome profile."""
    _require_token_import_scope(auth_ctx)
    token_manager = container.token_manager
    database = token_manager.db

    session_token = body.session_token.strip()
    try:
        raw_cookies = json.loads(body.google_cookies)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="google_cookies must be valid JSON") from exc
    if not isinstance(raw_cookies, list) or not raw_cookies:
        raise HTTPException(status_code=400, detail="google_cookies must be a non-empty list")

    normalized_cookies = []
    for raw_cookie in raw_cookies:
        if not isinstance(raw_cookie, dict):
            continue
        name = str(raw_cookie.get("name") or "").strip()
        value = str(raw_cookie.get("value") or "").strip()
        if not name or not value:
            continue
        normalized_cookies.append(
            {
                "name": name,
                "value": value,
                "domain": str(raw_cookie.get("domain") or ""),
                "path": str(raw_cookie.get("path") or "/"),
                "expirationDate": raw_cookie.get("expirationDate"),
            }
        )
    if not normalized_cookies:
        raise HTTPException(status_code=400, detail="google_cookies contains no usable cookies")
    google_cookies = json.dumps(normalized_cookies, ensure_ascii=False, separators=(",", ":"))

    try:
        result = await token_manager.flow_client.st_to_at(session_token)
        access_token = str(result.get("access_token") or "").strip()
        user_info = result.get("user") or {}
        email = str(user_info.get("email") or "").strip()
        expires = result.get("expires")
        if not access_token or not email:
            raise HTTPException(status_code=400, detail="Could not resolve the Google account from the session token")

        at_expires = None
        if expires:
            try:
                at_expires = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                at_expires = None
        if at_expires is not None:
            aware_expires = at_expires if at_expires.tzinfo else at_expires.replace(tzinfo=timezone.utc)
            if aware_expires <= datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=400,
                    detail="The Google Labs session is expired; reopen Flow and import again",
                )

        existing = await database.get_token_by_email(email)
        common_fields = {
            "protocol_mode": "protocol",
            "google_cookies": google_cookies,
            "auto_refresh_enabled": True,
            "refresh_interval_minutes": body.refresh_interval_minutes,
        }
        if existing is not None:
            await token_manager.update_token(
                token_id=existing.id,
                st=session_token,
                at=access_token,
                at_expires=at_expires,
                **common_fields,
            )
            token_id = int(existing.id)
            added = 0
            updated = 1
        else:
            created = await token_manager.add_token(
                st=session_token,
                remark="Imported by Chrome extension",
                image_enabled=True,
                video_enabled=True,
                image_concurrency=-1,
                video_concurrency=-1,
                **common_fields,
            )
            token_id = int(created.id)
            added = 1
            updated = 0

        await database.update_token(
            token_id,
            last_st_refresh_result="Chrome extension synchronized the current Google account",
        )

        if auth_ctx.key_id is not None:
            assigned_accounts = set(await database.get_api_key_account_ids(auth_ctx.key_id))
            if token_id not in assigned_accounts:
                assigned_accounts.add(token_id)
                await database.update_api_key(
                    auth_ctx.key_id,
                    account_ids=sorted(assigned_accounts),
                )
                await container.api_key_manager.invalidate(auth_ctx.key_id)

        provider_account = None
        repositories = getattr(container, "repositories", None)
        if repositories is not None:
            provider_account = await repositories.provider_accounts.get_by_legacy(
                "google-flow", "tokens", str(token_id)
            )
            if provider_account is None:
                provider_account = await repositories.provider_accounts.create(
                    ProviderAccountRecord(
                        provider_key="google-flow",
                        label=email,
                        external_account_id=email,
                        legacy_source="tokens",
                        legacy_id=str(token_id),
                        metadata={"billing_pool": "google-flow:subscription"},
                    )
                )
            if auth_ctx.key_id is not None:
                await repositories.provider_accounts.assign_api_key(provider_account.id, auth_ctx.key_id)
            if body.worker_id:
                worker = await repositories.workers.get_device_for_auth(body.worker_id)
                if worker is None or not worker.enabled or worker.revoked_at is not None:
                    raise HTTPException(status_code=400, detail="The paired worker is unavailable")
                await repositories.credential_bindings.bind_worker_session(
                    provider_account_id=provider_account.id,
                    worker_id=worker.id,
                )

        return {
            "success": True,
            "added": added,
            "updated": updated,
            "email": email,
            "token_id": token_id,
            "provider_account_id": provider_account.id if provider_account is not None else None,
            "expires": expires,
        }
    except HTTPException:
        raise
    except Exception as exc:
        debug_logger.log_error(f"[EXTENSION_IMPORT] Current Google account import failed: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/extension/generation-upload")
async def extension_generation_upload(
    request: Request,
    upload_id: str = Query(..., description="Slot id from submit_generation message"),
    upload_secret: str = Query(..., description="One-time secret for this upload"),
):
    """Receive an extension generation response body over the authenticated HTTP side channel."""
    body = await request.body()
    if len(body) > int(app_config.extension_generation_upload_max_bytes):
        raise HTTPException(status_code=413, detail="body_too_large")
    service = await ExtensionCaptchaService.get_instance()
    ok, error = await service.ingest_generation_upload_body(upload_id, upload_secret, body)
    if not ok:
        raise HTTPException(status_code=400, detail=error)
    return Response(status_code=204)


@router.get("/api/extension/metadata-session")
async def extension_metadata_session(
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
):
    """Validate that a managed key may activate the sub2gen Metadata extension."""
    if auth_ctx.is_legacy or auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    legacy._require_managed_scope(auth_ctx, "adobe:metadata")
    return {
        "active": True,
        "service": "sub2gen-metadata",
        "keyLabel": auth_ctx.key_label,
        "capabilities": ["adobe:metadata"],
    }
