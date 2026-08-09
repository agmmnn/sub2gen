"""SQLite migration runner with safe adoption of legacy databases."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Literal

from .common import MigrationError, MigrationFile, discover_migrations


MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "sqlite"
LEGACY_TABLE_SIGNATURES: dict[str, set[str]] = {
    "api_clients": {"id", "name"},
    "api_keys": {"id", "client_id", "key_hash"},
    "projects": {"id", "project_id"},
    "tasks": {"id", "task_id"},
    "tokens": {"id", "st"},
}


class SQLiteMigrationError(MigrationError):
    pass


def discover_sqlite_migrations(directory: Path = MIGRATIONS_DIR) -> list[MigrationFile]:
    return discover_migrations(directory, engine="SQLite")


async def _table_names(connection: Any) -> set[str]:
    cursor = await connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {str(row[0]) for row in await cursor.fetchall()}


async def _column_names(connection: Any, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    cursor = await connection.execute(f'PRAGMA table_info("{escaped}")')
    return {str(row[1]) for row in await cursor.fetchall()}


async def _index_names(connection: Any) -> set[str]:
    cursor = await connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
    )
    return {str(row[0]) for row in await cursor.fetchall()}


def _expected_schema(migrations: list[MigrationFile]) -> tuple[dict[str, set[str]], set[str]]:
    connection = sqlite3.connect(":memory:")
    try:
        for migration in migrations:
            connection.executescript(migration.sql_text)
        tables = {
            str(row[0]): {
                str(column[1])
                for column in connection.execute(
                    f'PRAGMA table_info("{str(row[0]).replace(chr(34), chr(34) * 2)}")'
                ).fetchall()
            }
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        return tables, indexes
    finally:
        connection.close()


async def _validate_schema(
    connection: Any,
    required_tables: dict[str, set[str]],
    *,
    required_indexes: set[str] | None = None,
    label: str,
) -> None:
    actual_tables = await _table_names(connection)
    missing_tables = sorted(set(required_tables) - actual_tables)
    missing_columns: list[str] = []
    for table in sorted(set(required_tables) & actual_tables):
        actual_columns = await _column_names(connection, table)
        for column in sorted(required_tables[table] - actual_columns):
            missing_columns.append(f"{table}.{column}")
    missing_indexes = sorted((required_indexes or set()) - await _index_names(connection))
    if missing_tables or missing_columns or missing_indexes:
        details = []
        if missing_tables:
            details.append(f"missing tables: {', '.join(missing_tables)}")
        if missing_columns:
            details.append(f"missing columns: {', '.join(missing_columns)}")
        if missing_indexes:
            details.append(f"missing indexes: {', '.join(missing_indexes)}")
        raise SQLiteMigrationError(f"Incompatible {label} SQLite schema ({'; '.join(details)})")


async def _validate_legacy_candidate(
    connection: Any,
    migrations: list[MigrationFile],
) -> None:
    expected_tables, _expected_indexes = _expected_schema(migrations)
    actual_tables = await _table_names(connection)
    recognized_tables = actual_tables & set(expected_tables)
    missing_columns: list[str] = []
    for table in sorted(recognized_tables):
        required = LEGACY_TABLE_SIGNATURES.get(table)
        if required is None:
            required = {"id"} if "id" in expected_tables[table] else set()
        actual_columns = await _column_names(connection, table)
        for column in sorted(required - actual_columns):
            missing_columns.append(f"{table}.{column}")
    if not recognized_tables or missing_columns:
        details = []
        if not recognized_tables:
            details.append("no recognized sub2gen tables")
        if missing_columns:
            details.append(f"missing identity columns: {', '.join(missing_columns)}")
        raise SQLiteMigrationError(f"Incompatible legacy SQLite schema ({'; '.join(details)})")


async def _read_applied(connection: Any) -> dict[str, str]:
    cursor = await connection.execute("SELECT revision, checksum FROM schema_migrations ORDER BY revision")
    return {str(row[0]): str(row[1]) for row in await cursor.fetchall()}


def _validate_history(applied: dict[str, str], migrations: list[MigrationFile]) -> None:
    known = {item.revision: item for item in migrations}
    unknown = sorted(set(applied) - set(known))
    if unknown:
        raise SQLiteMigrationError(f"Database contains unknown SQLite schema revisions: {', '.join(unknown)}")
    for revision, checksum in applied.items():
        if checksum != known[revision].checksum:
            raise SQLiteMigrationError(f"Checksum mismatch for SQLite migration {known[revision].path.name}")


async def _ensure_tracker(connection: Any) -> None:
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            revision TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await connection.commit()


async def _apply_pending(
    connection: Any,
    migrations: list[MigrationFile],
    applied: dict[str, str],
) -> str:
    for migration in migrations:
        if migration.revision in applied:
            continue
        revision = migration.revision.replace("'", "''")
        checksum = migration.checksum.replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            + migration.sql_text
            + "\nINSERT INTO schema_migrations (revision, checksum) "
            + f"VALUES ('{revision}', '{checksum}');\nCOMMIT;"
        )
        try:
            await connection.executescript(script)
        except Exception:
            try:
                await connection.rollback()
            except Exception:
                pass
            raise
    return migrations[-1].revision


async def prepare_sqlite_migrations(connection: Any) -> Literal["current", "fresh", "legacy"]:
    """Apply migrations to fresh/tracked databases or validate a legacy adoption candidate."""

    migrations = discover_sqlite_migrations()
    tables = await _table_names(connection)
    if "schema_migrations" in tables:
        applied = await _read_applied(connection)
        _validate_history(applied, migrations)
        await _apply_pending(connection, migrations, applied)
        return "current"
    if tables:
        await _validate_legacy_candidate(connection, migrations)
        return "legacy"

    await _ensure_tracker(connection)
    await _apply_pending(connection, migrations, {})
    return "fresh"


async def stamp_compatible_sqlite_database(connection: Any) -> str:
    """Stamp the newest compatible prefix, then execute later migrations."""

    migrations = discover_sqlite_migrations()
    final_tables, final_indexes = _expected_schema(migrations)
    compatible_count = len(migrations)
    try:
        await _validate_schema(
            connection,
            final_tables,
            required_indexes=final_indexes,
            label="existing",
        )
    except SQLiteMigrationError:
        compatible_count = 0
        last_error: SQLiteMigrationError | None = None
        for count in range(len(migrations) - 1, 0, -1):
            prefix_tables, prefix_indexes = _expected_schema(migrations[:count])
            try:
                await _validate_schema(
                    connection,
                    prefix_tables,
                    required_indexes=prefix_indexes,
                    label="existing",
                )
            except SQLiteMigrationError as exc:
                last_error = exc
                continue
            compatible_count = count
            break
        if compatible_count == 0:
            assert last_error is not None
            raise last_error

    await _ensure_tracker(connection)
    applied = await _read_applied(connection)
    _validate_history(applied, migrations)
    for migration in migrations[:compatible_count]:
        if migration.revision not in applied:
            await connection.execute(
                "INSERT INTO schema_migrations (revision, checksum) VALUES (?, ?)",
                (migration.revision, migration.checksum),
            )
            applied[migration.revision] = migration.checksum
    await connection.commit()
    revision = await _apply_pending(connection, migrations, applied)
    await _validate_schema(
        connection,
        final_tables,
        required_indexes=final_indexes,
        label="migrated",
    )
    return revision
