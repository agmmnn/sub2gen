from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import load_settings

security = HTTPBearer(auto_error=False)


def require_sub2gen_bearer(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    s = load_settings()
    if not s.sub2gen_bearer:
        raise HTTPException(
            status_code=500,
            detail="GATEWAY_SUB2GEN_BEARER is not set",
        )
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid connection token")
    accepted = {s.sub2gen_bearer}
    if s.sub2gen_bearer_previous:
        accepted.add(s.sub2gen_bearer_previous)
    if creds.credentials not in accepted:
        raise HTTPException(status_code=401, detail="Invalid connection token")
    return creds.credentials
