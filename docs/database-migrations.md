# Database migrations

sub2gen applies ordered, checksummed SQL migrations automatically during application startup. SQLite and PostgreSQL both record applied revisions in `schema_migrations`; operators do not need to run a separate migration command.

## Startup behavior

For a new database, sub2gen creates the migration tracker and applies every SQL file in revision order. For an existing database without migration history, it uses a guarded adoption path:

- SQLite first verifies that the file contains a recognizable legacy sub2gen schema. The legacy compatibility upgrader runs, the complete current schema is validated, and only then is the baseline stamped.
- PostgreSQL validates the existing tables, columns, and indexes against the initial migration before stamping it. Missing or partial schemas fail startup with an `Incompatible existing PostgreSQL schema` diagnostic.

An unknown revision or a changed checksum always stops startup. Never edit an applied migration; add a new numbered SQL file instead.

## Backup before an upgrade

Stop application writers before taking a SQLite filesystem copy. Copy the database and its `-wal` and `-shm` sidecars together, or use the dashboard database backup while the application is running.

For PostgreSQL, use sub2gen's encrypted database backup workflow or a PostgreSQL 16 `pg_dump` taken from the same deployment credentials. Confirm that the backup can be decrypted or listed before upgrading.

Keep the backup outside the runtime volume being upgraded. Record the application commit and the latest migration revision alongside it.

## Inspect migration state

SQLite:

```sql
SELECT revision, checksum, applied_at
FROM schema_migrations
ORDER BY revision;
```

PostgreSQL, when using the default schema:

```sql
SELECT revision, checksum, applied_at
FROM sub2gen.schema_migrations
ORDER BY revision;
```

## Failure recovery and rollback

Do not manually insert a migration revision after a failure. Preserve the error, restore the pre-upgrade backup, and restart the previous known-good application commit. If a migration was applied successfully but the new application must be rolled back, restore the backup unless the migration explicitly documents backward compatibility with the previous commit.

Migrations are forward-only. A rollback means restoring durable data, not editing `schema_migrations` or reversing SQL by hand. A database that fails schema validation is left unstamped so it cannot be mistaken for a completed upgrade.
