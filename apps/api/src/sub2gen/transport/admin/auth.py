from fastapi import APIRouter

router = APIRouter()
PATHS = {
    "/api/admin/login",
    "/api/admin/logout",
    "/api/admin/change-password",
    "/api/login",
    "/api/logout",
    "/api/admin/password",
    "/api/admin/apikey",
}


def matches(path: str) -> bool:
    return path in PATHS
