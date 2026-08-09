from fastapi import APIRouter

router = APIRouter()
PREFIXES = ("/api/admin/captcha-worker-keys", "/api/admin/extension/workers")


def matches(path: str) -> bool:
    return path.startswith(PREFIXES)
