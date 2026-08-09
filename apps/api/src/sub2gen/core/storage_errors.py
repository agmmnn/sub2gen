"""Helpers for presenting SQLite storage exhaustion as a recoverable API error."""

import sqlite3
from typing import Any

from fastapi.responses import JSONResponse


STORAGE_FULL_CODE = "storage_full"
STORAGE_FULL_DETAIL = (
    "Server storage is full. Free disk space or clear cached media, then retry."
)
STORAGE_UNAVAILABLE_CODE = "storage_unavailable"
STORAGE_UNAVAILABLE_DETAIL = "Server storage is temporarily unavailable. Please retry shortly."


def is_sqlite_storage_full_error(exc: BaseException) -> bool:
    """Return whether an exception represents SQLite's SQLITE_FULL condition."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False

    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int) and (error_code & 0xFF) == sqlite3.SQLITE_FULL:
        return True

    message = str(exc).strip().lower()
    return any(
        marker in message
        for marker in (
            "database or disk is full",
            "database is full",
            "disk is full",
        )
    )


def is_sqlite_recoverable_storage_error(exc: BaseException) -> bool:
    """Return whether startup should try freeing generated cache and retrying."""
    if is_sqlite_storage_full_error(exc):
        return True
    if not isinstance(exc, sqlite3.OperationalError):
        return False

    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int) and (error_code & 0xFF) == sqlite3.SQLITE_IOERR:
        return True

    return "disk i/o error" in str(exc).strip().lower()


async def sqlite_operational_error_handler(_request: Any, exc: sqlite3.OperationalError):
    """Render SQLite storage failures without leaking database internals."""
    if is_sqlite_storage_full_error(exc):
        return JSONResponse(
            status_code=507,
            content={"detail": STORAGE_FULL_DETAIL, "code": STORAGE_FULL_CODE},
        )
    if is_sqlite_recoverable_storage_error(exc):
        return JSONResponse(
            status_code=503,
            content={"detail": STORAGE_UNAVAILABLE_DETAIL, "code": STORAGE_UNAVAILABLE_CODE},
            headers={"Retry-After": "30"},
        )
    raise exc
