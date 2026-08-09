"""Authentication module"""

import bcrypt
from typing import Optional
from fastapi import Depends, Header, HTTPException, Query, Security, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .config import config
from .api_key_manager import AuthContext
from ..services.redis_runtime import RedisUnavailableError, is_new_protected_work
from ..bootstrap.container import AppContainer
from ..bootstrap.dependencies import get_container

security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)
def _redis_unavailable_response(exc: RedisUnavailableError) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="redis_unavailable",
        headers={"Retry-After": "5"},
    )

async def _record_api_key_audit(
    *,
    api_key_id: Optional[int],
    endpoint: str,
    account_id: Optional[int],
    status_code: int,
    detail: str,
    request: Request,
    container: AppContainer,
) -> None:
    api_key_manager = container.api_key_manager
    payload = {
        "api_key_id": api_key_id,
        "endpoint": endpoint,
        "account_id": account_id,
        "status_code": status_code,
        "detail": detail,
        "ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
    }
    runtime = getattr(api_key_manager, "redis_runtime", None)
    if runtime is not None and runtime.ready:
        try:
            await runtime.queue_audit(payload)
        except RedisUnavailableError:
            if runtime.required and is_new_protected_work(request.method, endpoint):
                raise
        else:
            if runtime.required:
                return
    await api_key_manager.db.insert_api_key_audit_log(**payload)

class AuthManager:
    """Authentication manager"""

    @staticmethod
    def verify_api_key(api_key: str) -> bool:
        """Verify API key"""
        return api_key == config.api_key

    @staticmethod
    def verify_admin(username: str, password: str) -> bool:
        """Verify admin credentials"""
        # Compare with current config (which may be from database or config file)
        return username == config.admin_username and password == config.admin_password

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password"""
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """Verify password"""
        return bcrypt.checkpw(password.encode(), hashed.encode())

async def verify_api_key_header(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """Verify API key from Authorization header"""
    api_key = credentials.credentials
    if not AuthManager.verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key


async def verify_api_key_flexible(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(optional_security),
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    key: Optional[str] = Query(None),
    container: AppContainer = Depends(get_container),
) -> AuthContext:
    """Verify API key from Authorization header, x-goog-api-key header, or key query param."""
    api_key = None

    if credentials is not None:
        api_key = credentials.credentials
    elif x_goog_api_key:
        api_key = x_goog_api_key
    elif key:
        api_key = key

    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    endpoint = request.url.path
    require_assignment = endpoint == "/v1/projects" and request.method.upper() == "POST"
    api_key_manager = container.api_key_manager

    try:
        runtime = getattr(api_key_manager, "redis_runtime", None)
        require_redis = bool(
            runtime is not None
            and runtime.required
            and is_new_protected_work(request.method, endpoint)
        )
        context = await api_key_manager.authenticate(
            api_key,
            endpoint=endpoint,
            require_assignment=require_assignment,
            require_redis=require_redis,
        )
        await _record_api_key_audit(
            api_key_id=context.key_id,
            endpoint=endpoint,
            account_id=None,
            status_code=200,
            detail="ok",
            request=request,
            container=container,
        )
        return context
    except RedisUnavailableError as exc:
        raise _redis_unavailable_response(exc) from exc
    except PermissionError as exc:
        try:
            await _record_api_key_audit(
                api_key_id=None,
                endpoint=endpoint,
                account_id=None,
                status_code=403 if require_assignment else 401,
                detail=str(exc),
                request=request,
                container=container,
            )
        except RedisUnavailableError as redis_exc:
            raise _redis_unavailable_response(redis_exc) from redis_exc
        if "accounts assigned" in str(exc).lower():
            raise HTTPException(status_code=403, detail=str(exc))
        raise HTTPException(status_code=401, detail=str(exc))
    except RuntimeError as exc:
        try:
            await _record_api_key_audit(
                api_key_id=None,
                endpoint=endpoint,
                account_id=None,
                status_code=429,
                detail=str(exc),
                request=request,
                container=container,
            )
        except RedisUnavailableError as redis_exc:
            raise _redis_unavailable_response(redis_exc) from redis_exc
        raise HTTPException(status_code=429, detail=str(exc))


async def verify_managed_presence_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(optional_security),
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    container: AppContainer = Depends(get_container),
) -> AuthContext:
    """Validate a managed presence heartbeat without usage, limits, or audit side effects."""
    api_key = credentials.credentials if credentials is not None else x_goog_api_key
    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    try:
        context = await container.api_key_manager.authenticate(
            api_key,
            endpoint=request.url.path,
            enforce_rate_limits=False,
            touch_usage=False,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    if context.is_legacy or context.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required")
    return context
