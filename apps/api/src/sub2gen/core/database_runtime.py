"""Runtime database configuration shared by the app and migration tools."""

from __future__ import annotations

import os
import re
import asyncio
import sys
from dataclasses import dataclass


_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# psycopg async uses readiness callbacks that are incompatible with Windows'
# Proactor loop. Set the supported policy before application/test loops exist.
if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class DatabaseSettings:
    backend: str
    url: str
    schema: str
    pool_min_size: int
    pool_max_size: int
    pool_timeout_seconds: int
    statement_timeout_seconds: int
    require_cutover_marker: bool

    @classmethod
    def from_env(cls, *, backend: str | None = None, url: str | None = None) -> "DatabaseSettings":
        selected_backend = str(
            backend if backend is not None else os.environ.get("SUB2GEN_DATABASE_BACKEND", "sqlite")
        ).strip().lower()
        if selected_backend not in {"sqlite", "postgres"}:
            raise ValueError("SUB2GEN_DATABASE_BACKEND must be sqlite or postgres")
        database_url = str(
            url if url is not None else os.environ.get("SUB2GEN_DATABASE_URL", "")
        ).strip()
        if selected_backend == "postgres" and not database_url:
            raise ValueError("SUB2GEN_DATABASE_URL is required for the postgres backend")
        schema = str(os.environ.get("SUB2GEN_DB_SCHEMA", "sub2gen") or "sub2gen").strip()
        if not _SCHEMA_RE.fullmatch(schema):
            raise ValueError("SUB2GEN_DB_SCHEMA must be a valid unquoted PostgreSQL identifier")
        pool_min = _env_int("SUB2GEN_DB_POOL_MIN_SIZE", 2, minimum=1, maximum=50)
        pool_max = _env_int("SUB2GEN_DB_POOL_MAX_SIZE", 10, minimum=1, maximum=100)
        if pool_min > pool_max:
            raise ValueError("SUB2GEN_DB_POOL_MIN_SIZE cannot exceed SUB2GEN_DB_POOL_MAX_SIZE")
        return cls(
            backend=selected_backend,
            url=database_url,
            schema=schema,
            pool_min_size=pool_min,
            pool_max_size=pool_max,
            pool_timeout_seconds=_env_int(
                "SUB2GEN_DB_POOL_TIMEOUT_SECONDS", 5, minimum=1, maximum=120
            ),
            statement_timeout_seconds=_env_int(
                "SUB2GEN_DB_STATEMENT_TIMEOUT_SECONDS", 30, minimum=1, maximum=600
            ),
            require_cutover_marker=env_bool("SUB2GEN_REQUIRE_CUTOVER_MARKER", True),
        )
