"""
HTTP routes compatible with sub2gen ``remote_browser`` (see
``apps/api/src/sub2gen/services/flow_client.py``).
"""
import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path

from .config import load_settings
from .deps import require_sub2gen_bearer
from .state import registry

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/v1/solve")
async def api_v1_solve(
    body: dict[str, Any],
    _auth: str = Depends(require_sub2gen_bearer),
) -> dict[str, Any]:
    project_id = str(body.get("project_id") or "")
    action = str(body.get("action") or "IMAGE_GENERATION")

    if not project_id:
        raise HTTPException(
            status_code=400,
            detail="project_id is required for agent routing",
        )

    s = load_settings()
    try:
        result = await registry.dispatch_solve(
            project_id=project_id,
            action=action,
            timeout=float(s.solve_timeout_seconds),
        )
    except LookupError:
        raise HTTPException(
            status_code=503,
            detail="no agent connected",
        ) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="agent solve timeout",
        ) from None
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.exception("dispatch_solve failed")
        raise HTTPException(status_code=500, detail=str(e)) from e

    token = result.get("token")
    session_id = result.get("session_id")
    if not token or not session_id:
        raise HTTPException(
            status_code=500,
            detail="agent returned incomplete token/session_id",
        )
    out: dict[str, Any] = {
        "token": token,
        "session_id": str(session_id),
    }
    fp = result.get("fingerprint")
    if isinstance(fp, dict):
        out["fingerprint"] = fp
    return out


@router.post("/api/v1/prefill")
async def api_v1_prefill(
    body: dict[str, Any],
    _auth: str = Depends(require_sub2gen_bearer),
) -> dict[str, Any]:
    logger.info("prefill %s", body)
    return {"ok": True}


@router.get("/api/v1/agents")
async def api_v1_agents(
    _auth: str = Depends(require_sub2gen_bearer),
) -> dict[str, Any]:
    agents = await registry.list_agents()
    return {
        "ok": True,
        "count": len(agents),
        "agents": agents,
    }


@router.post("/api/v1/sessions/{session_id}/finish")
async def api_v1_session_finish(
    session_id: str = Path(...),
    body: Optional[dict[str, Any]] = None,
    _auth: str = Depends(require_sub2gen_bearer),
) -> dict[str, Any]:
    logger.info("session finish %s %s", session_id, body)
    return {"ok": True}


@router.post("/api/v1/sessions/{session_id}/error")
async def api_v1_session_error(
    session_id: str = Path(...),
    body: Optional[dict[str, Any]] = None,
    _auth: str = Depends(require_sub2gen_bearer),
) -> dict[str, Any]:
    logger.info("session error %s %s", session_id, body)
    return {"ok": True}
