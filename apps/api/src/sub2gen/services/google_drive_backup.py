"""Private Google Drive backups for the database and persistent browser profiles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import signal
import shutil
import sqlite3
import tarfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from ..core.config import config, get_runtime_data_dir, get_runtime_tmp_dir
from ..core.logger import debug_logger
from .browser_metrics_cleanup import cleanup_browser_metrics
from .browser_profile_service import BrowserProfileService
from .postgres_backup import (
    ENCRYPTED_BACKUP_FORMAT_VERSION,
    PostgresBackupError,
    create_postgres_archive,
    decrypt_and_extract_postgres_archive,
    load_backup_keys,
    restore_postgres_dump,
    verify_restored_row_counts,
)
from .redis_runtime import redis_runtime


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
OAUTH_SCOPES = "openid email https://www.googleapis.com/auth/drive.file"
BACKUP_FOLDER_NAME = "sub2gen Backups"
BACKUP_FORMAT_VERSION = 2
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
VOLATILE_PROFILE_DIRECTORIES = {
    "BrowserMetrics",
    "Cache",
    "Code Cache",
    "Crashpad",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GPUCache",
    "GrShaderCache",
    "GraphiteDawnCache",
    "Sessions",
    "ShaderCache",
    "component_crx_cache",
    "extensions_crx_cache",
}


class GoogleDriveBackupError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _schedule_restore_restart() -> bool:
    enabled = str(
        os.environ.get("SUB2GEN_AUTO_RESTART_AFTER_RESTORE", "true") or "true"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if enabled:
        asyncio.get_running_loop().call_later(2.0, os.kill, os.getpid(), signal.SIGTERM)
    return enabled


def _atomic_json(path: Path, payload: dict[str, Any], *, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    if mode is not None:
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
    os.replace(temporary, path)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(default)
    except (OSError, ValueError, TypeError):
        return dict(default)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _database_snapshot(source_path: Path, destination: Path) -> None:
    destination.unlink(missing_ok=True)
    with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as source:
        with sqlite3.connect(destination) as target:
            source.backup(target)
    with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as check:
        result = check.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise GoogleDriveBackupError("Database snapshot integrity check failed")


def _excluded_profile_path(relative: Path) -> bool:
    names = set(relative.parts)
    name = relative.name
    return (
        bool(VOLATILE_PROFILE_DIRECTORIES.intersection(names))
        or name.startswith("Singleton")
        or name.endswith(".part")
    )


def _build_archive(
    database_path: Path,
    profiles_root: Path,
    working_dir: Path,
    archive_path: Path,
    *,
    backup_id: str,
    backup_type: str,
    app_version: str,
) -> dict[str, Any]:
    snapshot_path = working_dir / "sub2gen.db"
    _database_snapshot(database_path, snapshot_path)
    entries: list[dict[str, Any]] = []

    def register(source: Path, archive_name: str) -> None:
        stat = source.stat()
        entries.append(
            {
                "path": archive_name,
                "size": stat.st_size,
                "sha256": _sha256_file(source),
            }
        )

    register(snapshot_path, "database/sub2gen.db")
    profile_files: list[tuple[Path, str]] = []
    if profiles_root.is_dir():
        for source in profiles_root.rglob("*"):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(profiles_root)
            if _excluded_profile_path(relative):
                continue
            archive_name = str(PurePosixPath("browser_profiles", *relative.parts))
            register(source, archive_name)
            profile_files.append((source, archive_name))

    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "backup_id": backup_id,
        "backup_type": backup_type,
        "created_at": _iso_now(),
        "application_version": app_version,
        "files": entries,
    }
    manifest_path = working_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    with tarfile.open(archive_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.add(manifest_path, arcname="manifest.json", recursive=False)
        archive.add(snapshot_path, arcname="database/sub2gen.db", recursive=False)
        for source, archive_name in profile_files:
            archive.add(source, arcname=archive_name, recursive=False)
    manifest["archive_size"] = archive_path.stat().st_size
    manifest["archive_sha256"] = _sha256_file(archive_path)
    return manifest


def _validate_and_extract_archive(archive_path: Path, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or not (member.isfile() or member.isdir())
            ):
                raise GoogleDriveBackupError("Backup archive contains an unsafe path")
            target = (destination / Path(*pure.parts)).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise GoogleDriveBackupError("Backup archive escapes the restore directory")
        for member in members:
            target = destination / Path(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise GoogleDriveBackupError("Backup archive contains an unreadable file")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)

    manifest_path = destination / "manifest.json"
    manifest = _read_json(manifest_path, {})
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise GoogleDriveBackupError("Unsupported backup format")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise GoogleDriveBackupError("Backup manifest is missing file metadata")
    for entry in entries:
        if not isinstance(entry, dict):
            raise GoogleDriveBackupError("Backup manifest is invalid")
        relative = PurePosixPath(str(entry.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise GoogleDriveBackupError("Backup manifest contains an unsafe path")
        path = destination / Path(*relative.parts)
        if not path.is_file() or path.stat().st_size != int(entry.get("size", -1)):
            raise GoogleDriveBackupError("Backup file size validation failed")
        if _sha256_file(path) != str(entry.get("sha256") or ""):
            raise GoogleDriveBackupError("Backup checksum validation failed")

    database_path = destination / "database" / "sub2gen.db"
    if not database_path.is_file():
        raise GoogleDriveBackupError("Backup does not contain sub2gen.db")
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    if not result or result[0] != "ok" or not {"admin_config", "tokens"}.issubset(tables):
        raise GoogleDriveBackupError("Backup database validation failed")
    return manifest


class GoogleDriveBackupService:
    def __init__(self, database: Any, *, app_version: str = "1.0.0"):
        self.database = database
        self.app_version = app_version
        self.data_dir = get_runtime_data_dir().resolve()
        self.tmp_root = (get_runtime_tmp_dir() / "google-drive-backups").resolve()
        self.profiles_root = (self.data_dir / "browser_profiles").resolve()
        self.config_path = self.data_dir / "google_drive_backup_config.json"
        self.credentials_path = self.data_dir / "google_drive_oauth.json"
        self._config = _read_json(self.config_path, self._default_config())
        self._oauth_states: dict[str, dict[str, Any]] = {}
        self._job: dict[str, Any] = {}
        self._job_task: Optional[asyncio.Task] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _default_config() -> dict[str, Any]:
        return {
            "enabled": False,
            "schedule_time": "03:00",
            "timezone": "Asia/Karachi",
            "retention": 14,
            "folder_id": "",
            "last_backup_at": None,
            "last_backup_status": None,
            "last_backup_error": None,
            "last_automatic_date": None,
            "last_rollback_cleanup_date": None,
        }

    @property
    def oauth_configured(self) -> bool:
        return all(
            os.environ.get(name, "").strip()
            for name in (
                "SUB2GEN_GOOGLE_DRIVE_CLIENT_ID",
                "SUB2GEN_GOOGLE_DRIVE_CLIENT_SECRET",
                "SUB2GEN_GOOGLE_DRIVE_REDIRECT_URI",
            )
        )

    def _oauth_env(self) -> tuple[str, str, str]:
        client_id = os.environ.get("SUB2GEN_GOOGLE_DRIVE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("SUB2GEN_GOOGLE_DRIVE_CLIENT_SECRET", "").strip()
        redirect_uri = os.environ.get("SUB2GEN_GOOGLE_DRIVE_REDIRECT_URI", "").strip()
        if not client_id or not client_secret or not redirect_uri:
            raise GoogleDriveBackupError("Google Drive OAuth environment variables are incomplete")
        return client_id, client_secret, redirect_uri

    def _credentials(self) -> dict[str, Any]:
        return _read_json(self.credentials_path, {})

    def _save_credentials(self, credentials: dict[str, Any]) -> None:
        safe = {
            key: credentials.get(key)
            for key in ("access_token", "refresh_token", "expires_at", "account_email")
            if credentials.get(key) is not None
        }
        _atomic_json(self.credentials_path, safe, mode=0o600)

    def public_status(self) -> dict[str, Any]:
        credentials = self._credentials()
        job = self._sanitize_job(self._job)
        return {
            "database_backend": getattr(self.database, "backend", "sqlite"),
            "database_revision": getattr(self.database, "database_revision", None),
            "encryption_configured": self._encryption_configured(),
            "encryption_key_id": str(os.environ.get("SUB2GEN_BACKUP_ACTIVE_KEY_ID", "") or "") or None,
            "oauth_configured": self.oauth_configured,
            "connected": bool(credentials.get("refresh_token")),
            "account_email": credentials.get("account_email") or None,
            "enabled": bool(self._config.get("enabled")),
            "schedule_time": str(self._config.get("schedule_time") or "03:00"),
            "timezone": str(self._config.get("timezone") or "Asia/Karachi"),
            "retention": int(self._config.get("retention") or 14),
            "folder_configured": bool(self._config.get("folder_id")),
            "last_backup_at": self._config.get("last_backup_at"),
            "last_backup_status": self._config.get("last_backup_status"),
            "last_backup_error": self._config.get("last_backup_error"),
            "job": job or None,
        }

    def _encryption_configured(self) -> bool:
        if getattr(self.database, "backend", "sqlite") != "postgres":
            return False
        try:
            load_backup_keys()
            return True
        except PostgresBackupError:
            return False

    @staticmethod
    def _sanitize_job(job: dict[str, Any]) -> dict[str, Any]:
        if not job:
            return {}
        allowed = {
            "id", "kind", "status", "stage", "started_at", "finished_at",
            "bytes_total", "bytes_transferred", "error", "remote_file_id",
            "remote_name", "restart_required",
        }
        return {key: value for key, value in job.items() if key in allowed}

    def update_config(self, *, enabled: bool, schedule_time: str, timezone_name: str, retention: int) -> dict[str, Any]:
        try:
            hour_text, minute_text = schedule_time.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError as exc:
            raise GoogleDriveBackupError("Schedule time must use HH:MM") from exc
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise GoogleDriveBackupError("Unknown backup timezone") from exc
        if retention < 1 or retention > 365:
            raise GoogleDriveBackupError("Retention must be between 1 and 365")
        if enabled and not self._credentials().get("refresh_token"):
            raise GoogleDriveBackupError("Connect Google Drive before enabling automatic backups")
        self._config.update(
            enabled=bool(enabled),
            schedule_time=f"{hour:02d}:{minute:02d}",
            timezone=timezone_name,
            retention=int(retention),
        )
        _atomic_json(self.config_path, self._config)
        return self.public_status()

    def begin_oauth(self, admin_session: str) -> str:
        client_id, _client_secret, redirect_uri = self._oauth_env()
        state = secrets.token_urlsafe(32)
        self._oauth_states[state] = {
            "expires_at": time.time() + 600,
            "session_hash": hashlib.sha256(admin_session.encode("utf-8")).hexdigest(),
        }
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": OAUTH_SCOPES,
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
            }
        )
        return f"{GOOGLE_AUTH_URL}?{query}"

    async def finish_oauth(self, *, state: str, code: str, admin_session: str) -> dict[str, Any]:
        pending = self._oauth_states.pop(state, None)
        session_hash = hashlib.sha256(admin_session.encode("utf-8")).hexdigest()
        if not pending or pending.get("expires_at", 0) < time.time() or not secrets.compare_digest(
            str(pending.get("session_hash") or ""), session_hash
        ):
            raise GoogleDriveBackupError("OAuth state is invalid or expired")
        client_id, client_secret, redirect_uri = self._oauth_env()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        if response.status_code >= 400:
            raise GoogleDriveBackupError("Google rejected the OAuth authorization code")
        payload = response.json()
        if not payload.get("refresh_token"):
            raise GoogleDriveBackupError("Google did not return an offline refresh token")
        credentials = {
            "access_token": payload.get("access_token"),
            "refresh_token": payload.get("refresh_token"),
            "expires_at": time.time() + int(payload.get("expires_in") or 3600) - 60,
        }
        self._save_credentials(credentials)
        about = await self._drive_json("GET", "/about", params={"fields": "user(emailAddress)"})
        credentials["account_email"] = ((about.get("user") or {}).get("emailAddress") or "")
        self._save_credentials(credentials)
        await self._ensure_folder()
        return self.public_status()

    async def disconnect(self) -> None:
        credentials = self._credentials()
        token = credentials.get("refresh_token") or credentials.get("access_token")
        if token:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.post("https://oauth2.googleapis.com/revoke", params={"token": token})
            except Exception:
                pass
        self.credentials_path.unlink(missing_ok=True)
        self._config["enabled"] = False
        _atomic_json(self.config_path, self._config)

    async def _access_token(self) -> str:
        credentials = self._credentials()
        if credentials.get("access_token") and float(credentials.get("expires_at") or 0) > time.time():
            return str(credentials["access_token"])
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            raise GoogleDriveBackupError("Google Drive is not connected")
        client_id, client_secret, _redirect_uri = self._oauth_env()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if response.status_code >= 400:
            raise GoogleDriveBackupError("Google Drive authorization refresh failed")
        payload = response.json()
        credentials.update(
            access_token=payload.get("access_token"),
            expires_at=time.time() + int(payload.get("expires_in") or 3600) - 60,
        )
        self._save_credentials(credentials)
        return str(credentials["access_token"])

    async def _drive_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        token = await self._access_token()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(method, f"{DRIVE_API}{path}", headers=headers, **kwargs)
        if response.status_code >= 400:
            raise GoogleDriveBackupError(f"Google Drive request failed with HTTP {response.status_code}")
        if not response.content:
            return {}
        return response.json()

    async def _ensure_folder(self) -> str:
        folder_id = str(self._config.get("folder_id") or "")
        if folder_id:
            try:
                await self._drive_json("GET", f"/files/{folder_id}", params={"fields": "id,trashed"})
                return folder_id
            except GoogleDriveBackupError:
                folder_id = ""
        query = "trashed=false and mimeType='application/vnd.google-apps.folder' and appProperties has { key='sub2genFolder' and value='backups' }"
        listed = await self._drive_json(
            "GET", "/files", params={"q": query, "spaces": "drive", "fields": "files(id,name)", "pageSize": 10}
        )
        files = listed.get("files") or []
        if files:
            folder_id = str(files[0]["id"])
        else:
            created = await self._drive_json(
                "POST",
                "/files",
                params={"fields": "id"},
                json={
                    "name": BACKUP_FOLDER_NAME,
                    "mimeType": "application/vnd.google-apps.folder",
                    "appProperties": {"sub2genFolder": "backups"},
                },
            )
            folder_id = str(created["id"])
        self._config["folder_id"] = folder_id
        _atomic_json(self.config_path, self._config)
        return folder_id

    async def test_connection(self) -> dict[str, Any]:
        folder_id = await self._ensure_folder()
        about = await self._drive_json("GET", "/about", params={"fields": "user(emailAddress),storageQuota"})
        return {
            "success": True,
            "folder_configured": bool(folder_id),
            "account_email": ((about.get("user") or {}).get("emailAddress") or None),
            "storage_quota": about.get("storageQuota") or {},
        }

    async def start(self) -> None:
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None

    def _next_scheduled_time(self, now: Optional[datetime] = None) -> datetime:
        zone = ZoneInfo(str(self._config.get("timezone") or "Asia/Karachi"))
        local_now = (now or _utc_now()).astimezone(zone)
        hour, minute = (int(part) for part in str(self._config.get("schedule_time") or "03:00").split(":"))
        target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                cleanup_date = _utc_now().date().isoformat()
                if (
                    self._credentials().get("refresh_token")
                    and self._config.get("last_rollback_cleanup_date") != cleanup_date
                ):
                    try:
                        await self._cleanup_expired_rollback_backups()
                    except Exception as exc:
                        debug_logger.log_warning(
                            f"[GoogleDriveBackup] rollback cleanup failed: {type(exc).__name__}"
                        )
                    else:
                        self._config["last_rollback_cleanup_date"] = cleanup_date
                        _atomic_json(self.config_path, self._config)
                if not self._config.get("enabled"):
                    continue
                zone = ZoneInfo(str(self._config.get("timezone") or "Asia/Karachi"))
                local_now = _utc_now().astimezone(zone)
                hour, minute = (
                    int(part)
                    for part in str(self._config.get("schedule_time") or "03:00").split(":")
                )
                scheduled = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                local_date = local_now.date().isoformat()
                if local_now < scheduled or self._config.get("last_automatic_date") == local_date:
                    continue
                try:
                    await self.start_backup("automatic")
                except GoogleDriveBackupError:
                    pass
                else:
                    self._config["last_automatic_date"] = local_date
                    _atomic_json(self.config_path, self._config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                debug_logger.log_warning(f"[GoogleDriveBackup] scheduler error: {type(exc).__name__}")
                await asyncio.sleep(60)

    async def start_backup(self, backup_type: str = "manual") -> dict[str, Any]:
        async with self._lock:
            if self._job_task is not None and not self._job_task.done():
                raise GoogleDriveBackupError("A Google Drive backup or restore job is already running")
            if not self._credentials().get("refresh_token"):
                raise GoogleDriveBackupError("Google Drive is not connected")
            if getattr(self.database, "backend", "sqlite") == "postgres" and not self._encryption_configured():
                raise GoogleDriveBackupError("Configure PostgreSQL backup encryption keys first")
            job_id = uuid.uuid4().hex
            self._job = {
                "id": job_id,
                "kind": "backup",
                "status": "running",
                "stage": "starting",
                "started_at": _iso_now(),
                "bytes_total": 0,
                "bytes_transferred": 0,
                "error": None,
            }
            self._job_task = asyncio.create_task(self._run_backup(job_id, backup_type))
            return self._sanitize_job(self._job)

    async def _run_backup(self, job_id: str, backup_type: str) -> None:
        try:
            await self._perform_backup(job_id, backup_type)
            self._job.update(status="completed", stage="complete", finished_at=_iso_now())
            self._config.update(last_backup_at=_iso_now(), last_backup_status="completed", last_backup_error=None)
        except asyncio.CancelledError:
            self._job.update(status="cancelled", stage="cancelled", finished_at=_iso_now())
            raise
        except Exception as exc:
            self._job.update(
                status="failed", stage="failed", finished_at=_iso_now(), error=str(exc)[:300]
            )
            self._config.update(last_backup_status="failed", last_backup_error=str(exc)[:300])
            debug_logger.log_warning(f"[GoogleDriveBackup] backup failed: {type(exc).__name__}")
        finally:
            _atomic_json(self.config_path, self._config)

    async def _perform_backup(self, job_id: str, backup_type: str) -> dict[str, Any]:
        working_dir = self.tmp_root / f"job-{job_id}"
        is_postgres = getattr(self.database, "backend", "sqlite") == "postgres"
        extension = ".s2g" if is_postgres else ".tar.gz"
        archive_path = working_dir / f"sub2gen-{backup_type}-{_utc_now().strftime('%Y%m%dT%H%M%SZ')}{extension}"
        shutil.rmtree(working_dir, ignore_errors=True)
        working_dir.mkdir(parents=True, exist_ok=False)
        try:
            self._job["stage"] = "closing_profiles"
            profile_service = BrowserProfileService.get_existing_instance()
            if profile_service is not None:
                await profile_service.close_all()
            await asyncio.to_thread(cleanup_browser_metrics)
            self._job["stage"] = "creating_archive"
            if is_postgres:
                manifest = await create_postgres_archive(
                    self.database,
                    self.profiles_root,
                    working_dir,
                    archive_path,
                    backup_id=job_id,
                    backup_type=backup_type,
                    app_version=self.app_version,
                )
            else:
                manifest = await asyncio.to_thread(
                    _build_archive,
                    Path(self.database.db_path),
                    self.profiles_root,
                    working_dir,
                    archive_path,
                    backup_id=job_id,
                    backup_type=backup_type,
                    app_version=self.app_version,
                )
            self._job["bytes_total"] = archive_path.stat().st_size
            self._job["stage"] = "uploading"
            remote = await self._upload_archive(archive_path, manifest, backup_type)
            self._job.update(remote_file_id=remote.get("id"), remote_name=remote.get("name"))
            if backup_type == "automatic":
                self._job["stage"] = "applying_retention"
                await self._apply_retention()
            return remote
        finally:
            await asyncio.to_thread(shutil.rmtree, working_dir, True)

    async def _upload_archive(self, archive_path: Path, manifest: dict[str, Any], backup_type: str) -> dict[str, Any]:
        folder_id = await self._ensure_folder()
        token = await self._access_token()
        metadata = {
            "name": archive_path.name,
            "parents": [folder_id],
            "mimeType": "application/octet-stream" if manifest.get("database_backend") == "postgres" else "application/gzip",
            "appProperties": {
                "sub2genBackup": "true",
                "backupType": backup_type,
                "formatVersion": str(manifest.get("format_version") or BACKUP_FORMAT_VERSION),
                "backupId": str(manifest["backup_id"]),
                "archiveSha256": str(
                    manifest.get("encrypted_archive_sha256") or manifest.get("archive_sha256") or ""
                ),
                "databaseBackend": str(manifest.get("database_backend") or "sqlite"),
                "databaseRevision": str(manifest.get("database_revision") or ""),
                "encryptionKeyId": str(manifest.get("encryption_key_id") or ""),
                "backupFormat": str(manifest.get("dump_format") or "sqlite-snapshot"),
                **(
                    {"rollbackExpiresAt": (_utc_now() + timedelta(days=7)).isoformat()}
                    if backup_type.startswith(("pre-change", "pre_change", "pre-restore", "pre_restore"))
                    else {}
                ),
            },
        }
        total = archive_path.stat().st_size
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Upload-Content-Type": metadata["mimeType"],
            "X-Upload-Content-Length": str(total),
            "Content-Type": "application/json; charset=UTF-8",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            session = await client.post(
                f"{DRIVE_UPLOAD_API}/files",
                params={"uploadType": "resumable", "fields": "id,name,size,createdTime,appProperties"},
                headers=headers,
                json=metadata,
            )
            if session.status_code >= 400 or not session.headers.get("Location"):
                raise GoogleDriveBackupError("Google Drive refused the resumable upload")
            upload_url = session.headers["Location"]
            sent = 0
            with archive_path.open("rb") as handle:
                while sent < total:
                    chunk = await asyncio.to_thread(handle.read, min(UPLOAD_CHUNK_BYTES, total - sent))
                    end = sent + len(chunk) - 1
                    response = await client.put(
                        upload_url,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {sent}-{end}/{total}",
                        },
                        content=chunk,
                        timeout=120,
                    )
                    if response.status_code not in (200, 201, 308):
                        raise GoogleDriveBackupError(f"Google Drive upload failed with HTTP {response.status_code}")
                    sent += len(chunk)
                    self._job["bytes_transferred"] = sent
            if response.status_code not in (200, 201):
                raise GoogleDriveBackupError("Google Drive upload did not complete")
            return response.json()

    async def list_backups(self) -> list[dict[str, Any]]:
        folder_id = await self._ensure_folder()
        query = f"'{folder_id}' in parents and trashed=false and appProperties has {{ key='sub2genBackup' and value='true' }}"
        payload = await self._drive_json(
            "GET",
            "/files",
            params={
                "q": query,
                "spaces": "drive",
                "orderBy": "createdTime desc",
                "pageSize": 100,
                "fields": "files(id,name,size,createdTime,modifiedTime,appProperties)",
            },
        )
        return [self._sanitize_remote(item) for item in payload.get("files") or []]

    @staticmethod
    def _sanitize_remote(item: dict[str, Any]) -> dict[str, Any]:
        properties = item.get("appProperties") or {}
        return {
            "id": item.get("id"),
            "name": item.get("name"),
            "size": int(item.get("size") or 0),
            "created_at": item.get("createdTime"),
            "modified_at": item.get("modifiedTime"),
            "backup_type": properties.get("backupType"),
            "backup_id": properties.get("backupId"),
            "sha256": properties.get("archiveSha256"),
            "rollback_expires_at": properties.get("rollbackExpiresAt"),
            "database_backend": properties.get("databaseBackend") or "sqlite",
            "database_revision": properties.get("databaseRevision") or None,
            "encryption_key_id": properties.get("encryptionKeyId") or None,
            "backup_format": properties.get("backupFormat") or "sqlite-snapshot",
            "format_version": int(properties.get("formatVersion") or BACKUP_FORMAT_VERSION),
        }

    async def delete_backup(self, file_id: str) -> None:
        known = {str(item.get("id")) for item in await self.list_backups()}
        if file_id not in known:
            raise GoogleDriveBackupError("Backup file was not found in the private backup folder")
        await self._drive_json("DELETE", f"/files/{file_id}")

    async def _apply_retention(self) -> None:
        automatic = [item for item in await self.list_backups() if item.get("backup_type") == "automatic"]
        retention = int(self._config.get("retention") or 14)
        for item in automatic[retention:]:
            await self._drive_json("DELETE", f"/files/{item['id']}")

    async def _cleanup_expired_rollback_backups(self) -> int:
        now = _utc_now()
        deleted = 0
        for item in await self.list_backups():
            expires_raw = str(item.get("rollback_expires_at") or "").strip()
            if not expires_raw:
                continue
            try:
                expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if expires_at <= now:
                await self._drive_json("DELETE", f"/files/{item['id']}")
                deleted += 1
        return deleted

    async def _download_backup(self, file_id: str, destination: Path) -> dict[str, Any]:
        backups = {str(item.get("id")): item for item in await self.list_backups()}
        metadata = backups.get(file_id)
        if not metadata:
            raise GoogleDriveBackupError("Backup file was not found in the private backup folder")
        expected_size = int(metadata.get("size") or 0)
        usage = shutil.disk_usage(self.tmp_root.parent)
        if expected_size and usage.free < expected_size * 3:
            raise GoogleDriveBackupError("Not enough free disk space to stage this restore safely")
        token = await self._access_token()
        self._job["bytes_total"] = expected_size
        downloaded = 0
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET",
                f"{DRIVE_API}/files/{file_id}",
                params={"alt": "media"},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status_code >= 400:
                    raise GoogleDriveBackupError(f"Google Drive download failed with HTTP {response.status_code}")
                with destination.open("wb") as handle:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        handle.write(chunk)
                        downloaded += len(chunk)
                        self._job["bytes_transferred"] = downloaded
        if expected_size and downloaded != expected_size:
            raise GoogleDriveBackupError("Downloaded backup size does not match Drive metadata")
        expected_sha = str(metadata.get("sha256") or "")
        if expected_sha and await asyncio.to_thread(_sha256_file, destination) != expected_sha:
            raise GoogleDriveBackupError("Downloaded backup checksum does not match Drive metadata")
        return metadata

    async def start_restore(self, file_id: str) -> dict[str, Any]:
        async with self._lock:
            if self._job_task is not None and not self._job_task.done():
                raise GoogleDriveBackupError("A Google Drive backup or restore job is already running")
            job_id = uuid.uuid4().hex
            self._job = {
                "id": job_id,
                "kind": "restore",
                "status": "running",
                "stage": "safety_backup",
                "started_at": _iso_now(),
                "bytes_total": 0,
                "bytes_transferred": 0,
                "error": None,
                "restart_required": False,
            }
            self._job_task = asyncio.create_task(self._run_restore(job_id, file_id))
            return self._sanitize_job(self._job)

    async def _run_restore(self, job_id: str, file_id: str) -> None:
        if getattr(self.database, "backend", "sqlite") == "postgres":
            await self._run_postgres_restore(job_id, file_id)
            return
        working_dir = self.tmp_root / f"restore-{job_id}"
        archive_path = working_dir / "restore.tar.gz"
        extracted = working_dir / "extracted"
        rollback_db = Path(self.database.db_path).with_name(f"sub2gen.db.pre-restore-{job_id}")
        rollback_profiles = self.profiles_root.with_name(f"browser_profiles.pre-restore-{job_id}")
        try:
            working_dir.mkdir(parents=True, exist_ok=False)
            await self._perform_backup(f"pre-restore-{job_id}", "pre_restore")
            self._job.update(stage="downloading", bytes_total=0, bytes_transferred=0)
            await self._download_backup(file_id, archive_path)
            self._job["stage"] = "validating"
            await asyncio.to_thread(_validate_and_extract_archive, archive_path, extracted)
            self._job["stage"] = "applying"
            profile_service = BrowserProfileService.get_existing_instance()
            if profile_service is not None:
                await profile_service.close_all()
            db_path = Path(self.database.db_path)
            staged_db = extracted / "database" / "sub2gen.db"
            staged_profiles = extracted / "browser_profiles"
            try:
                async with self.database._write_lock:
                    for suffix in ("-wal", "-shm", "-journal"):
                        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
                    if db_path.exists():
                        os.replace(db_path, rollback_db)
                    os.replace(staged_db, db_path)
                    if self.profiles_root.exists():
                        os.replace(self.profiles_root, rollback_profiles)
                    if staged_profiles.exists():
                        os.replace(staged_profiles, self.profiles_root)
                    else:
                        self.profiles_root.mkdir(parents=True, exist_ok=True)
                await self.database.init_db()
                await self.database.check_and_migrate_db(config.get_raw_config())
                await self.database.reload_config_to_memory()
            except Exception:
                async with self.database._write_lock:
                    db_path.unlink(missing_ok=True)
                    if rollback_db.exists():
                        os.replace(rollback_db, db_path)
                    shutil.rmtree(self.profiles_root, ignore_errors=True)
                    if rollback_profiles.exists():
                        os.replace(rollback_profiles, self.profiles_root)
                raise
            rollback_db.unlink(missing_ok=True)
            await asyncio.to_thread(shutil.rmtree, rollback_profiles, True)
            self._job.update(
                status="completed",
                stage="complete",
                finished_at=_iso_now(),
                restart_required=True,
            )
        except Exception as exc:
            self._job.update(
                status="failed", stage="failed", finished_at=_iso_now(), error=str(exc)[:300]
            )
            debug_logger.log_warning(f"[GoogleDriveBackup] restore failed: {type(exc).__name__}")
        finally:
            await asyncio.to_thread(shutil.rmtree, working_dir, True)

    async def _apply_restored_profiles(self, extracted: Path, rollback_profiles: Path) -> None:
        staged_profiles = extracted / "browser_profiles"
        rollback_profiles.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.rmtree, rollback_profiles, True)
        if self.profiles_root.exists():
            os.replace(self.profiles_root, rollback_profiles)
        try:
            if staged_profiles.exists():
                os.replace(staged_profiles, self.profiles_root)
            else:
                self.profiles_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            await asyncio.to_thread(shutil.rmtree, self.profiles_root, True)
            if rollback_profiles.exists():
                os.replace(rollback_profiles, self.profiles_root)
            raise

    async def _wait_for_restore_drain(self, timeout_seconds: int = 300) -> dict[str, int]:
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        remaining = {"redis": 0, "runway": 0, "geminigen": 0}
        while True:
            redis_total = 0
            async for key in redis_runtime.client.scan_iter(match="sub2gen:inflight:*"):
                try:
                    redis_total += max(0, int(await redis_runtime.client.get(key) or 0))
                except (TypeError, ValueError):
                    continue
            async with self.database._connect() as connection:
                runway_cursor = await connection.execute(
                    "SELECT COALESCE(SUM(in_flight), 0) AS count FROM runway_accounts"
                )
                gemini_cursor = await connection.execute(
                    """
                    SELECT COALESCE(SUM(image_in_flight + video_in_flight), 0) AS count
                    FROM geminigen_accounts
                    """
                )
                runway_total = int((await runway_cursor.fetchone())["count"] or 0)
                gemini_total = int((await gemini_cursor.fetchone())["count"] or 0)
            remaining = {
                "redis": redis_total,
                "runway": runway_total,
                "geminigen": gemini_total,
            }
            if not any(remaining.values()):
                return remaining
            if time.monotonic() >= deadline:
                raise GoogleDriveBackupError(
                    f"Active work did not drain before restore: {remaining}"
                )
            await asyncio.sleep(1)

    async def _restore_downloaded_postgres_archive(
        self,
        encrypted_path: Path,
        working_dir: Path,
        rollback_profiles: Path,
        *,
        on_database_restore_start: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        working_dir.mkdir(parents=True, exist_ok=True)
        manifest, extracted = await decrypt_and_extract_postgres_archive(encrypted_path, working_dir)
        self._job["stage"] = "restoring_database"
        if on_database_restore_start is not None:
            on_database_restore_start()
        await restore_postgres_dump(self.database, extracted / "database" / "sub2gen.dump")
        await verify_restored_row_counts(self.database, manifest.get("row_counts") or {})
        self._job["stage"] = "restoring_profiles"
        await self._apply_restored_profiles(extracted, rollback_profiles)
        await self.database.reload_config_to_memory()
        return manifest

    async def _run_postgres_restore(self, job_id: str, file_id: str) -> None:
        working_dir = self.tmp_root / f"restore-{job_id}"
        archive_path = working_dir / "restore.s2g"
        rollback_profiles = self.profiles_root.with_name(f"browser_profiles.pre-restore-{job_id}")
        pre_restore_remote: dict[str, Any] | None = None
        maintenance_set = False
        rollback_succeeded = False
        restore_started = False
        try:
            working_dir.mkdir(parents=True, exist_ok=False)
            known_backups = {str(item.get("id")): item for item in await self.list_backups()}
            selected_metadata = known_backups.get(file_id)
            if not selected_metadata:
                raise GoogleDriveBackupError("Backup file was not found in the private backup folder")
            if selected_metadata.get("database_backend") != "postgres":
                raise GoogleDriveBackupError(
                    "SQLite rollback artifacts cannot be restored into PostgreSQL"
                )
            if not redis_runtime.ready:
                raise GoogleDriveBackupError("Redis is required for PostgreSQL restore maintenance")
            await redis_runtime.set_maintenance(
                True,
                reason="postgres_restore",
                owner="google_drive_restore",
            )
            maintenance_set = True
            self._job["stage"] = "draining"
            await self._wait_for_restore_drain(300)
            self._job["stage"] = "safety_backup"
            pre_restore_remote = await self._perform_backup(
                f"pre-restore-{job_id}", "pre_restore"
            )
            self._job.update(stage="downloading", bytes_total=0, bytes_transferred=0)
            await self._download_backup(file_id, archive_path)
            self._job["stage"] = "validating"
            profile_service = BrowserProfileService.get_existing_instance()
            if profile_service is not None:
                await profile_service.close_all()

            def mark_restore_started() -> None:
                nonlocal restore_started
                restore_started = True

            manifest = await self._restore_downloaded_postgres_archive(
                archive_path,
                working_dir / "selected",
                rollback_profiles,
                on_database_restore_start=mark_restore_started,
            )
            async with self.database._connect(write=True) as connection:
                await connection.execute(
                    """
                    INSERT INTO system_metadata (key, value, updated_at)
                    VALUES ('last_restore_at', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                    """,
                    (_iso_now(),),
                )
                await connection.execute(
                    """
                    INSERT INTO system_metadata (key, value, updated_at)
                    VALUES ('last_restore_backup_id', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                    """,
                    (str(manifest.get("backup_id") or file_id),),
                )
                await connection.commit()
            health = await self.database.health_snapshot()
            if not health.get("database_ready"):
                raise GoogleDriveBackupError("PostgreSQL did not become ready after restore")
            restore_status = {
                "status": "restart_pending",
                "backup_id": str(manifest.get("backup_id") or file_id),
                "restored_at": _iso_now(),
            }
            await redis_runtime.client.set(
                "sub2gen:restore:status",
                json.dumps(restore_status, separators=(",", ":")),
            )
            await asyncio.to_thread(shutil.rmtree, rollback_profiles, True)
            self._job.update(
                status="completed",
                stage="restart_pending",
                finished_at=_iso_now(),
                restart_required=True,
            )
            _schedule_restore_restart()
        except Exception as exc:
            rollback_error: Exception | None = None
            if restore_started and pre_restore_remote and pre_restore_remote.get("id"):
                try:
                    recovery_dir = working_dir / "recovery"
                    recovery_dir.mkdir(parents=True, exist_ok=True)
                    recovery_archive = recovery_dir / "pre-restore.s2g"
                    await self._download_backup(str(pre_restore_remote["id"]), recovery_archive)
                    await self._restore_downloaded_postgres_archive(
                        recovery_archive,
                        recovery_dir / "unpacked",
                        rollback_profiles,
                    )
                    await asyncio.to_thread(shutil.rmtree, rollback_profiles, True)
                    rollback_succeeded = True
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
                    debug_logger.log_warning(
                        f"[GoogleDriveBackup] PostgreSQL restore rollback failed: {type(rollback_exc).__name__}"
                    )
            if rollback_succeeded:
                rollback_status = {
                    "status": "rollback_restart_pending",
                    "failed_backup_id": file_id,
                    "rolled_back_at": _iso_now(),
                    "error": str(exc)[:300],
                }
                try:
                    await redis_runtime.client.set(
                        "sub2gen:restore:status",
                        json.dumps(rollback_status, separators=(",", ":")),
                    )
                except Exception:
                    pass
                _schedule_restore_restart()
            elif restore_started:
                failure_status = {
                    "status": "rollback_failed",
                    "failed_backup_id": file_id,
                    "failed_at": _iso_now(),
                    "error": str(exc)[:300],
                    "rollback_error": str(rollback_error or "pre-restore backup unavailable")[:300],
                }
                try:
                    await redis_runtime.client.set(
                        "sub2gen:restore:status",
                        json.dumps(failure_status, separators=(",", ":")),
                        ex=7 * 24 * 3600,
                    )
                except Exception:
                    pass
            elif maintenance_set:
                try:
                    await redis_runtime.set_maintenance(False, reason="restore_failed")
                except Exception:
                    pass
            self._job.update(
                status="failed",
                stage=(
                    "rollback_restart_pending"
                    if rollback_succeeded
                    else "rollback_failed" if restore_started else "failed"
                ),
                finished_at=_iso_now(),
                error=str(exc)[:300],
                restart_required=rollback_succeeded,
            )
            debug_logger.log_warning(
                f"[GoogleDriveBackup] PostgreSQL restore failed: {type(exc).__name__}"
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, working_dir, True)
