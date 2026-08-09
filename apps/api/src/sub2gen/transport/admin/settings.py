from fastapi import APIRouter

router = APIRouter()
PREFIXES = (
    "/api/config",
    "/api/proxy",
    "/api/generation",
    "/api/token-refresh",
    "/api/captcha",
    "/api/plugin",
    "/api/call-logic",
    "/api/admin/debug",
)


def matches(path: str) -> bool:
    return path.startswith(PREFIXES)
