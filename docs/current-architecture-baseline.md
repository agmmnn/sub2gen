# Current Architecture Baseline

- Status: Phase 0 characterization baseline
- Captured: 2026-08-09
- Baseline code commit: `85f3866a9c222bfdb2d2c34146690b554aa1e598`
- Scope: behavior before the unified provider SDK, generic worker protocol, and local
  image worker

This document inventories the seams that later phases may move. It describes current
behavior; it is not the target architecture. Exact HTTP and model contracts are frozen
in machine-readable fixtures, and the two legacy worker dialects are frozen as
sanitized transcripts.

## Reproducible baselines

| Contract | Fixture | Guard |
| --- | --- | --- |
| FastAPI routes and schemas | `apps/api/tests/contracts/openapi.json` | `apps/api/tests/test_http_contract.py` |
| Native Flow configuration and aliases, Runway manifest, GeminiGen manifest | `apps/api/tests/contracts/model-catalog.json` | `apps/api/tests/test_model_catalog_contract.py` |
| Existing extension registration/jobs | `apps/api/tests/contracts/extension-worker-*.json` | `apps/api/tests/test_extension_worker_contract.py` |
| Complete pre-v1 extension and agent-gateway dialects | `apps/api/tests/contracts/legacy-workers/` | `apps/api/tests/test_legacy_worker_contract_fixtures.py` |
| Fixture credential safety | all contract and fixture trees | `apps/api/tests/test_fixture_safety.py` |

Regenerate the two generated snapshots only after reviewing an intentional contract
change:

```bash
uv run python scripts/update_contract_snapshots.py
```

## Runtime composition

`apps/api/src/sub2gen/main.py` creates the FastAPI application. Its lifespan in
`bootstrap/lifecycle.py` owns startup, recovery, scheduled work, and shutdown.
`bootstrap/container.py` constructs one `AppContainer` containing:

- the active SQLite or PostgreSQL database adapter and capability repositories;
- `FlowClient`, `TokenManager`, load balancing, concurrency, and `GenerationHandler`;
- `RunwayService` and `GeminiGenService`;
- one shared `FileCache` owned by `GenerationHandler` and reused by those services;
- API-key, Redis, backup, failed-payload, and lifecycle-task services.

The container is explicit. Reusable Google Flow resources and its provider-SDK adapter
live in `packages/provider-google-flow`; application-owned token, project, CAPTCHA,
logging, and streaming orchestration remains in `FlowClient` and `GenerationHandler`.
Runway and GeminiGen combine provider HTTP, persistence, capacity, task, cache, and
normalization behavior in service modules.

The unified generation control plane now has a namespaced `ModelRegistry`, a
deterministic `GenerationRouter`, persistence-backed provider/account/worker/credential
candidate inventory, provider-isolated runtime health/quota signals, and a durable
`GenerationAuditService`. Generic provider accounts are assigned to managed API keys
through `provider_account_api_keys`; adopted Flow accounts continue to inherit their
existing token assignment isolation.

The separate `apps/agent-gateway` application exposes its own solve HTTP/WebSocket
surface and has an in-memory connected-agent registry. It does not share a protocol
implementation with the API application's extension worker.

## Model catalog and routing

### Catalog inputs

- Native Flow: `services/generation_handler.py::MODEL_CONFIG` contains 216 concrete and
  compatibility model IDs at this baseline. `core/model_resolver.py` supplies simplified
  aliases and request-time aspect/resolution mapping. Native 4K entries are exposed only
  when an active eligible Flow account is present.
- Runway: `core/runway_manifest.py::RUNWAY_MODEL_MANIFEST` contains 17 captured entries.
  The database seeds editable `runway_models` rows, and only enabled, live-available
  rows are exposed when the integration and at least one account are active.
- GeminiGen: `core/geminigen_manifest.py::GEMINIGEN_MODEL_MANIFEST` contains 80 entries.
  Exposure depends on integration/account state, and video models additionally depend
  on the video feature switch.

`transport/models.py` serves OpenAI- and Gemini-shaped catalogs. These responses are
deployment-state dependent; `model-catalog.json` therefore freezes the complete,
deterministic catalog inputs rather than a single account-dependent response.

### Request routing

