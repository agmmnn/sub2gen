from fastapi import APIRouter

router = APIRouter()


def matches(path: str) -> bool:
    return path.startswith("/api/logs") or path == "/api/stats"
