"""Public Flow project HTTP routes."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible
from ..core.models import FlowProjectCreateRequest, Project


router = APIRouter()


def _require_managed_projects_read(auth_ctx: AuthContext) -> None:
    """Managed keys may read projects with a read, write, or wildcard scope."""
    if auth_ctx.is_legacy:
        return
    if "*" in auth_ctx.scopes or "projects:read" in auth_ctx.scopes or "projects:write" in auth_ctx.scopes:
        return
    raise HTTPException(
        status_code=403,
        detail="Missing scope: allow '*', 'projects:read', or 'projects:write'",
    )


def _project_row_to_api_dict(project: Project) -> Dict[str, Any]:
    """Serialize a project model for public JSON APIs."""
    payload = project.model_dump()
    created = payload.get("created_at")
    if created is not None and hasattr(created, "isoformat"):
        payload["created_at"] = created.isoformat()
    return {
        "project_id": payload.get("project_id"),
        "project_name": payload.get("project_name"),
        "token_id": payload.get("token_id"),
        "is_active": bool(payload.get("is_active", True)),
        "created_at": payload.get("created_at"),
    }


def _require_managed_projects_write(auth_ctx: AuthContext) -> None:
    """Managed keys need wildcard or projects:write to create Flow projects."""
    if auth_ctx.is_legacy:
        return
    if "*" in auth_ctx.scopes or "projects:write" in auth_ctx.scopes:
        return
    raise HTTPException(
        status_code=403,
        detail="Missing scope: allow '*' or add 'projects:write' for this key",
    )


@router.get("/v1/projects")
async def list_flow_projects(
    account_id: Optional[int] = Query(None),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """List VideoFX projects visible to this managed API key (optional filter by account / token id)."""
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    _require_managed_projects_read(auth_ctx)
    database = container.db
    key_id = auth_ctx.key_id
    limit_clean = max(1, min(int(limit), 100))
    offset_clean = max(0, int(offset))
    if account_id is not None:
        account_id_clean = int(account_id)
        if account_id_clean not in auth_ctx.allowed_accounts:
            raise HTTPException(status_code=400, detail="account_id is not assigned to this API key")
        total = await database.count_projects_for_api_key_account(key_id, account_id_clean)
        projects = await database.list_projects_for_api_key_account(
            key_id,
            account_id_clean,
            limit=limit_clean,
            offset=offset_clean,
        )
    else:
        total = await database.count_projects_by_api_key(key_id)
        projects = await database.list_projects_by_api_key(
            key_id,
            limit=limit_clean,
            offset=offset_clean,
        )
    data = [_project_row_to_api_dict(project) for project in projects]
    return {
        "object": "list",
        "data": data,
        "total": total,
        "limit": limit_clean,
        "offset": offset_clean,
    }


@router.get("/v1/projects/{project_id}")
async def get_flow_project(
    project_id: str,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Return one VideoFX project row if it belongs to this managed API key."""
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    _require_managed_projects_read(auth_ctx)
    project_id_clean = project_id.strip()
    if not project_id_clean:
        raise HTTPException(status_code=400, detail="project_id is required")
    project = await container.db.get_project_by_id(project_id_clean, auth_ctx.key_id)
    if not project or int(project.token_id) not in auth_ctx.allowed_accounts:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"object": "flow_project", **_project_row_to_api_dict(project)}


@router.post("/v1/projects")
async def create_flow_project(
    body: FlowProjectCreateRequest,
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
):
    """Create VideoFX project(s) for managed key assigned account(s)."""
    raise HTTPException(status_code=410, detail="Project management APIs have been removed")
