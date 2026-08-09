"""FastAPI application initialization"""

import asyncio
import errno
import gc
import heapq
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import warnings
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from pathlib import Path

from .core.config import REPO_ROOT, config
from .core.postgres_database import DatabaseUnavailableError
from .core.storage_errors import (
    is_sqlite_recoverable_storage_error,
    sqlite_operational_error_handler,
)
from .core.monitoring import (
    CONTENT_TYPE_LATEST,
    record_endpoint_duration,
    render_main_metrics,
)
from .services.browser_profile_service import BrowserProfileService
from .api import routes, admin
from .core.logger import debug_logger
from .bootstrap import AppContainer, build_container
from .bootstrap.lifecycle import build_lifespan


_LOCAL_NO_PROXY_HOSTS = ("127.0.0.1", "localhost", "::1")


async def _abort_refresh_batch_on_resource_exhaustion(
    *,
    source: str,
    reason: str = "",
    error: BaseException | None = None,
) -> bool:
    exhausted = reason == "browser_profile_resource_exhausted" or (
        error is not None and BrowserProfileService.is_resource_exhaustion_error(error)
    )
    if not exhausted:
        return False
    profile_service = BrowserProfileService.get_existing_instance()
    released = await profile_service.close_unpinned_runtimes() if profile_service is not None else 0
    debug_logger.log_error(
        f"[{source}] Container runtime capacity exhausted; released "
        f"{released} transient runtime(s) and stopped this batch"
    )
    return True


def _configure_stdio() -> None:
    for stream in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _configure_local_no_proxy() -> None:
    for env_name in ("NO_PROXY", "no_proxy"):
        entries = [
            item.strip()
            for item in str(os.environ.get(env_name, "") or "").replace(";", ",").split(",")
            if item.strip()
        ]
        normalized = {item.lower() for item in entries}
        for host in _LOCAL_NO_PROXY_HOSTS:
            if host.lower() not in normalized:
                entries.append(host)
                normalized.add(host.lower())
        os.environ[env_name] = ",".join(entries)


def _configure_asyncio_policy() -> None:
    if os.name != "nt":
        return
    policy_class = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_class is not None and not isinstance(asyncio.get_event_loop_policy(), policy_class):
        asyncio.set_event_loop_policy(policy_class())


def _configure_process_runtime() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    _configure_stdio()
    _configure_local_no_proxy()
    _configure_asyncio_policy()
    warnings.filterwarnings(
        "ignore",
        message=r".*Proactor event loop does not implement add_reader family of methods required.*",
        category=RuntimeWarning,
    )


_configure_process_runtime()


def _normalize_host(host: str) -> str:
    if not host:
        return ""
    return host.split(":")[0].strip().lower()


def _api_only_hostnames() -> set[str]:
    """Comma-separated FQDNs from ``SUB2GEN_API_ONLY_HOST`` (see ``infra/compose``)."""
    raw = (os.environ.get("SUB2GEN_API_ONLY_HOST") or "").strip()
    if not raw:
        return set()
    return {_normalize_host(h) for h in raw.split(",") if h.strip()}


def _incoming_hostname(request: Request) -> str:
    # RFC 7239 Forwarded (some proxies; Cloudflare may use X-Forwarded-Host only)
    fwd = request.headers.get("forwarded") or ""
    if fwd:
        for segment in fwd.split(","):
            for token in segment.split(";"):
                t = token.strip()
                if t.lower().startswith("host="):
                    v = t.split("=", 1)[-1].strip().strip('"')
                    if v:
                        return _normalize_host(v)
    xf = (request.headers.get("x-forwarded-host") or "").strip()
    if xf:
        return _normalize_host(xf.split(",")[0].strip())
    cdn = (request.headers.get("x-cdn-request-host") or "").strip()  # rare
    if cdn:
        return _normalize_host(cdn)
    return _normalize_host(request.headers.get("host", ""))


