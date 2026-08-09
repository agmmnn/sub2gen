"""Encrypted PostgreSQL 16 backup archive creation and restore helpers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import struct
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.exceptions import InvalidTag
from psycopg.conninfo import conninfo_to_dict


ENCRYPTED_BACKUP_MAGIC = b"S2GPGBAK2"
ENCRYPTED_BACKUP_FORMAT_VERSION = 2
STREAM_CHUNK_BYTES = 4 * 1024 * 1024
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


class PostgresBackupError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(STREAM_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def load_backup_keys() -> tuple[str, dict[str, bytes]]:
    active_key_id = str(os.environ.get("SUB2GEN_BACKUP_ACTIVE_KEY_ID", "") or "").strip()
    raw = str(os.environ.get("SUB2GEN_BACKUP_KEYS_JSON", "") or "").strip()
    if not active_key_id or not raw:
        raise PostgresBackupError("PostgreSQL backup encryption keys are not configured")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise PostgresBackupError("SUB2GEN_BACKUP_KEYS_JSON is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise PostgresBackupError("SUB2GEN_BACKUP_KEYS_JSON must be an object")
    keys: dict[str, bytes] = {}
    for key_id, encoded in decoded.items():
        try:
            key = base64.b64decode(str(encoded), validate=True)
        except Exception as exc:
            raise PostgresBackupError(f"Backup key {key_id!r} is not valid base64") from exc
        if len(key) != 32:
            raise PostgresBackupError(f"Backup key {key_id!r} must decode to 32 bytes")
        keys[str(key_id)] = key
    if active_key_id not in keys:
        raise PostgresBackupError("The active PostgreSQL backup key ID is not present in the key ring")
    return active_key_id, keys


def encrypt_archive(source: Path, destination: Path, *, key_id: str, key: bytes) -> dict[str, Any]:
    nonce = os.urandom(12)
    header = {
        "format_version": ENCRYPTED_BACKUP_FORMAT_VERSION,
        "algorithm": "AES-256-GCM",
        "key_id": key_id,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "created_at": _utc_now(),
    }
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    prefix = ENCRYPTED_BACKUP_MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(prefix)
    destination.unlink(missing_ok=True)
    with source.open("rb") as input_handle, destination.open("wb") as output_handle:
        output_handle.write(prefix)
        while chunk := input_handle.read(STREAM_CHUNK_BYTES):
            output_handle.write(encryptor.update(chunk))
        output_handle.write(encryptor.finalize())
        output_handle.write(encryptor.tag)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    return {
        **header,
        "size": destination.stat().st_size,
        "sha256": _sha256_file(destination),
    }


def encrypted_archive_header(path: Path) -> tuple[dict[str, Any], bytes, int]:
    with path.open("rb") as handle:
        magic = handle.read(len(ENCRYPTED_BACKUP_MAGIC))
        if magic != ENCRYPTED_BACKUP_MAGIC:
            raise PostgresBackupError("Backup is not an encrypted PostgreSQL archive")
        length_raw = handle.read(4)
        if len(length_raw) != 4:
            raise PostgresBackupError("Encrypted backup header is truncated")
        header_length = struct.unpack(">I", length_raw)[0]
        if header_length < 2 or header_length > 64 * 1024:
            raise PostgresBackupError("Encrypted backup header length is invalid")
        header_bytes = handle.read(header_length)
        if len(header_bytes) != header_length:
            raise PostgresBackupError("Encrypted backup header is truncated")
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise PostgresBackupError("Encrypted backup header is invalid") from exc
    prefix = magic + length_raw + header_bytes
    return header, prefix, len(prefix)


def decrypt_archive(source: Path, destination: Path, *, keys: dict[str, bytes]) -> dict[str, Any]:
    header, prefix, payload_offset = encrypted_archive_header(source)
    if int(header.get("format_version") or 0) != ENCRYPTED_BACKUP_FORMAT_VERSION:
        raise PostgresBackupError("Unsupported encrypted PostgreSQL backup format")
    key_id = str(header.get("key_id") or "")
    key = keys.get(key_id)
    if key is None:
        raise PostgresBackupError(f"Backup encryption key {key_id!r} is unavailable")
    try:
        nonce = base64.b64decode(str(header.get("nonce") or ""), validate=True)
    except Exception as exc:
        raise PostgresBackupError("Encrypted backup nonce is invalid") from exc
    if len(nonce) != 12:
        raise PostgresBackupError("Encrypted backup nonce length is invalid")
    total_size = source.stat().st_size
    ciphertext_size = total_size - payload_offset - 16
    if ciphertext_size < 0:
        raise PostgresBackupError("Encrypted backup payload is truncated")
    with source.open("rb") as input_handle:
        input_handle.seek(total_size - 16)
        tag = input_handle.read(16)
        input_handle.seek(payload_offset)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(prefix)
        destination.unlink(missing_ok=True)
        remaining = ciphertext_size
        try:
            with destination.open("wb") as output_handle:
                while remaining:
                    chunk = input_handle.read(min(STREAM_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise PostgresBackupError("Encrypted backup payload is truncated")
                    remaining -= len(chunk)
                    output_handle.write(decryptor.update(chunk))
                output_handle.write(decryptor.finalize())
                output_handle.flush()
                os.fsync(output_handle.fileno())
        except InvalidTag as exc:
            destination.unlink(missing_ok=True)
            raise PostgresBackupError("Encrypted backup authentication failed") from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return header


def _profile_excluded(relative: Path) -> bool:
    return (
        bool(VOLATILE_PROFILE_DIRECTORIES.intersection(relative.parts))
        or relative.name.startswith("Singleton")
        or relative.name.endswith(".part")
    )


def _archive_files(
    dump_path: Path,
    profiles_root: Path,
    working_dir: Path,
    archive_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = [
        {
            "path": "database/sub2gen.dump",
            "size": dump_path.stat().st_size,
            "sha256": _sha256_file(dump_path),
        }
    ]
    profiles: list[tuple[Path, str]] = []
    snapshot_root = working_dir / "browser-profile-snapshot"
    if profiles_root.is_dir():
        for source in profiles_root.rglob("*"):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(profiles_root)
            if _profile_excluded(relative):
                continue
            archive_name = str(PurePosixPath("browser_profiles", *relative.parts))
            snapshot = snapshot_root / relative
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source, snapshot)
            except FileNotFoundError:
                # Chromium rotates session/cache files while a profile is live.
                # The durable profile state is still captured by the retained files.
                continue
            entries.append(
                {
                    "path": archive_name,
                    "size": snapshot.stat().st_size,
                    "sha256": _sha256_file(snapshot),
                }
            )
            profiles.append((snapshot, archive_name))
    manifest["files"] = entries
    manifest_path = working_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    with tarfile.open(archive_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.add(manifest_path, arcname="manifest.json", recursive=False)
        archive.add(dump_path, arcname="database/sub2gen.dump", recursive=False)
        for source, archive_name in profiles:
            archive.add(source, arcname=archive_name, recursive=False)
    return manifest


def _postgres_process_args(database_url: str) -> tuple[dict[str, str], list[str]]:
    values = conninfo_to_dict(database_url)
    env = dict(os.environ)
    password = str(values.pop("password", "") or "")
    if password:
        env["PGPASSWORD"] = password
    args: list[str] = []
    mapping = {"host": "--host", "port": "--port", "user": "--username", "dbname": "--dbname"}
    for key, flag in mapping.items():
        value = str(values.get(key, "") or "")
        if value:
            args.extend([flag, value])
    for key, env_name in (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT")):
        value = str(values.get(key, "") or "")
        if value:
            env[env_name] = value
    return env, args


async def _run_process(command: list[str], *, env: dict[str, str]) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise PostgresBackupError(f"Required PostgreSQL client is unavailable: {command[0]}") from exc
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["unknown error"]
        raise PostgresBackupError(f"{Path(command[0]).name} failed: {detail[0][:300]}")


async def _require_pg16(binary: str) -> None:
    env = dict(os.environ)
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "--version",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise PostgresBackupError(f"Required PostgreSQL client is unavailable: {binary}") from exc
    stdout, _stderr = await process.communicate()
    text = stdout.decode("utf-8", errors="replace")
    if process.returncode != 0 or not any(token.startswith("16.") or token == "16" for token in text.split()):
        raise PostgresBackupError(f"PostgreSQL 16 client required; got {text.strip()[:120]}")


async def database_row_counts(database: Any, *, connection: Any | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if connection is None:
        async with database._connect() as acquired:
            return await database_row_counts(database, connection=acquired)
    tables_cursor = await connection.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = ? AND table_name NOT LIKE '\\_%' ESCAPE '\\'
        ORDER BY table_name
        """,
        (database.schema,),
    )
    for row in await tables_cursor.fetchall():
        table = str(row["table_name"])
        if table in {"schema_migrations"}:
            continue
        count_cursor = await connection.execute(f'SELECT COUNT(*) AS count FROM "{table}"')
        counts[table] = int((await count_cursor.fetchone())["count"])
    return counts


