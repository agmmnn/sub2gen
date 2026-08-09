from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from sub2gen.core.database import Database
from sub2gen.persistence.migrations.sqlite import (
    SQLiteMigrationError,
    discover_sqlite_migrations,
    prepare_sqlite_migrations,
    stamp_compatible_sqlite_database,
)


@pytest.mark.asyncio
async def test_fresh_sqlite_database_applies_checksummed_baseline(tmp_path) -> None:
    path = tmp_path / "fresh.db"
    async with aiosqlite.connect(path) as connection:
        state = await prepare_sqlite_migrations(connection)
        tracker = await connection.execute_fetchall("SELECT revision, checksum FROM schema_migrations")
        tables = await connection.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")

    migrations = discover_sqlite_migrations()
    assert state == "fresh"
    assert tracker == [(migration.revision, migration.checksum) for migration in migrations]
    assert {row[0] for row in tables} >= {"tokens", "projects", "schema_migrations"}


@pytest.mark.asyncio
async def test_partial_legacy_sqlite_schema_fails_with_diagnostic(tmp_path) -> None:
    path = tmp_path / "partial.db"
    async with aiosqlite.connect(path) as connection:
        await connection.execute("CREATE TABLE tokens (id INTEGER PRIMARY KEY)")
        await connection.commit()

        with pytest.raises(SQLiteMigrationError, match="missing identity columns: tokens.st"):
            await prepare_sqlite_migrations(connection)


@pytest.mark.asyncio
async def test_compatible_existing_sqlite_schema_is_validated_before_stamp(tmp_path) -> None:
    path = tmp_path / "existing.db"
    async with aiosqlite.connect(path) as connection:
        await prepare_sqlite_migrations(connection)
        await connection.execute("DROP TABLE schema_migrations")
        await connection.commit()

        assert await prepare_sqlite_migrations(connection) == "legacy"
        revision = await stamp_compatible_sqlite_database(connection)
        tracker = await connection.execute_fetchall("SELECT revision, checksum FROM schema_migrations")

    assert revision == discover_sqlite_migrations()[-1].revision
    assert tracker == [
        (migration.revision, migration.checksum)
        for migration in discover_sqlite_migrations()
    ]


@pytest.mark.asyncio
async def test_sqlite_migration_checksum_drift_is_rejected(tmp_path) -> None:
    path = tmp_path / "drift.db"
    async with aiosqlite.connect(path) as connection:
        await prepare_sqlite_migrations(connection)
        await connection.execute("UPDATE schema_migrations SET checksum = 'changed' WHERE revision = '0001'")
        await connection.commit()

        with pytest.raises(SQLiteMigrationError, match="Checksum mismatch"):
            await prepare_sqlite_migrations(connection)


@pytest.mark.asyncio
async def test_database_startup_records_sqlite_revision(tmp_path) -> None:
    path = tmp_path / "application.db"
    database = Database(str(path))
    await database.init_db()

    with sqlite3.connect(path) as connection:
        tracker = connection.execute("SELECT revision FROM schema_migrations ORDER BY revision").fetchall()

    assert database.database_revision == discover_sqlite_migrations()[-1].revision
    assert tracker == [(migration.revision,) for migration in discover_sqlite_migrations()]
    assert (await database.health_snapshot())["database_revision"] == database.database_revision


@pytest.mark.asyncio
async def test_identity_migration_disables_old_managed_key_prefixes(tmp_path) -> None:
    path = tmp_path / "identity.db"
    async with aiosqlite.connect(path) as connection:
        migrations = discover_sqlite_migrations()
        baseline = migrations[0]
        await connection.executescript(baseline.sql_text)
        await connection.execute(
            """
            CREATE TABLE schema_migrations (
                revision TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await connection.execute(
            "INSERT INTO schema_migrations (revision, checksum) VALUES (?, ?)",
            (baseline.revision, baseline.checksum),
        )
        await connection.execute("INSERT INTO api_clients (name) VALUES ('Identity test')")
        await connection.execute(
            """
            INSERT INTO api_keys (client_id, label, key_prefix, key_hash, is_active)
            VALUES (1, 'old', 'old_live_example', 'old-hash', 1),
                   (1, 'current', 's2g_live_example', 'current-hash', 1)
            """
        )
        await connection.commit()

        assert await prepare_sqlite_migrations(connection) == "current"
        rows = await connection.execute_fetchall(
            "SELECT label, is_active FROM api_keys ORDER BY label"
        )

    assert rows == [("current", 1), ("old", 0)]


@pytest.mark.asyncio
async def test_tracked_0002_database_upgrades_to_unified_provider_schema(tmp_path) -> None:
    path = tmp_path / "upgrade-0002.db"
    migrations = discover_sqlite_migrations()
    assert [migration.revision for migration in migrations] == ["0001", "0002", "0003", "0004", "0005"]

    async with aiosqlite.connect(path) as connection:
        await connection.executescript(migrations[0].sql_text)
        await connection.executescript(migrations[1].sql_text)
        await connection.execute(
            """
            CREATE TABLE schema_migrations (
                revision TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await connection.executemany(
            "INSERT INTO schema_migrations (revision, checksum) VALUES (?, ?)",
            [(migration.revision, migration.checksum) for migration in migrations[:2]],
        )
        await connection.execute("INSERT INTO api_clients (name) VALUES ('Preserved client')")
        await connection.commit()

        assert await prepare_sqlite_migrations(connection) == "current"
        tracker = await connection.execute_fetchall(
            "SELECT revision, checksum FROM schema_migrations ORDER BY revision"
        )
        client = await connection.execute_fetchall("SELECT name FROM api_clients")
        provider_tables = await connection.execute_fetchall(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name IN (
                'provider_accounts', 'credential_bindings', 'worker_devices',
                'generation_jobs', 'generation_attempts', 'generation_artifacts',
                'provider_account_api_keys'
            )
            ORDER BY name
            """
        )

    assert tracker == [(migration.revision, migration.checksum) for migration in migrations]
    assert client == [("Preserved client",)]
    assert [row[0] for row in provider_tables] == [
        "credential_bindings",
        "generation_artifacts",
        "generation_attempts",
        "generation_jobs",
        "provider_account_api_keys",
        "provider_accounts",
        "worker_devices",
    ]