def _path_allowed_on_api_only_host(path: str) -> bool:
    """
    Paths allowed on the API-only public host (no admin SPA / /api on this host).

    - OpenAI / Chat Completions style: /v1/chat/completions, /v1/models, /v1/models/aliases, /v1/projects, …
    - Gemini (Google) style: /v1beta/models/…:generateContent, :streamGenerateContent, list models, …
    - Same body on alternate paths: /models, /models/{m}:generateContent, …
    - Cached media (authenticated): /api/cache/file, /api/cache/file/{project_id}, /api/cache/blob/...
    - Desktop presence (authenticated): /api/client/presence
    - Protocol-v1 workers and authenticated account import: /worker_ws, /api/extension/...
    - Discovery/liveness: /openapi.json, /health, /metrics
    """
    if path in ("/openapi.json", "/health", "/metrics", "/worker_ws"):
        return True
    if path.startswith(("/v1/", "/v1beta/")) or path in ("/v1", "/v1beta"):
        return True
    if path.startswith("/models/") or path == "/models":
        return True
    if path.startswith("/api/cache/"):
        return True
    if path.startswith("/api/extension/"):
        return True
    if path.startswith("/api/workers/"):
        return True
    if path.startswith("/api/tracker/"):
        return True
    if path == "/api/client/presence":
        return True
    # Public cloning + metadata endpoints (managed-API-key auth) used by external clients.
    if path in (
        "/api/generate-cloning-prompts",
        "/api/generate-cloning-video-prompt",
        "/api/generate-metadata",
    ):
        return True
    return False


class ApiOnlyHostMiddleware(BaseHTTPMiddleware):
    """
    If SUB2GEN_API_ONLY_HOST is set (comma-separated FQDNs), requests whose Host
    (or X-Forwarded-Host) matches get public OpenAI- and Gemini-style routes + /tmp only;
    SPA, /api, /assets, /docs, /redoc, etc. return 404.
    Add before CORS in code so CORS still wraps the response.
    """

    async def dispatch(self, request: Request, call_next):
        hosts = _api_only_hostnames()
        if not hosts:
            return await call_next(request)
        h = _incoming_hostname(request)
        if h not in hosts:
            return await call_next(request)
        path = request.url.path
        if _path_allowed_on_api_only_host(path):
            return await call_next(request)
        return JSONResponse({"detail": "Not Found"}, status_code=404)


class PerformanceMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            route_obj = request.scope.get("route")
            route = getattr(route_obj, "path", None) or request.url.path
            try:
                request_bytes = max(0, int(request.headers.get("content-length", "0") or 0))
            except (TypeError, ValueError):
                request_bytes = 0
            record_endpoint_duration(
                request.method,
                route,
                getattr(response, "status_code", 500),
                time.perf_counter() - started,
                request_bytes,
            )


class MaintenanceMiddleware(BaseHTTPMiddleware):
    """Block new submissions and admin mutations while Redis maintenance is active."""

    _ALLOWED_MUTATION_PREFIXES = (
        "/api/admin/maintenance",
        "/api/admin/backups/google-drive",
    )
    _ALLOWED_MUTATION_PATHS = {
        "/api/admin/login",
        "/api/admin/logout",
        "/api/login",
        "/api/logout",
    }

    async def dispatch(self, request: Request, call_next):
        method = request.method.upper()
        path = request.url.path.rstrip("/") or "/"
        redis_runtime = request.app.state.container.redis_runtime
        if (
            redis_runtime.maintenance_active
            and method not in {"GET", "HEAD", "OPTIONS"}
            and path not in self._ALLOWED_MUTATION_PATHS
            and not path.startswith(self._ALLOWED_MUTATION_PREFIXES)
        ):
            return JSONResponse(
                {"detail": "maintenance"},
                status_code=503,
                headers={"Retry-After": "5"},
            )
        return await call_next(request)


def _storage_recovery_diagnostic(stats: dict) -> str:
    return (
        "sub2gen startup blocked: storage I/O remains unavailable after cache recovery "
        f"(free={int(stats.get('free_after', 0))} bytes, "
        f"reclaimed={int(stats.get('reclaimed_bytes', 0))} bytes, "
        f"target={int(stats.get('target_free', 0))} bytes)."
    )


EMERGENCY_PRUNE_TABLES = (
    "request_logs",
    "tasks",
    "cache_files",
    "geminigen_tasks",
    "runway_tasks",
    "api_key_audit_logs",
    "admin_sessions",
)
def _format_bytes(value: int) -> str:
    size = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TB"


