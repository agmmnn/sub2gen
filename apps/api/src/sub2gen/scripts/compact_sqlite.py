"""Verified sub2gen seven-day cleanup and SQLite compaction maintenance tool."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict

from ..core.config import get_runtime_data_dir
from ..core.database import Database
from ..services.google_drive_backup import GoogleDriveBackupService


PROTECTED_COUNT_QUERIES = {
    "tokens": "SELECT COUNT(*) FROM tokens",
    "api_keys": "SELECT COUNT(*) FROM api_keys",
    "admin_config": "SELECT COUNT(*) FROM admin_config",
    "generation_config": "SELECT COUNT(*) FROM generation_config",
    "projects": "SELECT COUNT(*) FROM projects",
    "active_tasks": "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('completed','failed','cancelled','canceled')",
    "active_geminigen_tasks": "SELECT COUNT(*) FROM geminigen_tasks WHERE status IN ('queued','processing')",
    "active_runway_tasks": "SELECT COUNT(*) FROM runway_tasks WHERE status NOT IN ('completed','failed','cancelled','canceled')",
}


def _quote_sqlite_path(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _protected_counts(path: Path) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=30) as connection:
        for name, query in PROTECTED_COUNT_QUERIES.items():
            table_name = query.split("FROM ", 1)[1].split(" ", 1)[0]
            counts[name] = int(connection.execute(query).fetchone()[0]) if _table_exists(connection, table_name) else 0
    return counts


def _verify(path: Path, expected_counts: Dict[str, int]) -> Dict[str, Any]:
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=30) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    counts = _protected_counts(path)
    if integrity.lower() != "ok":
        raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
    if foreign_keys:
        raise RuntimeError(f"SQLite foreign_key_check found {len(foreign_keys)} violation(s)")
    if counts != expected_counts:
        raise RuntimeError(f"Protected row counts changed: before={expected_counts}, after={counts}")
    return {"integrity": integrity, "protected_counts": counts}


def _compact(source: Path, destination: Path, expected_counts: Dict[str, int]) -> Dict[str, Any]:
    if destination.exists():
        destination.unlink()
    with sqlite3.connect(source, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        estimated_bytes = max(page_size, (page_count - free_pages) * page_size)
        free_disk = shutil.disk_usage(source.parent).free
        required_free = int(estimated_bytes * 1.2)
        if free_disk < required_free:
            raise RuntimeError(
                f"Insufficient free space for VACUUM INTO: free={free_disk}, required~={required_free}"
            )
        connection.execute("ANALYZE")
        connection.execute("PRAGMA optimize")
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute(f"VACUUM INTO {_quote_sqlite_path(destination)}")
    verification = _verify(destination, expected_counts)
    return {
        **verification,
        "source_bytes": source.stat().st_size,
        "compact_bytes": destination.stat().st_size,
        "estimated_compact_bytes": estimated_bytes,
    }


async def _create_remote_backup(database: Database) -> Dict[str, Any]:
    service = GoogleDriveBackupService(database, app_version="1.0.0")
    await service.start_backup("pre-change-7d")
    if service._job_task is not None:
        await service._job_task
    job = service.public_status().get("job") or {}
    if job.get("status") != "completed" or not job.get("remote_file_id"):
        raise RuntimeError(f"Google Drive pre-change backup failed: {job.get('error') or job.get('status')}")
    return job


async def _run(args: argparse.Namespace) -> int:
    if args.confirm != "COMPACT":
        raise RuntimeError("Refusing maintenance: pass --confirm COMPACT")
    db_path = Path(args.database).resolve()
    if not db_path.is_file():
        raise RuntimeError(f"Database not found: {db_path}")

    database = Database(str(db_path))
    backup = await _create_remote_backup(database)
    before_counts = await asyncio.to_thread(_protected_counts, db_path)

    totals: Dict[str, int] = {}
    for _ in range(10_000):
        batch = await database.cleanup_retention_batch(days=args.days, batch_size=500)
        for key, value in batch.items():
            totals[key] = totals.get(key, 0) + int(value)
        if not any(batch.values()):
            break
        await asyncio.sleep(0)
    else:
        raise RuntimeError("Retention did not converge within 10,000 batches")

    compact_path = db_path.with_name(f"{db_path.name}.compact-{os.getpid()}")
    rollback_path = db_path.with_name(f"{db_path.name}.rollback-{int(time.time())}")
    verification = await asyncio.to_thread(_compact, db_path, compact_path, before_counts)

    await database.close_runtime_connections()
    try:
        os.replace(db_path, rollback_path)
        os.replace(compact_path, db_path)
        await asyncio.to_thread(_verify, db_path, before_counts)
    except Exception:
        if rollback_path.exists():
            if db_path.exists():
                db_path.unlink()
            os.replace(rollback_path, db_path)
        raise
    finally:
        if compact_path.exists():
            compact_path.unlink()

    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    rollback_path.unlink(missing_ok=True)

    print(json.dumps({
        "success": True,
        "google_drive_backup": backup,
        "retention": totals,
        "verification": verification,
        "database": str(db_path),
    }, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=str(get_runtime_data_dir() / "sub2gen.db"),
        help="SQLite database path (application must be stopped)",
    )
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--confirm", required=True, help="Must be COMPACT")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
