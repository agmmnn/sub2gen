"""PostgreSQL implementation of the existing sub2gen Database contract.

The service layer continues to call the methods implemented on ``Database``.
This subclass replaces the connection/cursor primitives and the SQLite runtime
migration hooks so the same domain methods execute against PostgreSQL during the
temporary bridge release.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from psycopg import sql
from psycopg import OperationalError as PsycopgOperationalError
from psycopg.pq import TransactionStatus
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from .database import Database
from .database_runtime import DatabaseSettings
from .models import GeminiGenAccount, RunwayAccount
from .postgres_migrations import read_database_markers, run_postgres_migrations


class DatabaseUnavailableError(RuntimeError):
    """Raised when PostgreSQL is unavailable or not ready for production use."""


class HybridRow(dict[str, Any]):
    """Dictionary row with aiosqlite-compatible positional access."""

    __slots__ = ("_ordered_values",)

    def __init__(self, columns: Sequence[str], values: Sequence[Any]):
        super().__init__(zip(columns, values))
        self._ordered_values = tuple(values)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._ordered_values[key]
        return super().__getitem__(key)


def hybrid_row_factory(cursor: Any):
    columns = [str(column.name) for column in (cursor.description or ())]

    def make_row(values: Sequence[Any]) -> HybridRow:
        compatible = tuple(
            value.isoformat() if isinstance(value, date) and not isinstance(value, datetime) else value
            for value in values
        )
        return HybridRow(columns, compatible)

    return make_row


IDENTITY_TABLES = {
    "admin_sessions",
    "api_clients",
    "api_key_accounts",
    "api_key_audit_logs",
    "api_key_rate_limits",
    "api_keys",
    "cache_files",
    "captcha_worker_keys",
    "dedicated_extension_workers",
    "extension_worker_bindings",
    "geminigen_accounts",
    "geminigen_tasks",
    "operation_stats",
    "projects",
    "request_logs",
    "runway_accounts",
    "runway_models",
    "runway_tasks",
    "tasks",
    "token_stats",
    "tokens",
}


BOOLEAN_COLUMNS = {
    "adobe_cloning_enabled",
    "adobe_metadata_enabled",
    "adobe_tracker_enabled",
    "allow_captcha",
    "allow_generation",
    "allow_session_refresh",
    "auto_enable_on_update",
    "auto_refresh_enabled",
    "browser_fallback_to_remote_browser",
    "browser_proxy_enabled",
    "cache_enabled",
    "cache_outputs",
    "dedicated_extension_enabled",
    "enabled",
    "error_ban_enabled",
    "extension_fallback_to_managed_on_dedicated_failure",
    "extension_generation_enabled",
    "image_enabled",
    "is_active",
    "is_enabled",
    "is_grok_image_max",
    "is_grok_max",
    "is_image_gen_max",
    "is_image_premium",
    "live_available",
    "log_requests",
    "log_responses",
    "mask_token",
    "media_proxy_enabled",
    "payload_available",
    "polling_mode_enabled",
    "profile_is_active",
    "proxy_enabled",
    "session_refresh_browser_first",
    "session_refresh_enabled",
    "session_refresh_fail_if_st_refresh_fails",
    "session_refresh_inject_st_cookie",
    "session_refresh_local_only",
    "session_refresh_scheduler_enabled",
    "session_refresh_update_st_from_cookie",
    "st_only_refresh_scheduler_enabled",
    "use_extension_for_generation",
    "video_enabled",
}


def _split_sql_csv(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote = ""
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
        index += 1
    parts.append(value[start:].strip())
    return parts


def _replace_qmark_placeholders(statement: str) -> str:
    output: list[str] = []
    quote = ""
    index = 0
    while index < len(statement):
        char = statement[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    output.append(statement[index + 1])
                    index += 2
                    continue
                quote = ""
            elif char == "%":
                output.append("%")
        elif char in {"'", '"'}:
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        elif char == "%":
            output.append("%%")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _translate_datetime_functions(statement: str) -> str:
    translated = statement
    translated = re.sub(
        r"DATE\(([^(),]+),\s*'localtime'\)",
        r"(\1 AT TIME ZONE 'Asia/Karachi')::date",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\(date\('now',\s*'\+1 day'\)\)",
        "date_trunc('day', CURRENT_TIMESTAMP) + INTERVAL '1 day'",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\('now',\s*'-(\d+) seconds?'\)",
        r"CURRENT_TIMESTAMP - INTERVAL '\1 seconds'",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\('now',\s*%s\)",
        r"CURRENT_TIMESTAMP + (%s)::interval",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\((COALESCE\([^)]*\))\)", r"\1", translated, flags=re.IGNORECASE
    )
    translated = re.sub(r"datetime\(([^()]+)\)", r"\1", translated, flags=re.IGNORECASE)
    translated = re.sub(
        r"DATE\('now'\)", "CURRENT_DATE", translated, flags=re.IGNORECASE
    )
    translated = re.sub(
        r"DATE\(([^()]+)\)", r"(\1)::date", translated, flags=re.IGNORECASE
    )
    translated = re.sub(
        r"strftime\('%%Y-%%m',\s*([^()]+)\)",
        r"to_char(\1, 'YYYY-MM')",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"strftime\('%%s',\s*([^()]+)\)",
        r"EXTRACT(EPOCH FROM \1)::BIGINT",
        translated,
        flags=re.IGNORECASE,
    )
    return translated


def _translate_boolean_literals(statement: str) -> str:
    translated = statement
    for column in sorted(BOOLEAN_COLUMNS, key=len, reverse=True):
        escaped = re.escape(column)
        translated = re.sub(
            rf"\b({escaped})\s*=\s*1\b", r"\1 = TRUE", translated, flags=re.IGNORECASE
        )
        translated = re.sub(
            rf"\b({escaped})\s*=\s*0\b", r"\1 = FALSE", translated, flags=re.IGNORECASE
        )
        translated = re.sub(
            rf"\b({escaped})\s*=\s*CASE\b", rf"{column} = CASE", translated, flags=re.IGNORECASE
        )
        translated = re.sub(
            rf"\bSET\s+({escaped})\s*=\s*1\b", r"SET \1 = TRUE", translated, flags=re.IGNORECASE
        )
        translated = re.sub(
            rf"\bSET\s+({escaped})\s*=\s*0\b", r"SET \1 = FALSE", translated, flags=re.IGNORECASE
        )
        translated = re.sub(
            rf"COALESCE\(\s*((?:[A-Za-z_][A-Za-z0-9_]*\.)?{escaped})\s*,\s*0\s*\)",
            r"COALESCE(\1, FALSE)",
            translated,
            flags=re.IGNORECASE,
        )
        translated = re.sub(
            rf"COALESCE\(\s*((?:[A-Za-z_][A-Za-z0-9_]*\.)?{escaped})\s*,\s*1\s*\)",
            r"COALESCE(\1, TRUE)",
            translated,
            flags=re.IGNORECASE,
        )
    return translated


def translate_sql(statement: str) -> str:
    """Translate the finite SQLite SQL dialect used by the domain methods."""
    translated = _replace_qmark_placeholders(str(statement))
    translated = _translate_datetime_functions(translated)
    translated = translated.replace("COLLATE NOCASE", "")
    translated = re.sub(r"\bIFNULL\s*\(", "COALESCE(", translated, flags=re.IGNORECASE)
    translated = re.sub(
        r"\bMAX\s*\(\s*COALESCE\(([^,]+),\s*0\),\s*(%s)\s*\)",
        r"GREATEST(COALESCE(\1, 0), \2)",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"COALESCE\(\s*((?:[A-Za-z_][A-Za-z0-9_]*\.)?today_date)\s*,\s*''\s*\)\s*!=\s*%s",
        r"\1 IS DISTINCT FROM (%s)::date",
        translated,
        flags=re.IGNORECASE,
    )
    translated = _translate_boolean_literals(translated)

    insert_values = re.search(
        r"INSERT(?:\s+OR\s+(?:IGNORE|REPLACE))?\s+INTO\s+"
        r"[A-Za-z_][A-Za-z0-9_]*\s*\((.*?)\)\s*VALUES\s*\((.*?)\)",
        translated,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if insert_values:
        columns = [part.strip().strip('"').lower() for part in _split_sql_csv(insert_values.group(1))]
        expressions = _split_sql_csv(insert_values.group(2))
        changed = False
        for index, column in enumerate(columns[: len(expressions)]):
            if column in BOOLEAN_COLUMNS and expressions[index].strip() in {"0", "1"}:
                expressions[index] = "TRUE" if expressions[index].strip() == "1" else "FALSE"
                changed = True
        if changed:
            translated = (
                translated[: insert_values.start(2)]
                + ", ".join(expressions)
                + translated[insert_values.end(2) :]
            )

    replace_match = re.search(
        r"INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*VALUES\s*\((.*?)\)\s*$",
        translated,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if replace_match:
        table = replace_match.group(1)
        columns = [part.strip() for part in _split_sql_csv(replace_match.group(2))]
        conflict = "id"
        if table == "api_key_rate_limits":
            conflict = "api_key_id, endpoint"
        updates = [
            f"{column} = EXCLUDED.{column}"
            for column in columns
            if column not in {part.strip() for part in conflict.split(",")}
        ]
        translated = (
            f"INSERT INTO {table} ({replace_match.group(2)}) VALUES ({replace_match.group(3)}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {', '.join(updates)}"
        )
    else:
        ignore = bool(re.search(r"INSERT\s+OR\s+IGNORE", translated, flags=re.IGNORECASE))
        translated = re.sub(
            r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", translated, flags=re.IGNORECASE
        )
        if ignore and " ON CONFLICT " not in translated.upper():
            translated = translated.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return translated


def _coerce_bool(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"0", "1", "true", "false"}:
        return value.strip().lower() in {"1", "true"}
    return value


def coerce_boolean_parameters(statement: str, parameters: Sequence[Any]) -> tuple[Any, ...]:
    values = list(parameters)
    placeholder_positions = [match.start() for match in re.finditer(r"%s", statement)]
    bool_indexes: set[int] = set()

    insert = re.search(
        r"INSERT\s+INTO\s+[A-Za-z_][A-Za-z0-9_]*\s*\((.*?)\)\s*VALUES\s*\((.*?)\)",
        statement,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if insert:
        columns = [part.strip().strip('"').lower() for part in _split_sql_csv(insert.group(1))]
        expressions = _split_sql_csv(insert.group(2))
        parameter_index = 0
        for column, expression in zip(columns, expressions):
            count = len(re.findall(r"%s", expression))
            if column in BOOLEAN_COLUMNS:
                bool_indexes.update(range(parameter_index, parameter_index + count))
            parameter_index += count

    update = re.search(
        r"UPDATE\s+[A-Za-z_][A-Za-z0-9_]*\s+SET\s+(.*?)(?:\s+WHERE\s+|$)",
        statement,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if update:
        parameter_index = 0
        for assignment in _split_sql_csv(update.group(1)):
            count = len(re.findall(r"%s", assignment))
            column = assignment.split("=", 1)[0].strip().strip('"').lower()
            if column in BOOLEAN_COLUMNS:
                bool_indexes.update(range(parameter_index, parameter_index + count))
            parameter_index += count

    for column in BOOLEAN_COLUMNS:
        pattern = re.compile(rf"\b{re.escape(column)}\s*=\s*%s", flags=re.IGNORECASE)
        for match in pattern.finditer(statement):
            bool_indexes.add(sum(1 for position in placeholder_positions if position < match.end()) - 1)

    for index in bool_indexes:
        if 0 <= index < len(values):
            values[index] = _coerce_bool(values[index])
    return tuple(values)


class PostgresCursorAdapter:
    def __init__(self, cursor: Any, *, lastrowid: int | None = None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount or 0)

    async def fetchone(self) -> Any:
        return await self._cursor.fetchone()

    async def fetchall(self) -> list[Any]:
        return await self._cursor.fetchall()


class PostgresConnectionAdapter:
    def __init__(self, connection: Any, owner: "PostgresDatabase"):
        self.connection = connection
        self.owner = owner
        self.row_factory = hybrid_row_factory

    @property
    def in_transaction(self) -> bool:
        return self.connection.info.transaction_status != TransactionStatus.IDLE

    async def execute(self, statement: str, parameters: Sequence[Any] = ()) -> PostgresCursorAdapter:
        translated = translate_sql(statement)
        params = coerce_boolean_parameters(translated, tuple(parameters or ()))
        insert = re.match(
            r"\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            translated,
            flags=re.IGNORECASE,
        )
        wants_identity = bool(
            insert
            and insert.group(1).lower() in IDENTITY_TABLES
            and " RETURNING " not in translated.upper()
        )
        if wants_identity:
            translated = translated.rstrip().rstrip(";") + " RETURNING id"
        started = time.perf_counter()
        try:
            cursor = self.connection.cursor(row_factory=hybrid_row_factory)
            await cursor.execute(translated, params)
            lastrowid = None
            if wants_identity:
                row = await cursor.fetchone()
                lastrowid = int(row[0]) if row else None
            self.owner._record_query(time.perf_counter() - started)
            return PostgresCursorAdapter(cursor, lastrowid=lastrowid)
        except Exception:
            self.owner._record_query(time.perf_counter() - started, failed=True)
            raise

    async def executemany(self, statement: str, parameters: Iterable[Sequence[Any]]) -> PostgresCursorAdapter:
        translated = translate_sql(statement)
        rows = [coerce_boolean_parameters(translated, tuple(row)) for row in parameters]
        started = time.perf_counter()
        try:
            cursor = self.connection.cursor(row_factory=hybrid_row_factory)
            await cursor.executemany(translated, rows)
            self.owner._record_query(time.perf_counter() - started)
            return PostgresCursorAdapter(cursor)
        except Exception:
            self.owner._record_query(time.perf_counter() - started, failed=True)
            raise

    async def commit(self) -> None:
        await self.connection.commit()

    async def rollback(self) -> None:
        await self.connection.rollback()


class PostgresDatabase(Database):
    """PostgreSQL-backed implementation of the Database public contract."""

    backend = "postgres"

    def __init__(self, url: str | None = None, *, settings: DatabaseSettings | None = None):
        self.settings = settings or DatabaseSettings.from_env(backend="postgres", url=url)
        super().__init__(db_path="")
        self.database_url = self.settings.url
        self.schema = self.settings.schema
        self.db_path = None
        self._pool: AsyncConnectionPool | None = None
        self._pool_open_lock = asyncio.Lock()
        self._pool_wait_samples: deque[float] = deque(maxlen=4096)
        self._query_duration_samples: deque[float] = deque(maxlen=4096)
        self._pool_wait_total = 0.0
        self._pool_wait_count = 0
        self._query_duration_total = 0.0
        self._query_count = 0
        self._query_errors = 0
        self._connection_errors = 0
        self.database_revision = ""
        self.cutover_marker_present = False
        self.restore_marker = ""

    def db_exists(self) -> bool:
        return True

    def enable_persistent_connections(self) -> None:
        return None

    async def _configure_pool_connection(self, connection: Any) -> None:
        await connection.execute("SET TIME ZONE 'UTC'")
        await connection.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{self.settings.statement_timeout_seconds * 1000}ms",),
        )
        await connection.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self.schema))
        )
        await connection.commit()

    async def _ensure_pool(self) -> AsyncConnectionPool:
        if self._pool is not None:
            return self._pool
        async with self._pool_open_lock:
            if self._pool is None:
                pool = AsyncConnectionPool(
                    conninfo=self.database_url,
                    min_size=self.settings.pool_min_size,
                    max_size=self.settings.pool_max_size,
                    timeout=float(self.settings.pool_timeout_seconds),
                    open=False,
                    configure=self._configure_pool_connection,
                )
                try:
                    await pool.open(wait=True, timeout=float(self.settings.pool_timeout_seconds))
                except Exception as exc:
                    self._connection_errors += 1
                    await pool.close()
                    raise DatabaseUnavailableError("database_unavailable") from exc
                self._pool = pool
        return self._pool

    @asynccontextmanager
    async def _connect(self, *, write: bool = False):
        pool = await self._ensure_pool()
        started = time.perf_counter()
        try:
            async with pool.connection(timeout=float(self.settings.pool_timeout_seconds)) as connection:
                waited = time.perf_counter() - started
                self._pool_wait_samples.append(waited)
                self._pool_wait_total += waited
                self._pool_wait_count += 1
                yield PostgresConnectionAdapter(connection, self)
        except DatabaseUnavailableError:
            raise
        except (PsycopgOperationalError, PoolTimeout) as exc:
            self._connection_errors += 1
            raise DatabaseUnavailableError("database_unavailable") from exc
        except Exception:
            self._connection_errors += 1
            raise

    def _record_query(self, duration: float, *, failed: bool = False) -> None:
        self._query_duration_samples.append(float(duration))
        self._query_duration_total += float(duration)
        self._query_count += 1
        if failed:
            self._query_errors += 1

    @staticmethod
    def _percentile(samples: deque[float], percentile: float) -> float:
        values = sorted(samples)
        if not values:
            return 0.0
        index = max(0, min(len(values) - 1, int(len(values) * percentile)))
        return float(values[index])

    def runtime_metrics(self) -> dict[str, float]:
        pool_stats: Mapping[str, Any] = self._pool.get_stats() if self._pool is not None else {}
        return {
            "pool_wait_seconds_total": float(self._pool_wait_total),
            "pool_wait_count": float(self._pool_wait_count),
            "pool_wait_seconds_p95": self._percentile(self._pool_wait_samples, 0.95),
            "query_duration_seconds_total": float(self._query_duration_total),
            "query_count": float(self._query_count),
            "query_duration_seconds_p95": self._percentile(self._query_duration_samples, 0.95),
            "query_errors": float(self._query_errors),
            "connection_errors": float(self._connection_errors),
            "pool_available": float(pool_stats.get("pool_available", 0) or 0),
            "pool_size": float(pool_stats.get("pool_size", 0) or 0),
            "pool_requests_waiting": float(pool_stats.get("requests_waiting", 0) or 0),
        }

    async def init_db(self) -> None:
        pool = await self._ensure_pool()
        async with pool.connection(timeout=float(self.settings.pool_timeout_seconds)) as connection:
            self.database_revision = await run_postgres_migrations(connection, self.schema)
            markers = await read_database_markers(connection, self.schema)
        self.cutover_marker_present = bool(markers.get("cutover_completed_at"))
        self.restore_marker = markers.get("last_restore_at", "")
        if self.settings.require_cutover_marker and not self.cutover_marker_present:
            raise DatabaseUnavailableError("postgres_cutover_marker_missing")

    async def check_and_migrate_db(self, config_dict: dict = None) -> None:
        if not self.database_revision:
            await self.init_db()

    async def _configure_write_pragmas(self, db: Any) -> None:
        return None

    async def cache_schema_capabilities(self) -> None:
        capabilities: dict[str, set[str]] = {}
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = ?
                ORDER BY table_name, ordinal_position
                """,
                (self.schema,),
            )
            for row in await cursor.fetchall():
                capabilities.setdefault(str(row["table_name"]), set()).add(str(row["column_name"]))
        self._schema_capabilities = capabilities

    async def _table_exists(self, db: Any, table_name: str) -> bool:
        cursor = await db.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = ? AND table_name = ?
            """,
            (self.schema, table_name),
        )
        return await cursor.fetchone() is not None

    async def close_runtime_connections(self) -> None:
        self._schema_capabilities = {}
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def optimize_after_retention(self) -> None:
        async with self._connect(write=True) as db:
            for table in ("request_logs", "api_key_audit_logs", "geminigen_tasks"):
                await db.execute(f"ANALYZE {table}")
            await db.commit()

    async def acquire_runway_account(self) -> RunwayAccount | None:
        """Atomically reserve a Runway slot across pooled PostgreSQL workers."""
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                SELECT * FROM runway_accounts
                WHERE is_active = 1
                  AND TRIM(COALESCE(raw_credential, '')) != ''
                  AND (concurrency_limit < 0 OR in_flight < concurrency_limit)
                ORDER BY COALESCE(last_used_at, '1970-01-01 00:00:00') ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            row = await cursor.fetchone()
            if not row:
                return None
            account = RunwayAccount(**dict(row))
            await db.execute(
                """
                UPDATE runway_accounts
                SET in_flight = in_flight + 1,
                    last_used_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (account.id,),
            )
            await db.commit()
            account.in_flight += 1
            return account

    async def acquire_geminigen_account(
        self,
        kind: str,
        excluded_account_ids: list[int] | None = None,
        endpoint_type: str | None = None,
    ) -> GeminiGenAccount | None:
        """Atomically reserve a GeminiGen account using row locks and skip-locked."""
        is_video = str(kind or "").lower() == "video"
        limit_col = "video_concurrency" if is_video else "image_concurrency"
        inflight_col = "video_in_flight" if is_video else "image_in_flight"
        daily_limit_col = self._geminigen_daily_limit_column(kind, endpoint_type)
        excluded = [int(value) for value in (excluded_account_ids or []) if int(value) > 0]
        exclusion_clause = ""
        parameters: list[Any] = []
        if excluded:
            exclusion_clause = f"AND id NOT IN ({', '.join('?' for _ in excluded)})"
            parameters.extend(excluded)
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                f"""
                SELECT * FROM geminigen_accounts
                WHERE is_active = 1
                  AND TRIM(COALESCE(bearer_token, '')) != ''
                  AND ({daily_limit_col} IS NULL OR {daily_limit_col} <= CURRENT_TIMESTAMP)
                  AND ({limit_col} < 0 OR {inflight_col} < {limit_col})
                  {exclusion_clause}
                ORDER BY COALESCE(last_used_at, '1970-01-01 00:00:00') ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                parameters,
            )
            row = await cursor.fetchone()
            if not row:
                return None
            account = GeminiGenAccount(**dict(row))
            await db.execute(
                f"""
                UPDATE geminigen_accounts
                SET {inflight_col} = {inflight_col} + 1,
                    last_used_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (account.id,),
            )
            await db.commit()
            if is_video:
                account.video_in_flight += 1
            else:
                account.image_in_flight += 1
            return account

    async def _get_or_create_api_client(self, client_name: str) -> int:
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO api_clients (name, is_active)
                VALUES (?, TRUE)
                ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                (client_name,),
            )
            row = await cursor.fetchone()
            await db.commit()
            return int(row["id"])

    async def health_snapshot(self) -> dict[str, Any]:
        started = time.perf_counter()
        pool = await self._ensure_pool()
        async with pool.connection(timeout=float(self.settings.pool_timeout_seconds)) as connection:
            async with connection.cursor(row_factory=hybrid_row_factory) as cursor:
                await cursor.execute(
                    """
                    SELECT
                        current_setting('server_version_num')::BIGINT AS server_version_num,
                        pg_database_size(current_database())::BIGINT AS database_size_bytes
                    """
                )
                row = await cursor.fetchone()
            markers = await read_database_markers(connection, self.schema)
        revision = markers.get("schema_revision") or self.database_revision
        return {
            "database_backend": "postgres",
            "database_ready": bool(revision) and (
                self.cutover_marker_present or not self.settings.require_cutover_marker
            ),
            "database_revision": revision,
            "cutover_marker_present": bool(markers.get("cutover_completed_at")),
            "restore_marker": markers.get("last_restore_at") or None,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "server_version_num": int(row["server_version_num"]),
            "database_size_bytes": int(row["database_size_bytes"]),
            "pool": self.runtime_metrics(),
        }
