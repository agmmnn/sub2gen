"""Managed-key cache discovery and delivery routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..api import routes as legacy
from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container
from ..core.api_key_manager import AuthContext
from ..core.auth import verify_api_key_flexible


router = APIRouter()


def _cache_file_row_to_list_item(row: Dict[str, Any], file_cache: Any = None) -> Dict[str, Any]:
    """Shape a cache_files row for GET /api/cache/file list APIs."""
    filename = Path(str(row.get("filename") or "")).name
    flow_project_id = legacy._strip_optional_project_id(row.get("flow_project_id"))
    download_path = f"/api/cache/blob/{filename}"
    if flow_project_id:
        download_path = f"{download_path}?project_id={quote(flow_project_id, safe='')}"
    if row.get("delivery_mode") == "cdn" and file_cache is not None:
        direct = (
            file_cache.backend.public_url(filename) if file_cache and getattr(file_cache, "backend", None) else None
        )
        if direct:
            download_path = direct
    created = row.get("created_at")
    updated = row.get("updated_at")
    return {
        "filename": filename,
        "flow_project_id": flow_project_id,
        "media_type": row.get("media_type"),
        "source_url": row.get("source_url"),
        "token_id": row.get("token_id"),
        "storage_provider": row.get("storage_provider") or "local",
        "delivery_mode": row.get("delivery_mode") or "proxy",
        "size_bytes": row.get("size_bytes"),
        "created_at": created.isoformat()
        if hasattr(created, "isoformat")
        else (str(created) if created is not None else None),
        "updated_at": updated.isoformat()
        if hasattr(updated, "isoformat")
        else (str(updated) if updated is not None else None),
        "download_path": download_path,
    }


@router.get("/api/cache/file")
async def list_cache_files_for_key(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """List cache file metadata rows owned by this managed API key."""
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    handler = container.generation_handler
    key_id = auth_ctx.key_id
    limit_clean = int(limit)
    offset_clean = int(offset)
    total = await handler.db.count_cache_files_for_api_key(key_id)
    rows = await handler.db.list_cache_files_for_api_key(
        key_id,
        limit=limit_clean,
        offset=offset_clean,
    )
    data = [_cache_file_row_to_list_item(row, handler.file_cache) for row in rows]
    return {
        "object": "list",
        "data": data,
        "pagination": {
            "total": total,
            "limit": limit_clean,
            "offset": offset_clean,
            "has_more": offset_clean + len(data) < total,
        },
    }


@router.get("/api/cache/file/{project_id}")
async def list_cache_files_for_key_project(
    project_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """List cache file metadata for one Flow project UUID under this managed API key."""
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    project_id_clean = project_id.strip()
    if not project_id_clean:
        raise HTTPException(status_code=400, detail="project_id is required")
    handler = container.generation_handler
    project = await handler.db.get_project_by_id(project_id_clean, auth_ctx.key_id)
    if not project:
        raise HTTPException(status_code=400, detail="project_id not found for this API key")
    token_id = int(project.token_id)
    if token_id not in auth_ctx.allowed_accounts:
        raise HTTPException(status_code=400, detail="project_id is not assigned to this API key")
    key_id = auth_ctx.key_id
    limit_clean = int(limit)
    offset_clean = int(offset)
    total = await handler.db.count_cache_files_for_api_key_project(key_id, project_id_clean)
    rows = await handler.db.list_cache_files_for_api_key_project(
        key_id,
        project_id_clean,
        limit=limit_clean,
        offset=offset_clean,
    )
    data = [_cache_file_row_to_list_item(row, handler.file_cache) for row in rows]
    return {
        "object": "list",
        "data": data,
        "pagination": {
            "total": total,
            "limit": limit_clean,
            "offset": offset_clean,
            "has_more": offset_clean + len(data) < total,
        },
    }


@router.get("/api/cache/blob/{filename}")
async def get_cached_blob(
    filename: str,
    request: Request,
    project_id: Optional[str] = Query(None),
    auth_ctx: AuthContext = Depends(verify_api_key_flexible),
    container: AppContainer = Depends(get_container),
):
    """Stream a cache file owned by this managed API key (use list endpoints to discover filenames)."""
    handler = container.generation_handler
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    safe_name = Path(filename).name
    metadata = await handler.db.get_cache_file_for_api_key(safe_name, auth_ctx.key_id)
    if not metadata:
        raise HTTPException(status_code=403, detail="Cache file not owned by this API key")
    metadata_project_id = legacy._strip_optional_project_id(metadata.get("flow_project_id"))
    if metadata_project_id:
        requested_project_id = legacy._strip_optional_project_id(project_id)
        if not requested_project_id or requested_project_id != metadata_project_id:
            raise HTTPException(
                status_code=403,
                detail="project_id query parameter required and must match the cache entry",
            )
        project = await handler.db.get_project_by_id(requested_project_id, auth_ctx.key_id)
        if not project:
            raise HTTPException(status_code=400, detail="project_id not found for this API key")
        token_id = int(project.token_id)
        if token_id not in auth_ctx.allowed_accounts:
            raise HTTPException(status_code=400, detail="project_id is not assigned to this API key")
    try:
        cached = await handler.file_cache.open_cached(
            safe_name,
            request.headers.get("range") if request else None,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Cache file not found")
    except ValueError as exc:
        raise HTTPException(status_code=416, detail=str(exc))
    except Exception as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            raise HTTPException(status_code=404, detail="Cache file not found")
        raise
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(cached.content_length),
        "Content-Disposition": f'inline; filename="{safe_name}"',
    }
    if cached.content_range:
        headers["Content-Range"] = cached.content_range
    if cached.etag:
        headers["ETag"] = cached.etag
    if cached.last_modified:
        headers["Last-Modified"] = cached.last_modified.strftime("%a, %d %b %Y %H:%M:%S GMT")
    return StreamingResponse(
        cached.body,
        status_code=cached.status_code,
        media_type=cached.content_type,
        headers=headers,
    )