class _BackupSnapshot:
    """Small holder that guarantees an exported snapshot stays valid during pg_dump."""

    def __init__(self, context: Any, connection: Any, snapshot_id: str, counts: dict[str, int]):
        self.context = context
        self.connection = connection
        self.snapshot_id = snapshot_id
        self.counts = counts

    async def close(self) -> None:
        try:
            await self.connection.rollback()
        finally:
            await self.context.__aexit__(None, None, None)


async def open_backup_snapshot(database: Any) -> _BackupSnapshot:
    context = database._connect()
    connection = await context.__aenter__()
    try:
        await connection.execute(
            """
            BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY
            """
        )
        cursor = await connection.execute("SELECT pg_export_snapshot() AS snapshot_id")
        row = await cursor.fetchone()
        snapshot_id = str(row["snapshot_id"] if row else "").strip()
        if not snapshot_id:
            raise PostgresBackupError("PostgreSQL did not export a backup snapshot")
        counts = await database_row_counts(database, connection=connection)
        return _BackupSnapshot(context, connection, snapshot_id, counts)
    except Exception:
        try:
            await connection.rollback()
        finally:
            await context.__aexit__(None, None, None)
        raise


async def create_postgres_archive(
    database: Any,
    profiles_root: Path,
    working_dir: Path,
    encrypted_path: Path,
    *,
    backup_id: str,
    backup_type: str,
    app_version: str,
) -> dict[str, Any]:
    active_key_id, keys = load_backup_keys()
    pg_dump = str(os.environ.get("SUB2GEN_PG_DUMP_BIN", "pg_dump") or "pg_dump")
    await _require_pg16(pg_dump)
    dump_dir = working_dir / "database"
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_path = dump_dir / "sub2gen.dump"
    env, connection_args = _postgres_process_args(database.database_url)
    snapshot = await open_backup_snapshot(database)
    try:
        await _run_process(
            [
                pg_dump,
                *connection_args,
                "--format=custom",
                "--compress=6",
                "--no-owner",
                "--no-privileges",
                "--snapshot",
                snapshot.snapshot_id,
                "--schema",
                database.schema,
                "--file",
                str(dump_path),
            ],
            env=env,
        )
    finally:
        await snapshot.close()
    health = await database.health_snapshot()
    row_counts = snapshot.counts
    manifest = {
        "format_version": ENCRYPTED_BACKUP_FORMAT_VERSION,
        "backup_id": backup_id,
        "backup_type": backup_type,
        "created_at": _utc_now(),
        "application_version": app_version,
        "database_backend": "postgres",
        "database_schema": database.schema,
        "database_revision": health.get("database_revision"),
        "cutover_marker_present": health.get("cutover_marker_present"),
        "encryption_key_id": active_key_id,
        "dump_format": "postgresql-custom",
        "row_counts": row_counts,
    }
    plain_archive = working_dir / "archive.tar.gz"
    await asyncio.to_thread(
        _archive_files,
        dump_path,
        profiles_root,
        working_dir,
        plain_archive,
        manifest,
    )
    encryption = await asyncio.to_thread(
        encrypt_archive,
        plain_archive,
        encrypted_path,
        key_id=active_key_id,
        key=keys[active_key_id],
    )
    manifest["encrypted_archive_size"] = encryption["size"]
    manifest["encrypted_archive_sha256"] = encryption["sha256"]
    return manifest


