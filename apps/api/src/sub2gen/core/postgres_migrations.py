"""Checksummed PostgreSQL SQL migration runner."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row

from ..persistence.migrations.common import (
    MigrationError,
    MigrationFile,
    discover_migrations as discover_migration_files,
)


MIGRATION_LOCK_ID = 0x5355423247454E  # stable "SUB2GEN" advisory lock
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "postgres"


class PostgresMigrationError(MigrationError):
    pass


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[MigrationFile]:
    try:
        return discover_migration_files(directory, engine="PostgreSQL")
    except MigrationError as exc:
        raise PostgresMigrationError(str(exc)) from exc


def baseline_schema_signature(migration: MigrationFile) -> tuple[dict[str, set[str]], set[str]]:
    """Extract the deterministic table/column/index contract from the initial migration."""

    tables: dict[str, set[str]] = {}
    indexes: set[str] = set()
    current_table: str | None = None
    for line in migration.sql_text.splitlines():
        table_match = re.match(r'^CREATE TABLE\s+"?([A-Za-z_][A-Za-z0-9_]*)"?\s*\($', line.strip())
        if table_match:
            current_table = table_match.group(1)
            tables[current_table] = set()
            continue
        if current_table is not None:
            if line.strip() == ");":
                current_table = None
                continue
            column_match = re.match(r'^\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s+[A-Za-z]', line)
            if column_match and column_match.group(1).upper() not in {
                "CONSTRAINT",
                "FOREIGN",
                "PRIMARY",
                "UNIQUE",
            }:
                tables[current_table].add(column_match.group(1))
            continue
        index_match = re.match(
            r'^CREATE\s+(?:UNIQUE\s+)?INDEX\s+"?([A-Za-z_][A-Za-z0-9_]*)"?',
            line.strip(),
        )
        if index_match:
            indexes.add(index_match.group(1))
    if not tables:
        raise PostgresMigrationError(f"Unable to read schema contract from PostgreSQL migration {migration.path.name}")
    return tables, indexes


async def _postgres_schema_signature(
    connection: Any,
    schema: str,
) -> tuple[dict[str, set[str]], set[str]]:
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = %s
            ORDER BY table_name, ordinal_position
            """,
            (schema,),
        )
        rows = await cursor.fetchall()
        await cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
            (schema,),
        )
        index_rows = await cursor.fetchall()
    tables: dict[str, set[str]] = {}
    for row in rows:
        tables.setdefault(str(row["table_name"]), set()).add(str(row["column_name"]))
    return tables, {str(row["indexname"]) for row in index_rows}


async def _validate_existing_postgres_schema(
    connection: Any,
    schema: str,
    baseline: MigrationFile,
) -> None:
    expected_tables, expected_indexes = baseline_schema_signature(baseline)
    actual_tables, actual_indexes = await _postgres_schema_signature(connection, schema)
    missing_tables = sorted(set(expected_tables) - set(actual_tables))
    missing_columns = [
        f"{table}.{column}"
        for table in sorted(set(expected_tables) & set(actual_tables))
        for column in sorted(expected_tables[table] - actual_tables[table])
    ]
    missing_indexes = sorted(expected_indexes - actual_indexes)
    if missing_tables or missing_columns or missing_indexes:
        details = []
        if missing_tables:
            details.append(f"missing tables: {', '.join(missing_tables)}")
        if missing_columns:
            details.append(f"missing columns: {', '.join(missing_columns)}")
        if missing_indexes:
            details.append(f"missing indexes: {', '.join(missing_indexes)}")
        raise PostgresMigrationError(f"Incompatible existing PostgreSQL schema ({'; '.join(details)})")


async def run_postgres_migrations(connection: Any, schema: str) -> str:
    migrations = discover_migrations()
    await connection.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
    try:
        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        await connection.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        actual_tables, _actual_indexes = await _postgres_schema_signature(connection, schema)
        adopting_existing = bool(actual_tables) and "schema_migrations" not in actual_tables
        if adopting_existing:
            await _validate_existing_postgres_schema(connection, schema, migrations[0])
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                revision TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute("SELECT revision, checksum FROM schema_migrations ORDER BY revision")
            rows = await cursor.fetchall()
        applied = {str(row["revision"]): str(row["checksum"]) for row in rows}
        if adopting_existing:
            baseline = migrations[0]
            await connection.execute(
                "INSERT INTO schema_migrations (revision, checksum) VALUES (%s, %s)",
                (baseline.revision, baseline.checksum),
            )
            applied[baseline.revision] = baseline.checksum
        known = {item.revision for item in migrations}
        unknown = sorted(set(applied) - known)
        if unknown:
            raise PostgresMigrationError(f"Database contains unknown schema revisions: {', '.join(unknown)}")
        for migration in migrations:
            existing = applied.get(migration.revision)
            if existing and existing != migration.checksum:
                raise PostgresMigrationError(f"Checksum mismatch for PostgreSQL migration {migration.path.name}")
            if existing:
                continue
            async with connection.transaction():
                await connection.execute(migration.sql_text)
                await connection.execute(
                    "INSERT INTO schema_migrations (revision, checksum) VALUES (%s, %s)",
                    (migration.revision, migration.checksum),
                )
        await connection.execute(
            """
            INSERT INTO system_metadata (key, value, updated_at)
            VALUES ('schema_revision', %s, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (migrations[-1].revision,),
        )
        await connection.commit()
        return migrations[-1].revision
    except Exception:
        await connection.rollback()
        raise
    finally:
        try:
            await connection.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
            await connection.commit()
        except Exception:
            await connection.rollback()


async def read_database_markers(connection: Any, schema: str) -> dict[str, str]:
    await connection.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute("SELECT key, value FROM system_metadata")
        rows = await cursor.fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}