`transport/openai.py` normalizes `/v1/chat/completions` and
`/v1/async/chat/completions`, then selects one of three paths:

1. IDs recognized by `RunwayService.is_runway_model` go to `RunwayService` after the
   Runway API-key scope check.
2. IDs recognized by `GeminiGenService.is_geminigen_model` go to `GeminiGenService`
   after the GeminiGen scope and feature checks.
3. Remaining IDs must resolve through native `MODEL_CONFIG`; API-key account/project
   selection runs before `GenerationHandler`, which uses Google Flow accounts.

The Gemini-compatible transport normalizes its request into the same native generation
shape and currently special-cases GeminiGen but not Runway. A model name does not
identify an authentication surface by itself.

Terminology is important: native Google Flow may execute Gemini, Imagen, and Veo model
families. The existing GeminiGen integration targets `geminigen.ai`; it is not a direct
Google Gemini API or Gemini Apps provider. The product must not claim direct Google
Gemini support until that provider exists independently.

## Accounts, credentials, and persistence

SQLite and PostgreSQL implement the same initial schema through paired migration files.
The relevant current tables are:

| Concern | Tables | Current ownership |
| --- | --- | --- |
| Native Google Flow | `tokens`, `token_stats`, `projects` | Session/access material, browser profile state, cookies/login fields, quota/tier, project pinning, refresh and generation switches are combined in `tokens` |
| API clients and assignment | `api_clients`, `api_keys`, `api_key_accounts`, `api_key_rate_limits`, `api_key_audit_logs` | Caller authentication, scopes, rate limits, and allowed native token IDs |
| Extension workers | `extension_worker_bindings`, `captcha_worker_keys` | Route-key/API-key mapping and standalone CAPTCHA-worker credentials; live sockets and routing metrics are not durable |
| Runway | `runway_accounts`, `runway_models`, `runway_tasks`, `runway_config` | Raw credential, account capacity, editable model registry, tasks, and integration settings |
| GeminiGen | `geminigen_accounts`, `geminigen_tasks`, `geminigen_config` | Cookie/bearer/refresh material, detailed quota/capacity state, tasks, and settings |
| Native async work | `tasks` | Native Flow job result/progress fields linked to one token and API key |
| Artifacts and audit | `cache_files`, `request_logs`, `operation_stats` | Delivery metadata and request/operation history |

Current live credential columns are plaintext at rest. PostgreSQL backup-archive
encryption does not encrypt `tokens`, `runway_accounts`, `geminigen_accounts`, API-key
plaintext compatibility columns, or legacy worker-key plaintext columns. Later phases
must not describe these as an encrypted credential store.

`api_key_accounts` stores a numeric account ID without a provider discriminator, so its
meaning is native-Flow-specific today. PostgreSQL also retains a legacy
`dedicated_extension_workers` table that has no SQLite equivalent.

Only `0001_initial.sql` exists for each backend at this baseline. PostgreSQL is driven
through its migration runner. SQLite has checksummed migrations but also retains
dynamic compatibility DDL in `Database.init_db()`, so Phase 3 must test both fresh and
legacy-adoption paths rather than assuming one schema authority.

`persistence/repositories.py` currently provides thin account, project, API-key, cache,
request-log, and worker capability facades over the large database adapters. It has no
generic provider-account, credential-binding, generation-job, or attempt repository.
Provider-specific acquisition and capacity operations still belong to their database
and service implementations.

## Task lifecycle

The public polling surface is `GET /v1/jobs/{job_id}`, but it reads three different
durable task shapes:

- Native Flow IDs use `tasks`. Submission creates the row and schedules a FastAPI
  background task. Progress and terminal fields are updated during generation. Native
  work has no durable attempt/lease/idempotency model and is not reconstructed after a
  process restart.
- Runway IDs use `runway_tasks`. Submission persists local and upstream IDs; polling
  may contact Runway and persist the result. The public polling route verifies the
  originating API-key ID.
- GeminiGen IDs use `geminigen_tasks`. Submission persists a queued task plus request
  log, background work acquires capacity and polls, and startup explicitly resumes
  active GeminiGen tasks.