def _safe_extract(archive_path: Path, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or not (member.isfile() or member.isdir()):
                raise PostgresBackupError("Backup archive contains an unsafe path")
            target = (destination / Path(*pure.parts)).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise PostgresBackupError("Backup archive escapes the restore directory")
        for member in members:
            target = destination / Path(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise PostgresBackupError("Backup archive contains an unreadable file")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=STREAM_CHUNK_BYTES)
    manifest_path = destination / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise PostgresBackupError("Backup manifest is invalid") from exc
    if manifest.get("database_backend") != "postgres":
        raise PostgresBackupError("Backup is not a PostgreSQL backup")
    for entry in manifest.get("files") or []:
        relative = PurePosixPath(str(entry.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise PostgresBackupError("Backup manifest contains an unsafe path")
        path = destination / Path(*relative.parts)
        if not path.is_file() or path.stat().st_size != int(entry.get("size", -1)):
            raise PostgresBackupError("Backup file size validation failed")
        if _sha256_file(path) != str(entry.get("sha256") or ""):
            raise PostgresBackupError("Backup checksum validation failed")
    dump_path = destination / "database" / "sub2gen.dump"
    if not dump_path.is_file():
        raise PostgresBackupError("Backup does not contain a PostgreSQL dump")
    return manifest


async def decrypt_and_extract_postgres_archive(
    encrypted_path: Path,
    working_dir: Path,
) -> tuple[dict[str, Any], Path]:
    working_dir.mkdir(parents=True, exist_ok=True)
    _active_key_id, keys = load_backup_keys()
    plain_archive = working_dir / "decrypted.tar.gz"
    await asyncio.to_thread(decrypt_archive, encrypted_path, plain_archive, keys=keys)
    extracted = working_dir / "extracted"
    manifest = await asyncio.to_thread(_safe_extract, plain_archive, extracted)
    return manifest, extracted


async def restore_postgres_dump(database: Any, dump_path: Path) -> None:
    pg_restore = str(os.environ.get("SUB2GEN_PG_RESTORE_BIN", "pg_restore") or "pg_restore")
    await _require_pg16(pg_restore)
    await database.close_runtime_connections()
    env, connection_args = _postgres_process_args(database.database_url)
    await _run_process(
        [
            pg_restore,
            *connection_args,
            "--clean",
            "--if-exists",
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--schema",
            database.schema,
            str(dump_path),
        ],
        env=env,
    )
    await database.init_db()
    await database.cache_schema_capabilities()


async def verify_restored_row_counts(database: Any, expected: dict[str, Any]) -> None:
    actual = await database_row_counts(database)
    mismatches = {
        table: {"expected": int(count), "actual": int(actual.get(table, -1))}
        for table, count in expected.items()
        if int(actual.get(table, -1)) != int(count)
    }
    if mismatches:
        raise PostgresBackupError(f"Restored PostgreSQL row counts do not match: {mismatches}")
