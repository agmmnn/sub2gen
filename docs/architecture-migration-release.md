# Architecture migration release and rollback report

Report date: 2026-08-08

This report closes the modular-monolith migration described in
[`architecture-migration-plan.md`](./architecture-migration-plan.md). It covers
compatibility, installation and upgrade evidence, operational checks, and the
rollback boundary for existing sub2gen installations.

## Compatibility summary

The supported user entry points remain:

```bash
uv run setup
uv run sub2gen
```

Existing HTTP paths, OpenAI/Gemini request shapes, WebSocket worker messages,
SQLite runtime data, PostgreSQL schema adoption, and extension modes retain
characterization coverage. The Python distribution and import package are both
named `sub2gen`; source code now lives under `apps/api/src/sub2gen`.

Intentional operator-facing changes:

- local mutable data lives under `.runtime/`;
- the admin web app, API, and extensions are Bun workspaces;
- the CAPTCHA extension must be built and Chrome must load
  `apps/captcha-extension/dist/`, not its source directory;
- TypeScript API contracts are generated from
  `apps/api/tests/contracts/openapi.json` into `packages/api-contract`;
- SQLite and PostgreSQL use ordered, checksummed migration revision `0001`.

## Verification evidence

The final local verification used Python 3.11.14, uv, and Bun 1.3.14.

### Complete regression and workspace checks

```bash
uv run ruff check .
uv run pyright
uv run pytest -q
bun run check
bun run build
```

The backend suite completed with 476 passed, 3 skipped, and 50 subtests. The
focused release suite completed with 42 passed. The Bun workspace checks cover
the admin web app, generated API contract, CAPTCHA extension, metadata
extension, and `extension-core` package.

The focused workflow command covers image/video request contracts, project
selection, cache storage/delivery, account import, token refresh, CAPTCHA and
generation jobs, and end-user/CAPTCHA/refresh worker routing:

```bash
uv run pytest -q \
  apps/api/tests/test_openai_generation_contract.py \
  apps/api/tests/test_generation_pipelines.py \
  apps/api/tests/test_project_pinning.py \
  apps/api/tests/test_cache_backends.py \
  apps/api/tests/test_extension_upload_store.py \
  apps/api/tests/test_extension_account_import.py \
  apps/api/tests/test_extension_refresh_jobs.py \
  apps/api/tests/test_extension_captcha_jobs.py \
  apps/api/tests/test_extension_generation_jobs.py \
  apps/api/tests/test_extension_worker_contract.py \
  apps/api/tests/test_extension_worker_routing.py \
  apps/api/tests/test_personal_session_refresh_jobs.py \
  apps/api/tests/test_personal_worker_routing.py \
  apps/api/tests/test_sqlite_migrations.py
```

These checks deliberately do not submit paid generation jobs to Google. A live
provider smoke test requires an operator-owned authenticated account and may
consume credits; perform it after deployment when that external side effect is
acceptable.

### Fresh installation

A new local clone with no `.venv`, `node_modules`, `.runtime`, or generated
frontend output successfully ran `uv run setup`. uv installed Python 3.11.14,
created the virtual environment, installed the locked backend, Bun installed the
locked workspaces, and the build created `apps/api/static/index.html`.

The clean clone then started with `uv run sub2gen`. `GET /health`,
`GET /openapi.json`, and `GET /` returned 200; the OpenAPI document exposed 158
paths and the admin HTML loaded from the generated static output.

### Existing SQLite upgrade

The upgrade test used SQLite's online backup command to copy the repository's
actual pre-migration `.runtime/data/sub2gen.db`. The source had no
`schema_migrations` table. `Database.check_and_migrate_db()` adopted and stamped
revision `0001`; the copy retained 1 token and 4 projects and returned `ok` from
`PRAGMA integrity_check`.

PostgreSQL 16 and Redis integration contracts run in GitHub Actions. Existing
PostgreSQL deployments are validated against the baseline before revision
`0001` is stamped; partial or unknown schemas fail without being stamped.

### Container and CI verification

The `Build and Push Docker Image` workflow builds both
`infra/docker/Dockerfile` and `infra/docker/Dockerfile.headed` for linux/amd64
and linux/arm64. The `Quality` workflow runs all backend and Bun workspace gates,
and the storage workflow runs PostgreSQL 16 plus Redis contracts. Compose files
are rendered by the infrastructure job. Historical pre-rename CI evidence remains
associated with the corresponding commits; new runs are published in the standalone
`agmmnn/sub2gen` repository. The Docker matrix covers both variants for linux/amd64
and linux/arm64.

## Upgrade procedure

1. Record the deployed Git commit and stop all sub2gen writers.
2. Back up `.runtime/data/sub2gen.db` together with its `-wal` and `-shm`
   sidecars, or use SQLite's online backup command. PostgreSQL operators should
   use the encrypted backup workflow or PostgreSQL 16 `pg_dump`.
3. Keep the backup outside the checkout/runtime volume and verify it.
4. Pull the desired `main` commit.
5. If upgrading the old repository layout, move `data` and `tmp` into
   `.runtime/` as documented in the main README. Never overwrite an existing
   `.runtime/data` directory.
6. Run `uv run setup`.
7. Start with `uv run sub2gen` and inspect startup migration output.
8. Verify `/health`, login, token/project counts, and one credentialed request.
9. Rebuild and reload `apps/captcha-extension/dist/` when using the extension.

Detailed database behavior is documented in
[`database-migrations.md`](./database-migrations.md); PostgreSQL cutover and
backup operations are in
[`postgres-migration-runbook.md`](./postgres-migration-runbook.md).

## Rollback boundary

Git rollback and data rollback are separate:

- Code can return to the last known-good commit only while its schema remains
  compatible with the restored application.
- Database migrations are forward-only. If the new release has written data or
  stamped a revision that the old release does not understand, stop writers and
  restore the verified pre-upgrade database/volume before starting old code.
- Do not delete or edit rows in `schema_migrations`; do not manually reverse SQL.
- Restore SQLite sidecars with the database, or restore the online-backup file as
  a unit. For PostgreSQL, restore the matching dump/backup and deployment
  markers together.
- Keep the pre-upgrade commit and backup until credentialed generation,
  extension synchronization, cache delivery, and scheduled refresh have run
  successfully in the deployed environment.

The last safe rollback point is therefore the pair of the pre-upgrade Git commit
and its verified durable-data backup. Neither half alone is a complete rollback.

## Known follow-up work

The CAPTCHA extension's extracted state boundaries are strict TypeScript. Its
large background and options orchestration entry points retain temporary
`@ts-nocheck` markers to avoid combining the architecture migration with a
behavioral rewrite. Future changes should move additional logic behind the
typed boundaries rather than enlarging those entry points.
