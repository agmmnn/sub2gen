"""sub2gen SQLite-to-PostgreSQL bridge migration CLI.

Commands are intentionally explicit and restartable:

    python -m sub2gen.scripts.migrate_sqlite_to_postgres preflight
    python -m sub2gen.scripts.migrate_sqlite_to_postgres backfill
    python -m sub2gen.scripts.migrate_sqlite_to_postgres cutover --confirm CUTOVER
    python -m sub2gen.scripts.migrate_sqlite_to_postgres verify
    python -m sub2gen.scripts.migrate_sqlite_to_postgres abort --confirm ABORT
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from ..core.config import get_runtime_data_dir
from ..core.database_runtime import DatabaseSettings
from ..core.postgres_migrations import read_database_markers, run_postgres_migrations
from ..services.redis_runtime import REDIS_MAINTENANCE_KEY, REDIS_STATE_KEY, REDIS_STATE_VERSION


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled", "error"}
HISTORY_TABLES = {
    "api_key_audit_logs",
    "geminigen_tasks",
    "redis_persisted_events",
    "request_logs",
    "runway_tasks",
    "tasks",
}
TABLE_ORDER = [
    "admin_config",
    "proxy_config",
    "generation_config",
    "call_logic_config",
    "cache_config",
    "debug_config",
    "captcha_config",
    "plugin_config",
    "token_refresh_config",
    "runway_config",
    "geminigen_config",
    "api_clients",
    "tokens",
    "token_stats",
    "admin_sessions",
    "runway_accounts",
    "runway_models",
    "geminigen_accounts",
    "api_keys",
    "api_key_accounts",
    "api_key_rate_limits",
    "projects",
    "cache_files",
    "extension_worker_bindings",
    "captcha_worker_keys",
    "dedicated_extension_workers",
    "operation_stats",
    "tasks",
    "request_logs",
    "runway_tasks",
    "geminigen_tasks",
    "api_key_audit_logs",
    "redis_persisted_events",
]
REQUIRED_SOURCE_TABLES = {"admin_config", "tokens"}
INTERNAL_TARGET_TABLES = {"schema_migrations", "system_metadata"}
OBSOLETE_SOURCE_COLUMNS = {
    ("geminigen_accounts", "image_daily_limit_reset_at"),
}
REDACT_KEY_RE = re.compile(
    r"(?i)(authorization|cookie|password|secret|token|api[_-]?key|st|at)"
    r"(\s*[=:]\s*)([^\s,;\]\}]+)"
)


class MigrationError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime,)):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(destination) as target_db:
            source_db.backup(target_db, pages=2048)
    with sqlite3.connect(f"{destination.resolve().as_uri()}?mode=ro", uri=True) as check:
        result = check.execute("PRAGMA quick_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise MigrationError("SQLite snapshot integrity validation failed")


def _sqlite_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _sqlite_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _sqlite_primary_key(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [str(row[1]) for row in sorted(rows, key=lambda item: int(item[5])) if int(row[5]) > 0]


def _history_where(table: str, columns: set[str], cutoff: str) -> tuple[str, tuple[Any, ...]]:
    if table in {"tasks", "runway_tasks", "geminigen_tasks"}:
        timestamp_parts = [name for name in ("completed_at", "updated_at", "created_at") if name in columns]
        timestamp = f"COALESCE({', '.join(timestamp_parts)})" if timestamp_parts else "NULL"
        return (
            f"WHERE LOWER(COALESCE(status, '')) NOT IN ({','.join('?' for _ in TERMINAL_STATUSES)}) "
            f"OR {timestamp} >= ?",
            tuple(sorted(TERMINAL_STATUSES)) + (cutoff,),
        )
    timestamp = "persisted_at" if table == "redis_persisted_events" else "created_at"
    if timestamp in columns:
        return f"WHERE {timestamp} >= ?", (cutoff,)
    return "", ()


def _selected_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    retention_days: int,
) -> Iterator[sqlite3.Row]:
    cutoff = (_utc_now() - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    where, parameters = _history_where(table, set(columns), cutoff) if table in HISTORY_TABLES else ("", ())
    primary = [column for column in _sqlite_primary_key(connection, table) if column in columns]
    quoted_primary = ", ".join('"' + column.replace('"', '""') + '"' for column in primary)
    quoted_columns = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
    order = f" ORDER BY {quoted_primary}" if primary else ""
    query = f'SELECT {quoted_columns} FROM "{table}" {where}{order}'
    yield from connection.execute(query, parameters)


def _redact_and_cap(value: Any, limit: int = 1024) -> str | None:
    if value is None:
        return None
    redacted = REDACT_KEY_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", str(value))
    encoded = redacted.encode("utf-8", errors="replace")[:limit]
    return encoded.decode("utf-8", errors="ignore")


def _normalize_source_value(value: Any, data_type: str) -> Any:
    if value is None:
        return None
    if data_type == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if "timestamp" in data_type and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return value
    return value


def _sanitize_row(table: str, row: dict[str, Any], data_types: dict[str, str]) -> tuple[Any, ...]:
    status = str(row.get("status") or "").lower()
    terminal = status in TERMINAL_STATUSES
    if table == "request_logs":
        row["request_body"] = None
        row["response_body"] = None
        for field in ("request_excerpt", "response_excerpt", "status_text"):
            if field in row:
                row[field] = _redact_and_cap(row[field])
    elif table == "api_key_audit_logs" and "detail" in row:
        row["detail"] = _redact_and_cap(row["detail"])
    elif table in {"tasks", "runway_tasks", "geminigen_tasks"}:
        if terminal:
            for field in ("request_payload", "response_payload"):
                if field in row:
                    row[field] = None
        if "prompt" in row and terminal:
            row["prompt"] = _redact_and_cap(row["prompt"])
        if "error_message" in row:
            row["error_message"] = _redact_and_cap(row["error_message"])
    return tuple(_normalize_source_value(value, data_types.get(column, "")) for column, value in row.items())


async def _connect_target(settings: DatabaseSettings) -> AsyncConnection[Any]:
    try:
        connection = await AsyncConnection.connect(settings.url, row_factory=dict_row)
    except Exception as exc:
        raise MigrationError("Unable to connect to target PostgreSQL") from exc
    await connection.execute("SET TIME ZONE 'UTC'")
    await connection.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(settings.schema)))
    await connection.commit()
    return connection


async def _target_tables(connection: AsyncConnection[Any], schema: str) -> set[str]:
    cursor = await connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (schema,),
    )
    return {str(row["table_name"]) for row in await cursor.fetchall()}


async def _target_columns(
    connection: AsyncConnection[Any], schema: str, table: str
) -> tuple[list[str], dict[str, str]]:
    cursor = await connection.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    rows = await cursor.fetchall()
    return (
        [str(row["column_name"]) for row in rows],
        {str(row["column_name"]): str(row["data_type"]) for row in rows},
    )


async def _primary_key(connection: AsyncConnection[Any], schema: str, table: str) -> list[str]:
    cursor = await connection.execute(
        """
        SELECT attribute.attname AS column_name
        FROM pg_index index_info
        JOIN pg_class table_info ON table_info.oid = index_info.indrelid
        JOIN pg_namespace namespace_info ON namespace_info.oid = table_info.relnamespace
        JOIN unnest(index_info.indkey) WITH ORDINALITY AS key_info(attnum, ordering) ON TRUE
        JOIN pg_attribute attribute
          ON attribute.attrelid = table_info.oid AND attribute.attnum = key_info.attnum
        WHERE namespace_info.nspname = %s AND table_info.relname = %s AND index_info.indisprimary
        ORDER BY key_info.ordering
        """,
        (schema, table),
    )
    return [str(row["column_name"]) for row in await cursor.fetchall()]


async def _target_nonempty_counts(
    connection: AsyncConnection[Any], schema: str, tables: Iterable[str]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        cursor = await connection.execute(
            sql.SQL("SELECT COUNT(*) AS count FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
        )
        counts[table] = int((await cursor.fetchone())["count"])
    return counts


def _projected_bytes(
    connection: sqlite3.Connection,
    target_columns: dict[str, list[str]],
    retention_days: int,
) -> tuple[int, dict[str, int]]:
    per_table: dict[str, int] = {}
    source_tables = _sqlite_tables(connection)
    for table in TABLE_ORDER:
        if table not in source_tables or table not in target_columns:
            continue
        source_columns = set(_sqlite_columns(connection, table))
        common = [column for column in target_columns[table] if column in source_columns]
        total = 0
        for row in _selected_rows(connection, table, common, retention_days):
            total += 32 + sum(len(str(value).encode("utf-8", errors="replace")) for value in row if value is not None)
        per_table[table] = total
    raw = sum(per_table.values())
    return int(raw * 1.6), per_table


async def preflight(
    sqlite_path: Path,
    settings: DatabaseSettings,
    *,
    retention_days: int,
    volume_capacity_bytes: int,
    require_empty: bool,
) -> dict[str, Any]:
    if not sqlite_path.is_file():
        raise MigrationError(f"SQLite database not found: {sqlite_path}")
    source = _sqlite_connection(sqlite_path)
    target = await _connect_target(settings)
    try:
        integrity = source.execute("PRAGMA quick_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise MigrationError(f"SQLite quick_check failed: {integrity[0] if integrity else 'unknown'}")
        foreign_key_errors = source.execute("PRAGMA foreign_key_check").fetchmany(20)
        if foreign_key_errors:
            raise MigrationError(f"SQLite contains unresolved foreign keys: {len(foreign_key_errors)}+")
        source_tables = _sqlite_tables(source)
        missing_required = sorted(REQUIRED_SOURCE_TABLES - source_tables)
        if missing_required:
            raise MigrationError(f"SQLite is missing required tables: {', '.join(missing_required)}")

        revision = await run_postgres_migrations(target, settings.schema)
        version_cursor = await target.execute(
            "SELECT current_setting('server_version_num')::BIGINT AS version"
        )
        version = int((await version_cursor.fetchone())["version"])
        if version // 10000 != 16:
            raise MigrationError(f"PostgreSQL 16 is required; target reports {version}")
        target_tables = await _target_tables(target, settings.schema)
        unknown_source = sorted(source_tables - target_tables)
        if unknown_source:
            raise MigrationError(
                "Target schema does not represent source tables: " + ", ".join(unknown_source)
            )
        column_map: dict[str, list[str]] = {}
        for table in source_tables & target_tables:
            target_column_list = (await _target_columns(target, settings.schema, table))[0]
            column_map[table] = target_column_list
            extra_columns = {
                column
                for column in _sqlite_columns(source, table)
                if column not in target_column_list and (table, column) not in OBSOLETE_SOURCE_COLUMNS
            }
            if extra_columns:
                raise MigrationError(
                    f"Target table {table} is missing source columns: {', '.join(sorted(extra_columns))}"
                )
        projected, per_table = _projected_bytes(source, column_map, retention_days)
        allowed = int(volume_capacity_bytes * 0.60)
        if projected > allowed:
            raise MigrationError(
                f"Projected PostgreSQL data plus indexes ({projected} bytes) exceeds "
                f"60% of the configured volume ({allowed} bytes)"
            )

        business_tables = sorted((target_tables - INTERNAL_TARGET_TABLES) & set(TABLE_ORDER))
        counts = await _target_nonempty_counts(target, settings.schema, business_tables)
        nonempty = {
            table: count
            for table, count in counts.items()
            if count > (1 if table.endswith("_config") or table == "admin_config" else 0)
        }
        markers = await read_database_markers(target, settings.schema)
        if require_empty and nonempty and not markers.get("bridge_backfill_source_sha256"):
            raise MigrationError(f"Target PostgreSQL is not empty: {nonempty}")
        return {
            "ok": True,
            "sqlite_integrity": "ok",
            "sqlite_size_bytes": sqlite_path.stat().st_size,
            "source_table_count": len(source_tables),
            "postgres_version_num": version,
            "database_revision": revision,
            "target_nonempty": nonempty,
            "projected_postgres_bytes": projected,
            "volume_capacity_bytes": volume_capacity_bytes,
            "volume_utilization_projected": round(projected / volume_capacity_bytes, 4),
            "largest_projected_tables": sorted(per_table.items(), key=lambda item: item[1], reverse=True)[:10],
            "history_retention_days": retention_days,
        }
    finally:
        source.close()
        await target.close()


async def _copy_to_stage(
    source: sqlite3.Connection,
    target: AsyncConnection[Any],
    settings: DatabaseSettings,
    table: str,
    retention_days: int,
) -> tuple[int, list[str]]:
    source_columns = set(_sqlite_columns(source, table))
    target_columns, data_types = await _target_columns(target, settings.schema, table)
    common = [column for column in target_columns if column in source_columns]
    if not common:
        raise MigrationError(f"No compatible columns for table {table}")
    stage = f"_stage_{table}"
    await target.execute(
        sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(settings.schema), sql.Identifier(stage))
    )
    await target.execute(
        sql.SQL("CREATE UNLOGGED TABLE {}.{} (LIKE {}.{} INCLUDING DEFAULTS)").format(
            sql.Identifier(settings.schema),
            sql.Identifier(stage),
            sql.Identifier(settings.schema),
            sql.Identifier(table),
        )
    )
    copy_statement = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
        sql.Identifier(settings.schema),
        sql.Identifier(stage),
        sql.SQL(", ").join(sql.Identifier(column) for column in common),
    )
    count = 0
    async with target.cursor() as cursor:
        async with cursor.copy(copy_statement) as copy:
            for sqlite_row in _selected_rows(source, table, common, retention_days):
                row_dict = {column: sqlite_row[column] for column in common}
                sanitized = _sanitize_row(table, row_dict, data_types)
                await copy.write_row(sanitized)
                count += 1
    return count, common


async def _delete_stale_from_stage(
    target: AsyncConnection[Any],
    settings: DatabaseSettings,
    table: str,
    primary: Sequence[str],
) -> None:
    stage = f"_stage_{table}"
    match = sql.SQL(" AND ").join(
        sql.SQL("source.{} = durable.{}").format(sql.Identifier(column), sql.Identifier(column))
        for column in primary
    )
    await target.execute(
        sql.SQL(
            "DELETE FROM {}.{} AS durable WHERE NOT EXISTS "
            "(SELECT 1 FROM {}.{} AS source WHERE {})"
        ).format(
            sql.Identifier(settings.schema),
            sql.Identifier(table),
            sql.Identifier(settings.schema),
            sql.Identifier(stage),
            match,
        )
    )


async def _upsert_stage(
    target: AsyncConnection[Any],
    settings: DatabaseSettings,
    table: str,
    common: Sequence[str],
    primary: Sequence[str],
) -> None:
    stage = f"_stage_{table}"
    columns_sql = sql.SQL(", ").join(sql.Identifier(column) for column in common)
    update_columns = [column for column in common if column not in primary]
    conflict_action = sql.SQL("DO NOTHING")
    if update_columns:
        conflict_action = sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
            for column in update_columns
        )
    await target.execute(
        sql.SQL(
            "INSERT INTO {}.{} ({}) SELECT {} FROM {}.{} WHERE TRUE "
            "ON CONFLICT ({}) {}"
        ).format(
            sql.Identifier(settings.schema),
            sql.Identifier(table),
            columns_sql,
            columns_sql,
            sql.Identifier(settings.schema),
            sql.Identifier(stage),
            sql.SQL(", ").join(sql.Identifier(column) for column in primary),
            conflict_action,
        )
    )
    await target.execute(
        sql.SQL("DROP TABLE {}.{}").format(sql.Identifier(settings.schema), sql.Identifier(stage))
    )


async def _reset_identities(
    target: AsyncConnection[Any], settings: DatabaseSettings, imported_tables: Iterable[str]
) -> None:
    for table in imported_tables:
        columns, _types = await _target_columns(target, settings.schema, table)
        if "id" not in columns:
            continue
        cursor = await target.execute(
            "SELECT pg_get_serial_sequence(%s, 'id') AS sequence_name",
            (f"{settings.schema}.{table}",),
        )
        row = await cursor.fetchone()
        sequence_name = str(row["sequence_name"] or "") if row else ""
        if not sequence_name:
            continue
        max_cursor = await target.execute(
            sql.SQL("SELECT COALESCE(MAX(id), 0)::BIGINT AS maximum FROM {}.{}").format(
                sql.Identifier(settings.schema), sql.Identifier(table)
            )
        )
        maximum = int((await max_cursor.fetchone())["maximum"])
        await target.execute("SELECT setval(%s, %s, %s)", (sequence_name, max(1, maximum), maximum > 0))


async def _load_snapshot(
    snapshot: Path,
    settings: DatabaseSettings,
    *,
    retention_days: int,
    marker_name: str,
    final_cutover: bool,
) -> dict[str, Any]:
    source = _sqlite_connection(snapshot)
    target = await _connect_target(settings)
    imported: dict[str, int] = {}
    try:
        source_tables = _sqlite_tables(source)
        target_tables = await _target_tables(target, settings.schema)
        # Catalog reads start an implicit psycopg transaction. End it before the
        # explicit reconciliation transaction so successful staging is committed
        # rather than becoming a nested savepoint rolled back on connection close.
        await target.commit()
        async with target.transaction():
            staged: dict[str, tuple[list[str], list[str]]] = {}
            for table in TABLE_ORDER:
                if table not in source_tables or table not in target_tables:
                    continue
                count, common = await _copy_to_stage(
                    source, target, settings, table, retention_days
                )
                primary = await _primary_key(target, settings.schema, table)
                if not primary or any(column not in common for column in primary):
                    raise MigrationError(f"Cannot reconcile {table}: primary key is not importable")
                staged[table] = (common, primary)
                imported[table] = count
            for table in reversed(TABLE_ORDER):
                if table in staged:
                    await _delete_stale_from_stage(
                        target, settings, table, staged[table][1]
                    )
            for table in TABLE_ORDER:
                if table in staged:
                    common, primary = staged[table]
                    await _upsert_stage(target, settings, table, common, primary)
            await _reset_identities(target, settings, imported)
            source_hash = await asyncio.to_thread(_sha256_file, snapshot)
            await target.execute(
                """
                INSERT INTO system_metadata (key, value, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
                """,
                (marker_name, source_hash),
            )
            await target.execute(
                """
                INSERT INTO system_metadata (key, value, updated_at)
                VALUES ('history_retention_days', %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
                """,
                (str(retention_days),),
            )
            if final_cutover:
                await target.execute(
                    """
                    INSERT INTO system_metadata (key, value, updated_at)
                    VALUES ('cutover_completed_at', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
                    """,
                    (_iso_now(),),
                )
        return {"imported_rows": imported, "source_sha256": source_hash}
    finally:
        source.close()
        await target.close()


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


async def _verify_table(
    source: sqlite3.Connection,
    target: AsyncConnection[Any],
    settings: DatabaseSettings,
    table: str,
    retention_days: int,
) -> dict[str, Any]:
    source_columns = set(_sqlite_columns(source, table))
    target_columns, data_types = await _target_columns(target, settings.schema, table)
    common = [column for column in target_columns if column in source_columns]
    source_digest = hashlib.sha256()
    source_count = 0
    for sqlite_row in _selected_rows(source, table, common, retention_days):
        row = {column: sqlite_row[column] for column in common}
        normalized = _sanitize_row(table, row, data_types)
        source_digest.update(
            json.dumps([_canonical(value) for value in normalized], separators=(",", ":"), default=_json_default).encode()
        )
        source_digest.update(b"\n")
        source_count += 1

    primary = await _primary_key(target, settings.schema, table)
    order = primary or common
    cursor = await target.execute(
        sql.SQL("SELECT {} FROM {}.{} ORDER BY {}").format(
            sql.SQL(", ").join(sql.Identifier(column) for column in common),
            sql.Identifier(settings.schema),
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(column) for column in order),
        )
    )
    target_digest = hashlib.sha256()
    target_count = 0
    async for row in cursor:
        target_digest.update(
            json.dumps([_canonical(row[column]) for column in common], separators=(",", ":"), default=_json_default).encode()
        )
        target_digest.update(b"\n")
        target_count += 1
    return {
        "source_count": source_count,
        "target_count": target_count,
        "source_sha256": source_digest.hexdigest(),
        "target_sha256": target_digest.hexdigest(),
        "matches": source_count == target_count and source_digest.digest() == target_digest.digest(),
    }


async def verify(
    sqlite_path: Path,
    settings: DatabaseSettings,
    *,
    retention_days: int,
) -> dict[str, Any]:
    source = _sqlite_connection(sqlite_path)
    target = await _connect_target(settings)
    results: dict[str, Any] = {}
    try:
        source_tables = _sqlite_tables(source)
        target_tables = await _target_tables(target, settings.schema)
        for table in TABLE_ORDER:
            if table in source_tables and table in target_tables:
                results[table] = await _verify_table(
                    source, target, settings, table, retention_days
                )
        failed = [table for table, result in results.items() if not result["matches"]]
        invalid_constraints = await target.execute(
            """
            SELECT conname FROM pg_constraint c
            JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = %s AND NOT c.convalidated
            """,
            (settings.schema,),
        )
        invalid = [str(row["conname"]) for row in await invalid_constraints.fetchall()]
        active: dict[str, dict[str, int]] = {}
        for table in ("tasks", "runway_tasks", "geminigen_tasks"):
            if table not in source_tables:
                continue
            source_active = source.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE LOWER(COALESCE(status, \'\')) '
                f'NOT IN ({",".join("?" for _ in TERMINAL_STATUSES)})',
                tuple(sorted(TERMINAL_STATUSES)),
            ).fetchone()[0]
            target_active_cursor = await target.execute(
                sql.SQL(
                    "SELECT COUNT(*) AS count FROM {}.{} WHERE LOWER(COALESCE(status, '')) "
                    "NOT IN ({})"
                ).format(
                    sql.Identifier(settings.schema),
                    sql.Identifier(table),
                    sql.SQL(",").join(sql.Literal(value) for value in sorted(TERMINAL_STATUSES)),
                )
            )
            active[table] = {
                "source": int(source_active),
                "target": int((await target_active_cursor.fetchone())["count"]),
            }
        if failed or invalid or any(item["source"] != item["target"] for item in active.values()):
            failed_results = {table: results[table] for table in failed}
            raise MigrationError(
                f"Verification failed: tables={failed_results}, constraints={invalid}, active_tasks={active}"
            )
        markers = await read_database_markers(target, settings.schema)
        return {"ok": True, "tables": results, "active_tasks": active, "markers": markers}
    finally:
        source.close()
        await target.close()


async def _redis_client() -> Any:
    url = str(os.environ.get("SUB2GEN_REDIS_URL", "") or "").strip()
    if not url:
        raise MigrationError("SUB2GEN_REDIS_URL is required for cutover maintenance")
    import redis.asyncio as redis

    client = redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=3)
    marker = await client.get(REDIS_STATE_KEY)
    if marker != REDIS_STATE_VERSION:
        await client.aclose()
        raise MigrationError("Redis state marker is missing or incompatible")
    return client


async def _set_maintenance(client: Any, active: bool, reason: str) -> None:
    if active:
        await client.set(
            REDIS_MAINTENANCE_KEY,
            json.dumps(
                {"active": True, "reason": reason, "owner": "migration_cli", "started_at": _iso_now()},
                separators=(",", ":"),
            ),
        )
    else:
        await client.delete(REDIS_MAINTENANCE_KEY)


def _sqlite_provider_inflight(sqlite_path: Path) -> dict[str, int]:
    connection = _sqlite_connection(sqlite_path)
    try:
        tables = _sqlite_tables(connection)
        runway = 0
        geminigen = 0
        if "runway_accounts" in tables:
            runway = int(
                connection.execute(
                    "SELECT COALESCE(SUM(in_flight), 0) FROM runway_accounts"
                ).fetchone()[0]
                or 0
            )
        if "geminigen_accounts" in tables:
            geminigen = int(
                connection.execute(
                    "SELECT COALESCE(SUM(image_in_flight + video_in_flight), 0) FROM geminigen_accounts"
                ).fetchone()[0]
                or 0
            )
        return {"runway": runway, "geminigen": geminigen}
    finally:
        connection.close()


async def _drain_inflight(
    client: Any, sqlite_path: Path, timeout_seconds: int
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_total = 0
    while True:
        total = 0
        async for key in client.scan_iter(match="sub2gen:inflight:*"):
            try:
                total += max(0, int(await client.get(key) or 0))
            except (TypeError, ValueError):
                continue
        provider = await asyncio.to_thread(_sqlite_provider_inflight, sqlite_path)
        last_total = total + provider["runway"] + provider["geminigen"]
        if last_total == 0:
            return {
                "drained": True,
                "remaining": 0,
                "redis": 0,
                **provider,
            }
        if time.monotonic() >= deadline:
            return {
                "drained": False,
                "remaining": last_total,
                "redis": total,
                **provider,
            }
        await asyncio.sleep(1)


async def backfill(args: argparse.Namespace, settings: DatabaseSettings) -> dict[str, Any]:
    report = await preflight(
        args.sqlite,
        settings,
        retention_days=args.retention_days,
        volume_capacity_bytes=args.volume_capacity_gb * 1024**3,
        require_empty=True,
    )
    snapshot = args.state_dir / "backfill-snapshot.db"
    await asyncio.to_thread(_sqlite_snapshot, args.sqlite, snapshot)
    loaded = await _load_snapshot(
        snapshot,
        settings,
        retention_days=args.retention_days,
        marker_name="bridge_backfill_source_sha256",
        final_cutover=False,
    )
    verification = await verify(snapshot, settings, retention_days=args.retention_days)
    state = {
        "phase": "backfilled",
        "updated_at": _iso_now(),
        "snapshot": str(snapshot),
        "preflight": report,
        "load": loaded,
        "verified": verification["ok"],
    }
    _write_json(args.state_dir / "state.json", state)
    return state


async def cutover(args: argparse.Namespace, settings: DatabaseSettings) -> dict[str, Any]:
    if args.confirm != "CUTOVER":
        raise MigrationError("Refusing cutover: pass --confirm CUTOVER")
    client = await _redis_client()
    snapshot = args.state_dir / "cutover-snapshot.db"
    cutover_started = time.monotonic()
    await _set_maintenance(client, True, "postgres_cutover")
    try:
        drain = await _drain_inflight(
            client, args.sqlite, args.drain_timeout_seconds
        )
        if not drain["drained"]:
            raise MigrationError(f"Active work did not drain before cutover: {drain}")
        async with asyncio.timeout(args.cutover_deadline_seconds):
            await asyncio.to_thread(_sqlite_snapshot, args.sqlite, snapshot)
            loaded = await _load_snapshot(
                snapshot,
                settings,
                retention_days=args.retention_days,
                marker_name="cutover_source_sha256",
                final_cutover=True,
            )
            verification = await verify(snapshot, settings, retention_days=args.retention_days)
        state = {
            "phase": "cutover_complete",
            "updated_at": _iso_now(),
            "snapshot": str(snapshot),
            "drain": drain,
            "load": loaded,
            "verified": verification["ok"],
            "duration_seconds": round(time.monotonic() - cutover_started, 3),
            "maintenance_active": True,
        }
        _write_json(args.state_dir / "state.json", state)
        return state
    except Exception:
        await _set_maintenance(client, False, "cutover_failed")
        raise
    finally:
        await client.aclose()


async def abort(args: argparse.Namespace, settings: DatabaseSettings) -> dict[str, Any]:
    if args.confirm != "ABORT":
        raise MigrationError("Refusing abort: pass --confirm ABORT")
    target = await _connect_target(settings)
    client = await _redis_client()
    try:
        markers = await read_database_markers(target, settings.schema)
        if markers.get("cutover_completed_at"):
            raise MigrationError(
                "PostgreSQL cutover is already committed; abort refuses to destroy it. "
                "Redeploy the unchanged SQLite bridge for rollback."
            )
        await target.commit()
        async with target.transaction():
            for table in TABLE_ORDER:
                await target.execute(
                    sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                        sql.Identifier(settings.schema), sql.Identifier(f"_stage_{table}")
                    )
                )
        await _set_maintenance(client, False, "migration_aborted")
        state = {"phase": "aborted", "updated_at": _iso_now(), "maintenance_active": False}
        _write_json(args.state_dir / "state.json", state)
        return state
    finally:
        await client.aclose()
        await target.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate sub2gen SQLite data to PostgreSQL 16")
    parser.add_argument(
        "command", choices=("preflight", "backfill", "cutover", "verify", "abort")
    )
    runtime_data_dir = get_runtime_data_dir()
    parser.add_argument("--sqlite", type=Path, default=runtime_data_dir / "sub2gen.db")
    parser.add_argument("--database-url", default=os.environ.get("SUB2GEN_DATABASE_URL", ""))
    parser.add_argument("--schema", default=os.environ.get("SUB2GEN_DB_SCHEMA", "sub2gen"))
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--volume-capacity-gb", type=int, default=5)
    parser.add_argument("--state-dir", type=Path, default=runtime_data_dir / "migration" / "postgres-bridge")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--drain-timeout-seconds", type=int, default=300)
    parser.add_argument("--cutover-deadline-seconds", type=int, default=720)
    return parser


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= args.retention_days <= 365:
        raise MigrationError("--retention-days must be between 1 and 365")
    if args.volume_capacity_gb < 1:
        raise MigrationError("--volume-capacity-gb must be positive")
    os.environ["SUB2GEN_DB_SCHEMA"] = args.schema
    settings = DatabaseSettings.from_env(backend="postgres", url=args.database_url)
    args.sqlite = args.sqlite.resolve()
    args.state_dir = args.state_dir.resolve()
    if args.command == "preflight":
        return await preflight(
            args.sqlite,
            settings,
            retention_days=args.retention_days,
            volume_capacity_bytes=args.volume_capacity_gb * 1024**3,
            require_empty=True,
        )
    if args.command == "backfill":
        return await backfill(args, settings)
    if args.command == "cutover":
        return await cutover(args, settings)
    if args.command == "verify":
        return await verify(args.sqlite, settings, retention_days=args.retention_days)
    return await abort(args, settings)


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_main(args))
    except (MigrationError, TimeoutError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
