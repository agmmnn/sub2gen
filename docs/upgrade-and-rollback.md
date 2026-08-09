# Upgrade and rollback

## Before upgrading

1. Stop `sub2gen` so SQLite and local artifact writes are quiescent.
2. Copy `.runtime/data` and the configured cache directory to a timestamped backup.
3. For PostgreSQL, create a schema-scoped dump:

   ```bash
   pg_dump --schema=sub2gen --format=custom --file=sub2gen-before-upgrade.dump "$SUB2GEN_DATABASE_URL"
   ```

4. Record the current Git revision and run `uv run sub2gen doctor --json`.
5. Update the checkout, run `uv sync --all-packages`, then `uv run setup`.
6. Start with `uv run sub2gen`; migrations run before the API begins serving traffic.

## Rollback boundary

Application commits can be rolled back directly only when the newer revision has not
written a newer schema. Once a schema migration has run, restore the matching
pre-upgrade database backup before starting the older application revision. Migration
files are forward-only; manually deleting rows from `schema_migrations` is not a
rollback.

Worker protocol v1 is the only supported wire protocol. Roll back the API and paired
workers/extensions as one versioned set when a release changes protocol schemas.
Generated artifacts are independent files, but job/artifact metadata must come from the
same database backup to preserve ownership and delivery links.

## Verification after upgrade or restore

```bash
uv run sub2gen doctor
uv run pytest -q
bun run check
```

Confirm that provider accounts retain their credential bindings, paired workers appear
online, and a small generation reaches the expected provider and billing pool.