The providers have different status vocabularies, retry behavior, restart semantics,
account claims, and response payloads. `request_logs` provide request audit but do not
replace durable generic job attempts or a resolved-execution record. This is why Phase
3 introduces additive generic job/attempt persistence before dispatching to a new local
worker.

## Artifact lifecycle

`services/file_cache.py::FileCache` is the current artifact boundary:

- local filesystem storage under the configured cache directory or DigitalOcean Spaces;
- proxy delivery for local storage and proxy/CDN delivery for Spaces;
- `cache_files` metadata scoped to the managed API key, with optional native token and
  Flow project ownership;
- generated-media validation, bounded downloads, cleanup/retention, and local disk
  reserve recovery.

When cache is disabled or a provider path cannot be mirrored, responses may retain
provider/CDN URLs. Those URLs can expire and should not be treated as durable artifacts.
Cache metadata writes are best effort in parts of the current path, and object cleanup
and metadata cleanup are not one atomic operation; stale rows or orphaned objects are
therefore possible and must be included in artifact-contract tests.

The extension has a separate in-memory `GenerationUploadStore` for oversized HTTP-relay
JSON responses. A generated secret, request ID, byte ceiling, TTL, and single-ingest
check protect the slot, but it is extension-specific and is lost on restart. It does not
yet provide the generic job/attempt/worker ownership, digest, media-type, object-scope,
or artifact-commit contract required for future workers.

## Legacy worker dialects

### `/captcha_ws`

Authentication is resolved during the WebSocket handshake from a managed API key,
CAPTCHA-worker key, or `refresh_token_id`. The first in-band message is `register` and
the server replies with `register_ack`.

Server-to-extension work includes `get_token`, `refresh_st`, `submit_generation`, and
`poll_generation`; it may also send `captcha_upstream_verdict`. The extension sends
`ping`, `client_shutdown`, and typed generation results. CAPTCHA and session-refresh
results are untyped and are matched only by `req_id`. Large relay results use
`POST /api/extension/generation-upload` and then reference the upload ID in the
WebSocket result.

### `/ws/agents`

The first message is `register`, authenticated with a legacy device token or Keygen
agent token. The server replies `registered`, dispatches `solve_job`, and accepts
`solve_result` or `solve_error` correlated by `job_id`. This dialect supports CAPTCHA
solving only.

### Defects protocol v1 must resolve

- Neither dialect has wire-version or capability negotiation.
- The extension sends `ping`, but the API emits no pong; the gateway treats an
  equivalent message as unknown.
- Extension CAPTCHA/refresh results have no message type, and the two dialects use
  different correlation and session concepts.
- Extension registration currently reports `allow_generation: false` while generation
  dispatch does not consistently enforce the advertised capability.
- Token-specific generation eligibility has contradictory refresh-worker filtering.
- Server generation timeouts are not transmitted to the extension, which uses its own
  default.
- The extension checks response ownership by socket; the gateway completes a job by ID
  without proving that the responding socket owns it.
- Gateway finish/error HTTP callbacks log lifecycle events but do not deliver verdict
  feedback to the solving agent.
- The current extension generation relay carries caller-shaped URL, headers, and body.
  Protocol v1 must replace it with typed provider-owned operations rather than preserve
  a generic browser HTTP proxy.

The exact current frames, including error and upload cases, are in
`apps/api/tests/contracts/legacy-workers/`. Absence of a field in those fixtures is
part of the characterized legacy contract. Every WebSocket frame also round-trips
through the executable compatibility codecs in
`workers/extension/legacy_codec.py` and `sub2gen_gateway/legacy_codec.py`; the existing
runtime handlers remain unchanged in Phase 0.

## Phase 0 conclusions

- Provider routing exists, but the universal abstraction is still transport/service
  branching around Google Flow's model configuration.
- Current task tables provide valuable provider-specific behavior but not a generic,
  restart-safe job/attempt/execution audit.
- `FileCache` is reusable, while worker artifact ingress needs a separate secured
  generic contract.
- Existing worker clients remain compatibility inputs, not the design of protocol v1.
- Credential locality and encryption require explicit new work; opaque references must
  not cause existing plaintext storage to be mislabeled.
- The source/runtime facts in `docs/provider-source-provenance.md` and the security
  controls in `docs/worker-threat-model.md` are engineering baselines for later phases.
