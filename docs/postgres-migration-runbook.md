# PostgreSQL 16 migration runbook

This runbook moves sub2gen from the SQLite bridge to a separate Railway PostgreSQL 16 service. Redis remains the hot-state and maintenance coordinator. Deploying the bridge code does not migrate data or change the production backend; final cutover is a separate operator action.

## Safety invariants

- Keep one SFO sub2gen replica throughout migration.
- Keep `SUB2GEN_DATABASE_BACKEND=sqlite` until final verified cutover.
- Never delete or modify `sub2gen.db` during backfill or cutover. Retain it unchanged for seven days afterward.
- Never run cutover without a verified encrypted Google Drive pre-change backup.
- Keep every encryption key referenced by retained backups in Railway and an offline password manager.
- Remove the PostgreSQL public TCP proxy after testing. sub2gen uses `${{Postgres.DATABASE_URL}}` over Railway private networking.
- Missing PostgreSQL schema/cutover markers or a missing Redis state marker keep readiness fail-closed.

## Railway service

Provision a service named `Postgres` in production:

- image `postgres:16-alpine`;
- SFO (`us-west` in the CLI), one replica;
- 1 GB memory limit;
- 5 GB volume at `/var/lib/postgresql/data`;
- `PGDATA=/var/lib/postgresql/data/pgdata`;
- private `DATABASE_URL` assembled from the private domain and PostgreSQL credentials.

Railway Hobby provides a 5 GB default volume but not scheduled volume backups. sub2gen therefore creates encrypted daily PostgreSQL backups in Google Drive and retains 14 automatic copies.

## Bridge variables

```text
SUB2GEN_DATABASE_BACKEND=sqlite
SUB2GEN_DATABASE_URL=${{Postgres.DATABASE_URL}}
SUB2GEN_DB_SCHEMA=sub2gen
SUB2GEN_DB_POOL_MIN_SIZE=2
SUB2GEN_DB_POOL_MAX_SIZE=10
SUB2GEN_DB_POOL_TIMEOUT_SECONDS=5
SUB2GEN_DB_STATEMENT_TIMEOUT_SECONDS=30
SUB2GEN_REQUIRE_CUTOVER_MARKER=true
SUB2GEN_TMP_DIR=/tmp/sub2gen
```

`SUB2GEN_TMP_DIR` must point to ephemeral container storage with enough free space for the SQLite snapshot, plaintext archive, encrypted archive, and restore extraction. Do not stage these multi-gigabyte files on the 5 GB persistent application volume.

Create a 32-byte encryption key offline, then store:

```text
SUB2GEN_BACKUP_ACTIVE_KEY_ID=2026-08
SUB2GEN_BACKUP_KEYS_JSON={"2026-08":"BASE64_ENCODED_32_BYTE_KEY"}
SUB2GEN_AUTO_RESTART_AFTER_RESTORE=true
```

Key rotation adds a key and changes the active ID. Do not remove an old key while a retained backup references it.

## Migration CLI

```text
uv run python -m sub2gen.scripts.migrate_sqlite_to_postgres preflight
uv run python -m sub2gen.scripts.migrate_sqlite_to_postgres backfill
uv run python -m sub2gen.scripts.migrate_sqlite_to_postgres cutover --confirm CUTOVER
uv run python -m sub2gen.scripts.migrate_sqlite_to_postgres verify
uv run python -m sub2gen.scripts.migrate_sqlite_to_postgres abort --confirm ABORT
```

Shared options include `--sqlite`, `--database-url`, `--schema`, `--retention-days`, `--volume-capacity-gb`, and `--state-dir`. Defaults are `.runtime/data/sub2gen.db`, `SUB2GEN_DATABASE_URL`, `sub2gen`, seven days, 5 GB, and `.runtime/data/migration/postgres-bridge`.

### Preflight and backfill

Preflight applies only the checksummed target schema, then validates SQLite integrity/foreign keys, required and unknown tables/columns, PostgreSQL major version 16, schema revision, target emptiness, retention scope, and projected use below 60% of the volume. If projected usage exceeds 3 GB on the initial volume, resize before continuing.

Backfill creates a consistent snapshot, writes through PostgreSQL `COPY` into unlogged staging tables, reconciles transactionally, resets identities, and verifies counts and deterministic hashes. It does not enable maintenance or write the final cutover marker. Run a production-sized dry run and compare application-level reads for one normal traffic cycle while SQLite remains authoritative.

