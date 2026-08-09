"""Extension worker WebSocket transport."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..bootstrap.dependencies import get_websocket_container
from ..core.logger import debug_logger
from ..persistence.repositories import Repositories
from ..services.browser_captcha_extension import ExtensionCaptchaService


router = APIRouter()


@router.websocket("/captcha_ws")
async def captcha_websocket_endpoint(websocket: WebSocket):
    container = get_websocket_container(websocket)
    database = container.db
    repositories = getattr(container, "repositories", None) or Repositories.from_database(
        database
    )
    captcha_worker_key = (
        websocket.query_params.get("captcha_worker_key")
        or websocket.query_params.get("captcha_key")
        or websocket.headers.get("x-sub2gen-captcha-worker-key")
    )
    if captcha_worker_key:
        if not hasattr(database, "get_captcha_worker_key_by_hash"):
            await websocket.accept()
            await websocket.close(code=1011, reason="Captcha worker auth unavailable")
            return
        captcha_worker_key_hash = hashlib.sha256(captcha_worker_key.encode("utf-8")).hexdigest()
        captcha_worker = await repositories.workers.get_captcha_worker_key_by_hash(
            captcha_worker_key_hash
        )
        if not captcha_worker or not bool(captcha_worker.get("is_active", True)):
            await websocket.accept()
            await websocket.close(code=1008, reason="Invalid captcha worker key")
            return
        service = await ExtensionCaptchaService.get_instance(db=database)
        await service.connect(websocket, authenticated_captcha_worker=captcha_worker)
        try:
            while True:
                data = await websocket.receive_text()
                await service.handle_message(websocket, data)
        except WebSocketDisconnect:
            service.disconnect(websocket)
        except Exception as exc:
            debug_logger.log_error(f"WebSocket error: {exc}")
            service.disconnect(websocket)
        return

    worker_key = (
        websocket.query_params.get("worker_key")
        or websocket.query_params.get("worker_auth_key")
        or websocket.headers.get("x-sub2gen-worker-key")
    )
    if worker_key:
        await websocket.accept()
        await websocket.close(code=1008, reason="Refresh worker keys removed; use refresh_token_id")
        return

    raw_refresh_token_id = websocket.query_params.get("refresh_token_id")
    if raw_refresh_token_id is not None:
        try:
            refresh_token_id = int(str(raw_refresh_token_id).strip())
        except (TypeError, ValueError):
            await websocket.accept()
            await websocket.close(code=1008, reason="refresh_token_id must be a positive integer")
            return
        if refresh_token_id <= 0:
            await websocket.accept()
            await websocket.close(code=1008, reason="refresh_token_id must be a positive integer")
            return
        if not hasattr(database, "get_token"):
            await websocket.accept()
            await websocket.close(code=1011, reason="Refresh token lookup unavailable")
            return
        refresh_token = await repositories.accounts.get_token(refresh_token_id)
        if not refresh_token:
            await websocket.accept()
            await websocket.close(code=1008, reason="refresh_token_id token not found")
            return
        service = await ExtensionCaptchaService.get_instance(db=database)
        await service.connect(websocket, refresh_token_id=refresh_token_id)
        try:
            while True:
                data = await websocket.receive_text()
                await service.handle_message(websocket, data)
        except WebSocketDisconnect:
            service.disconnect(websocket)
        except Exception as exc:
            debug_logger.log_error(f"WebSocket error: {exc}")
            service.disconnect(websocket)
        return

    api_key = (
        websocket.query_params.get("key")
        or websocket.query_params.get("api_key")
        or websocket.headers.get("x-goog-api-key")
    )
    if not api_key:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            api_key = auth_header[7:].strip()
    if not api_key:
        await websocket.accept()
        await websocket.close(code=1008, reason="Missing API key")
        return

    try:
        auth_ctx = await container.api_key_manager.authenticate(
            api_key,
            endpoint="/captcha_ws",
            require_assignment=False,
        )
    except PermissionError as exc:
        await websocket.accept()
        await websocket.close(code=1008, reason=str(exc) or "Invalid API key")
        return
    except RuntimeError as exc:
        await websocket.accept()
        await websocket.close(code=1013, reason=str(exc) or "Rate limited")
        return

    if auth_ctx.is_legacy or auth_ctx.key_id is None:
        await websocket.accept()
        await websocket.close(code=1008, reason="Managed API key required")
        return

    service = await ExtensionCaptchaService.get_instance(db=database)
    await service.connect(websocket, authenticated_managed_api_key_id=int(auth_ctx.key_id))
    try:
        while True:
            data = await websocket.receive_text()
            await service.handle_message(websocket, data)
    except WebSocketDisconnect:
        service.disconnect(websocket)
    except Exception as exc:
        debug_logger.log_error(f"WebSocket error: {exc}")
        service.disconnect(websocket)