def _directory_size(path: Path) -> int:
    total = 0
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    for root, dirnames, filenames in os.walk(path, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [name for name in dirnames if not (root_path / name).is_symlink()]
        for filename in filenames:
            file_path = root_path / filename
            try:
                if not file_path.is_symlink():
                    total += file_path.stat().st_size
            except OSError:
                continue
    return total


def _largest_files(path: Path, limit: int = 20):
    largest = []
    for root, dirnames, filenames in os.walk(path, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [name for name in dirnames if not (root_path / name).is_symlink()]
        for filename in filenames:
            file_path = root_path / filename
            try:
                if file_path.is_symlink():
                    continue
                size = file_path.stat().st_size
            except OSError:
                continue
            item = (size, str(file_path))
            if len(largest) < limit:
                heapq.heappush(largest, item)
            elif size > largest[0][0]:
                heapq.heapreplace(largest, item)
    return sorted(largest, reverse=True)


def _sqlite_literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _remove_sqlite_sidecars(db_path: Path) -> int:
    removed = 0
    candidates = [db_path.with_name(db_path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
    candidates.extend(db_path.parent.glob(f"{db_path.name}.upload-*"))
    for path in candidates:
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _emergency_prune_sqlite_history(database) -> dict:
    db_path = Path(getattr(database, "db_path", "") or "")
    if not db_path.is_file():
        return {"success": False, "reason": "database file not found"}

    old_size = db_path.stat().st_size
    source_path = Path(tempfile.gettempdir()) / f"sub2gen-source-{os.getpid()}.db"
    compact_path = Path(tempfile.gettempdir()) / f"sub2gen-compact-{os.getpid()}.db"
    for path in (source_path, compact_path):
        try:
            path.unlink()
        except OSError:
            pass

    shutil.copy2(db_path, source_path)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.is_file() and not sidecar.is_symlink():
            shutil.copy2(sidecar, source_path.with_name(source_path.name + suffix))

    deleted_rows = {}
    source_uri = f"{source_path.resolve().as_uri()}?mode=rw"
    conn = sqlite3.connect(source_uri, uri=True, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError as exc:
            print(f"WARN Emergency DB prune could not checkpoint WAL: {exc}")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")

        for table_name in EMERGENCY_PRUNE_TABLES:
            if not _sqlite_table_exists(conn, table_name):
                continue
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                conn.execute(f"DELETE FROM {table_name}")
                deleted_rows[table_name] = int(count or 0)
            except sqlite3.DatabaseError as exc:
                print(f"WARN Emergency DB prune skipped {table_name}: {exc}")
        conn.commit()

        conn.execute(f"VACUUM INTO {_sqlite_literal(compact_path)}")
    finally:
        conn.close()
        del conn
        gc.collect()

    with sqlite3.connect(f"{compact_path.resolve().as_uri()}?mode=ro", uri=True) as compact:
        check = compact.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise RuntimeError(f"Compacted SQLite quick_check failed: {check}")
    gc.collect()

    _remove_sqlite_sidecars(db_path)
    try:
        os.replace(compact_path, db_path)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        db_path.unlink()
        shutil.copy2(compact_path, db_path)
        compact_path.unlink()

    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            source_path.with_name(source_path.name + suffix).unlink()
        except OSError:
            pass

    new_size = db_path.stat().st_size
    return {
        "success": True,
        "old_size": int(old_size),
        "new_size": int(new_size),
        "reclaimed_bytes": int(max(0, old_size - new_size)),
        "deleted_rows": deleted_rows,
    }


def _try_emergency_prune_sqlite_history(database) -> dict:
    try:
        result = _emergency_prune_sqlite_history(database)
    except Exception as exc:
        print(f"WARN Emergency DB history prune failed: {exc}")
        return {"success": False, "reason": str(exc)}

    if result.get("success"):
        deleted = (
            ", ".join(f"{table}={count}" for table, count in sorted(result.get("deleted_rows", {}).items())) or "none"
        )
        print(
            "WARN Emergency DB history prune compacted SQLite "
            f"from {_format_bytes(result.get('old_size', 0))} "
            f"to {_format_bytes(result.get('new_size', 0))}; "
            f"reclaimed={_format_bytes(result.get('reclaimed_bytes', 0))}; "
            f"deleted_rows={deleted}"
        )
    else:
        print(f"WARN Emergency DB history prune skipped: {result.get('reason')}")
    return result


def _log_volume_usage_report(file_cache) -> None:
    cache_dir = getattr(file_cache, "cache_dir", None)
    root_value = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or (str(Path(cache_dir).parent) if cache_dir else "")
    if not root_value:
        return
    root = Path(root_value)
    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        print(f"WARN Unable to inspect runtime volume {root}: {exc}")
        return

    print(
        "WARN Runtime volume usage: "
        f"path={root}, total={_format_bytes(usage.total)}, "
        f"used={_format_bytes(usage.used)}, free={_format_bytes(usage.free)}"
    )
    try:
        children = list(root.iterdir())
    except OSError as exc:
        print(f"WARN Unable to list runtime volume {root}: {exc}")
        children = []
    if children:
        print("WARN Runtime volume top-level usage:")
        for child in sorted(children, key=_directory_size, reverse=True)[:20]:
            print(f"WARN   {_format_bytes(_directory_size(child))}\t{child}")

    largest = _largest_files(root, limit=20)
    if largest:
        print("WARN Runtime volume largest files:")
        for size, path in largest:
            print(f"WARN   {_format_bytes(size)}\t{path}")


async def _run_database_startup(database, config_dict: dict = None, is_first_startup: bool = None) -> None:
    await database.init_db()
    if is_first_startup is None:
        return

    if is_first_startup:
        print("First startup detected. Initializing database and configuration from setting.toml...")
        await database.init_config_from_toml(config_dict, is_first_startup=True)
        print("OK Database and configuration initialized successfully.")
    else:
        print("Existing database detected. Checking for missing tables and columns...")
        await database.check_and_migrate_db(config_dict)
        print("OK Database migration check completed.")


async def _init_database_with_storage_recovery(
    database,
    file_cache,
    config_dict: dict = None,
    is_first_startup: bool = None,
) -> None:
    """Run SQLite startup, evicting generated cache files on recoverable storage errors once."""
    await file_cache._cleanup_expired_files()
    try:
        await _run_database_startup(database, config_dict, is_first_startup)
        return
    except Exception as exc:
        if not is_sqlite_recoverable_storage_error(exc):
            raise
        first_error = exc

    _try_emergency_prune_sqlite_history(database)
    stats = await file_cache.reclaim_cache_space()
    if stats["free_after"] < stats["target_free"]:
        _log_volume_usage_report(file_cache)
        raise RuntimeError(_storage_recovery_diagnostic(stats)) from first_error

    print(
        "WARN SQLite startup storage error "
        f"({first_error}); generated cache recovery reclaimed "
        f"{stats['reclaimed_bytes']} bytes. Retrying database startup once."
    )
    try:
        await _run_database_startup(database, config_dict, is_first_startup)
    except Exception as exc:
        if is_sqlite_recoverable_storage_error(exc):
            _log_volume_usage_report(file_cache)
            raise RuntimeError(_storage_recovery_diagnostic(stats)) from exc
        raise


lifespan = build_lifespan(
    api_only_hostnames=_api_only_hostnames,
    init_database_with_storage_recovery=_init_database_with_storage_recovery,
    abort_refresh_batch_on_resource_exhaustion=_abort_refresh_batch_on_resource_exhaustion,
)

# Create FastAPI app
app = FastAPI(
    title="sub2gen", description="OpenAI-compatible API for Google VideoFX (Veo)", version="1.0.0", lifespan=lifespan
)
app.state.container = build_container()
app.add_exception_handler(sqlite3.OperationalError, sqlite_operational_error_handler)


async def database_unavailable_handler(request: Request, exc: DatabaseUnavailableError):
    return JSONResponse(
        {"detail": "database_unavailable"},
        status_code=503,
        headers={"Retry-After": "5"},
    )


app.add_exception_handler(DatabaseUnavailableError, database_unavailable_handler)

# CORS is added after this block so CORS is outer and still applies to 404s
app.add_middleware(ApiOnlyHostMiddleware)
app.add_middleware(PerformanceMetricsMiddleware)
app.add_middleware(MaintenanceMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(routes.router)
app.include_router(admin.router)


@app.get("/metrics")
async def metrics(request: Request):
    """Prometheus metrics endpoint for the main sub2gen service."""
    container: AppContainer = request.app.state.container
    payload = await render_main_metrics(
        container.db,
        concurrency_manager=container.concurrency_manager,
    )
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


# HTML routes for frontend
static_path = REPO_ROOT / "apps" / "api" / "static"
_SPA_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


async def _ensure_admin_spa_session(request: Request):
    token = admin.get_admin_token_from_cookie(request)
    if not await admin.is_admin_session_token_valid(token, request.app.state.container.db):
        return RedirectResponse(url="/login", status_code=302)
    return None


# Serve static assets (js, css, images from Vite build)
assets_path = static_path / "assets"
if assets_path.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_path)), name="assets")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str, request: Request):
    """Catch-all route to serve the React SPA"""
    # If the user tries to access the API directly via an undefined route, let it return 404 naturally
    # Or if it's an API route that somehow wasn't matched (though it should be matched earlier)
    if full_path.startswith("api/"):
        return HTMLResponse(content='{"detail": "Not Found"}', status_code=404)

    if full_path.strip("/") in {"manage", "test"}:
        guard_response = await _ensure_admin_spa_session(request)
        if guard_response is not None:
            return guard_response

    index_file = static_path / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), headers=_SPA_NO_CACHE_HEADERS)
    return HTMLResponse(
        content="<h1>sub2gen GUI</h1><p>Frontend not found. Please build the frontend first.</p>", status_code=404
    )