### Data scope

The importer preserves all configuration, credentials, clients, API keys, assignments, rate configuration, tokens, projects, statistics, cache metadata, provider accounts/models, worker bindings, admin sessions, and queued/active tasks.

Only seven days of terminal task, request-summary, API-key audit, and Redis-persistence history are imported. Historical full request/response bodies are never imported. Summaries are re-redacted and capped at 1 KB. Terminal provider payloads are removed; active recovery payloads remain intact. Unknown schema elements, foreign-key violations, and uniqueness violations abort migration.

The target schema includes the additive `0003` provider-account, credential-binding,
worker-device, generation-job, and generation-attempt tables. Backfill preserves rows
already present in those tables and keeps legacy provider credentials in their original
tables; it never materializes an opaque credential locator as secret content. The five
tables use text public IDs, so the identity-reset step intentionally skips them.

## Encrypted pre-change backup

Before maintenance, create a manual pre-change backup from the admin Google Drive controls and wait for completion. PostgreSQL backups contain a PostgreSQL 16 custom-format dump, browser profiles, schema/cutover metadata, row counts, and checksums. The dump and manifest row counts use the same exported repeatable-read snapshot, so restore verification remains exact even when normal traffic is writing concurrently. The complete archive is streamed through AES-256-GCM before upload. Keys are never stored in the archive or database.

Download and validate a backup in staging before authorizing production cutover.

## Final cutover

1. Confirm the bridge is in SQLite mode and Redis required mode is healthy.
2. Confirm the encrypted pre-change backup is verified.
3. Run `cutover --confirm CUTOVER` inside the bridge container so private PostgreSQL and Redis URLs resolve.
4. The CLI sets `sub2gen:maintenance`, blocks submissions/admin mutations, and waits up to five minutes for Redis in-flight counters.
5. It takes the final snapshot and performs staging, reverse-order stale deletion, forward-order upsert, identity reset, verification, and cutover-marker write transactionally.
6. Final reconciliation has a 12-minute deadline, leaving three minutes for deployment rollback.
7. Deploy with `SUB2GEN_DATABASE_BACKEND=postgres`.
8. Startup requires PostgreSQL, Redis, schema revision, cutover marker, cache warmup, and active-task recovery before clearing maintenance.
9. Verify `/health`, `/metrics`, admin reads, task polling, one protected request, WebSocket replay, cache delivery, and browser/captcha behavior.

PostgreSQL outage returns HTTP 503 `{"detail":"database_unavailable"}` with `Retry-After: 5`. Health and metrics remain available; database-backed reads and polling do not.

## Abort and rollback

Before a committed cutover, `abort --confirm ABORT` removes staging tables and clears migration maintenance. It refuses to destroy committed PostgreSQL data.

If deployment or verification fails, redeploy the unchanged SQLite bridge, leave PostgreSQL untouched, verify SQLite health, and reopen only after the bridge is healthy. Keep the final snapshot and target for investigation.

After seven stable days, remove `sub2gen.db`, `aiosqlite`, SQLite upload/download UI, and the runtime selector. Keep the offline importer and retained encrypted artifacts.

## One-click restore

Admin restore still requires a recent session and typed `RESTORE`. PostgreSQL restore enables maintenance, uploads an encrypted pre-restore backup, authenticates/decrypts/checks the selection, closes the pool, runs PostgreSQL 16 `pg_restore --clean --if-exists --single-transaction --exit-on-error`, reapplies migrations, verifies rows, swaps profiles, and restores the pre-restore archive on failure. A successful rollback also leaves maintenance active and restarts the replica; traffic reopens only after PostgreSQL readiness, Redis warmup, and active-task recovery. If rollback itself fails, maintenance remains fail-closed for operator recovery.

Legacy SQLite backups remain visible as rollback artifacts. PostgreSQL mode rejects old SQLite upload/download controls with HTTP 410.

## Acceptance

- all backend/storage contract tests pass against PostgreSQL 16 and Redis 7;
- final sync and verification complete below 12 minutes;
- origin control-plane p95 is below 100 ms;
- PostgreSQL pool-wait p95 is below 10 ms;
- normal database query p95 is below 25 ms;
- WebSocket progress is below 500 ms;
- no lost tasks, bad rate decisions, leaks, unbounded tables, or SQLite lock activity.
