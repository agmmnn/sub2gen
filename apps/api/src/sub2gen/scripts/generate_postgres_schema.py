"""Generate the deterministic initial PostgreSQL SQL migration.

This developer tool initializes a disposable current-version SQLite database,
reads its catalog, and emits the PostgreSQL 16 schema used by the bridge. It is
not used at application startup or during production cutover.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import tempfile
from pathlib import Path

from ..core.database import Database
from ..core.postgres_database import BOOLEAN_COLUMNS


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PACKAGE_ROOT / "migrations" / "postgres" / "0001_initial.sql"
LEGACY_DURABLE_COLUMNS = {
    "generation_config": [
        ("sub2gen_market_backend", "TEXT DEFAULT 'gemini_native'"),
        ("sub2gen_market_provider_order", "TEXT DEFAULT ''"),
        ("sub2gen_market_enabled_providers", "TEXT DEFAULT ''"),
        ("sub2gen_market_provider_retry_count", "INTEGER DEFAULT 1"),
        ("sub2gen_market_model", "TEXT DEFAULT 'gemini-2.5-flash'"),
        ("sub2gen_market_enabled_models", "TEXT DEFAULT ''"),
        ("sub2gen_market_primary_model", "TEXT DEFAULT ''"),
        ("sub2gen_market_fallback_models", "TEXT DEFAULT ''"),
    ],
}


def _quote(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _postgres_type(name: str, declared: str) -> str:
    declared_upper = str(declared or "").upper()
    lowered = name.lower()
    if declared_upper == "BOOLEAN" or lowered in BOOLEAN_COLUMNS:
        return "BOOLEAN"
    if "TIMESTAMP" in declared_upper or "DATETIME" in declared_upper:
        return "TIMESTAMPTZ"
    if declared_upper == "DATE":
        return "DATE"
    if any(token in declared_upper for token in ("REAL", "FLOAT", "DOUBLE")):
        return "DOUBLE PRECISION"
    if "INT" in declared_upper:
        return (
            "BIGINT"
            if lowered == "id"
            or lowered.endswith("_id")
            or lowered in {"created_at", "expires_at", "last_used_at"}
            else "INTEGER"
        )
    if "BLOB" in declared_upper:
        return "BYTEA"
    return "TEXT"


def _postgres_default(default: object, pg_type: str) -> str | None:
    if default is None:
        return None
    value = str(default).strip()
    if pg_type == "BOOLEAN":
        if value.strip("()'").lower() in {"1", "true"}:
            return "TRUE"
        if value.strip("()'").lower() in {"0", "false"}:
            return "FALSE"
    if value.upper() == "CURRENT_TIMESTAMP":
        return "CURRENT_TIMESTAMP"
    return value


def _identity_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ).fetchall()
    return {
        str(name)
        for name, ddl in rows
        if re.search(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", str(ddl), re.IGNORECASE)
    }


def _generate(connection: sqlite3.Connection) -> str:
    table_names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    identities = _identity_tables(connection)
    statements: list[str] = [
        "-- Generated from the current sub2gen bridge schema. Do not edit in place.",
        "-- Add a new checksummed migration for subsequent schema changes.",
        "",
        "CREATE TABLE system_metadata (",
        "    key TEXT PRIMARY KEY,",
        "    value TEXT NOT NULL,",
        "    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        ");",
        "",
    ]
    foreign_keys: list[str] = []
    indexes: list[str] = []
    singleton_tables: list[str] = []

    for table in table_names:
        columns = connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
        unique_sets: list[tuple[str, ...]] = []
        for index_row in connection.execute(f"PRAGMA index_list({_quote(table)})").fetchall():
            index_name = str(index_row[1])
            is_unique = bool(index_row[2])
            index_columns = tuple(
                str(row[2])
                for row in connection.execute(f"PRAGMA index_xinfo({_quote(index_name)})").fetchall()
                if int(row[5]) == 1 and row[2] is not None
            )
            if not index_columns:
                continue
            if is_unique:
                unique_sets.append(index_columns)
            elif not index_name.startswith("sqlite_autoindex"):
                indexes.append(
                    f"CREATE INDEX {_quote(index_name)} ON {_quote(table)} "
                    f"({', '.join(_quote(column) for column in index_columns)});"
                )

        definitions: list[str] = []
        primary_columns = [str(row[1]) for row in columns if int(row[5]) > 0]
        for _cid, name, declared, not_null, default, primary_order in columns:
            column_name = str(name)
            pg_type = _postgres_type(column_name, str(declared or ""))
            if table in identities and column_name == "id":
                definition = f"{_quote(column_name)} BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
            else:
                definition = f"{_quote(column_name)} {pg_type}"
                if len(primary_columns) == 1 and int(primary_order) > 0:
                    definition += " PRIMARY KEY"
                if bool(not_null):
                    definition += " NOT NULL"
                pg_default = _postgres_default(default, pg_type)
                if pg_default is not None:
                    definition += f" DEFAULT {pg_default}"
            definitions.append(definition)
        present_columns = {str(row[1]) for row in columns}
        for legacy_name, legacy_definition in LEGACY_DURABLE_COLUMNS.get(table, []):
            if legacy_name not in present_columns:
                definitions.append(f"{_quote(legacy_name)} {legacy_definition}")
        if len(primary_columns) > 1:
            definitions.append(
                f"PRIMARY KEY ({', '.join(_quote(column) for column in primary_columns)})"
            )
        for unique_columns in unique_sets:
            if unique_columns == tuple(primary_columns):
                continue
            definitions.append(
                f"UNIQUE ({', '.join(_quote(column) for column in unique_columns)})"
            )

        statements.append(f"CREATE TABLE {_quote(table)} (")
        statements.append("    " + ",\n    ".join(definitions))
        statements.extend([");", ""])

        if any(str(row[1]) == "id" and str(row[4]) == "1" for row in columns):
            singleton_tables.append(table)

        for fk in connection.execute(f"PRAGMA foreign_key_list({_quote(table)})").fetchall():
            _id, _seq, target_table, source_column, target_column, on_update, on_delete, _match = fk
            constraint = f"fk_{table}_{source_column}_{target_table}"
            clause = (
                f"ALTER TABLE {_quote(table)} ADD CONSTRAINT {_quote(constraint)} "
                f"FOREIGN KEY ({_quote(source_column)}) REFERENCES {_quote(target_table)} "
                f"({_quote(target_column)})"
            )
            if str(on_delete).upper() != "NO ACTION":
                clause += f" ON DELETE {str(on_delete).upper()}"
            if str(on_update).upper() != "NO ACTION":
                clause += f" ON UPDATE {str(on_update).upper()}"
            foreign_keys.append(clause + ";")

    # Older production databases can contain these dedicated worker records even
    # though current installations no longer create the legacy table. Keep it in
    # the import target so a cutover never silently discards durable credentials.
    if "dedicated_extension_workers" not in table_names:
        statements.extend(
            [
                'CREATE TABLE "dedicated_extension_workers" (',
                '    "id" BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,',
                '    "worker_key_prefix" TEXT NOT NULL UNIQUE,',
                '    "worker_key_hash" TEXT NOT NULL UNIQUE,',
                '    "label" TEXT DEFAULT \'\',',
                '    "token_id" BIGINT,',
                '    "route_key" TEXT,',
                '    "last_instance_id" TEXT,',
                '    "is_active" BOOLEAN DEFAULT TRUE,',
                '    "last_seen_at" TIMESTAMPTZ,',
                '    "last_error" TEXT,',
                '    "allow_captcha" BOOLEAN NOT NULL DEFAULT TRUE,',
                '    "allow_session_refresh" BOOLEAN NOT NULL DEFAULT TRUE,',
                '    "worker_registration_secret" TEXT,',
                '    "worker_key_plaintext" TEXT,',
                '    "allow_generation" BOOLEAN NOT NULL DEFAULT FALSE,',
                '    "created_at" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,',
                '    "updated_at" TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP',
                ');',
                '',
            ]
        )
        foreign_keys.append(
            'ALTER TABLE "dedicated_extension_workers" ADD CONSTRAINT '
            '"fk_dedicated_extension_workers_token_id_tokens" FOREIGN KEY '
            '("token_id") REFERENCES "tokens" ("id");'
        )
        indexes.append(
            'CREATE INDEX "idx_dedicated_extension_workers_token_id" ON '
            '"dedicated_extension_workers" ("token_id");'
        )

    statements.extend(foreign_keys)
    statements.append("")
    statements.extend(sorted(set(indexes)))
    statements.append("")
    for table in sorted(set(singleton_tables)):
        statements.append(
            f"INSERT INTO {_quote(table)} (id) VALUES (1) ON CONFLICT (id) DO NOTHING;"
        )
    statements.extend(
        [
            "",
            "INSERT INTO system_metadata (key, value) VALUES ('schema_revision', '0001')",
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP;",
            "",
        ]
    )
    return "\n".join(statements)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sub2gen-pg-schema-") as temporary:
        sqlite_path = Path(temporary) / "sub2gen.db"
        database = Database(str(sqlite_path))
        await database.init_db()
        await database.check_and_migrate_db({})
        await database.close_runtime_connections()
        connection = sqlite3.connect(sqlite_path)
        try:
            output = _generate(connection)
        finally:
            connection.close()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(output, encoding="utf-8", newline="\n")
    print(f"Generated {OUTPUT} ({len(output.splitlines())} lines)")


if __name__ == "__main__":
    asyncio.run(main())
