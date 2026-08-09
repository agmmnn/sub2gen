# Unified Generation Platform Plan

Status: Phases 0 through 7 complete; Phase 8 (public image API and asynchronous jobs) is next.

This plan evolves sub2gen from a Google Flow-focused compatibility service into a
local-first generation gateway with this product position:

> Unified image generation API for consumer AI subscriptions — bridge ChatGPT,
> Flow, Gemini, and other UI-backed generation services into one local API.

The change extends the completed modular-monolith migration. It is not a rewrite and
does not require microservices. Phase 1.5 establishes the only supported local command:

```bash
uv run setup
uv run sub2gen
```

No former command alias is retained after the identity cutover.

## Goals

- Expose server-backed and local UI-backed generation providers through one stable API.
- Make Google Flow one provider implementation rather than the product boundary.
- Keep browser sessions, OAuth material, and cookies at their approved execution location.
- Use one versioned worker protocol for browser extensions, local daemons, and gateways.
- Prevent silent routing between subscription or billing pools.
- Preserve existing model IDs, HTTP contracts, worker behavior, and database upgrades.
- Add providers without extending `generation_handler.py` or `core/database.py` into new god modules.
- Establish the provider-neutral `sub2gen` repository and code identity before the
  provider SDK and worker protocol create more old-name dependencies.

## Non-goals

- Offering a public multi-tenant service backed by consumer subscriptions.
- Uploading raw ChatGPT cookies or Codex OAuth tokens to the control plane.
- Vendoring or maintaining the complete `chrome-use` Rust/native-extension codebase.
- Replacing all existing provider-specific account tables in the first release.
- Completing the entire legacy database decomposition before adding a provider.
- Silently falling back from ChatGPT Web usage to metered Codex usage.
- Enabling third-party online style galleries by default.
- Combining every browser capability into one Chrome extension.
- Maintaining former project-identity aliases after the Phase 1.5 hard cutover.

## Architectural decisions

### 1. Three logical planes, one deployable by default

The planes are ownership boundaries, not mandatory services:

```text
Control plane
  API authentication, model catalog, routing, jobs, workers, audit, policy

Execution plane
  Provider adapters running on the API host, a local worker, or a browser extension

Artifact plane
  Generated files, cache backends, delivery URLs, retention, and cleanup
```

A local installation may run all three on one computer. A hosted control plane must
dispatch browser-backed work to an explicitly paired local worker.

### 2. One home for each provider

Provider implementations are Python packages, not application-owned copies:

```text
packages/
├── provider-sdk-python/
├── provider-google-flow/
├── provider-chatgpt/
├── provider-google-gemini/      # future direct Gemini integration
└── worker-protocol/
```

Applications contain only composition and runtime code. `apps/api` imports providers
that can execute on the server. `apps/image-worker` imports providers that require a
local browser, local OAuth file, OS keychain, or desktop tools.

Phase 1.5 renames the application import root to `sub2gen` without an old-namespace
shim. Google Flow moves into its provider package only after the provider contract is
exercised by existing Flow behavior and the materially different ChatGPT Web spike.

In this plan, `google-flow` means Labs/Flow and may expose Gemini, Imagen, and Veo model
families. The existing `GeminiGen` integration targets the separate `geminigen.ai`
service; it is not a direct Google Gemini provider. Product copy must not claim direct
Google Gemini support until a `provider-google-gemini` implementation passes the same
provider contract and technical verification.

### 3. One protocol, multiple worker implementations

`packages/worker-protocol` owns the canonical, versioned message schemas. It does not
force the CAPTCHA extension, image worker, and agent gateway into one executable.

The protocol supports capabilities such as:

```text
captcha.solve
session.refresh:google-flow
image.generate:google-flow
image.generate:chatgpt-web
image.generate:chatgpt-codex
```

Existing worker endpoints remain available during migration. Compatibility adapters
translate their current messages into the canonical job model until each client can
adopt protocol v1 directly.

### 4. Billing policy is internal and auditable

`quota_bucket` will not be accepted as a caller-controlled routing field. The control
plane resolves an internal execution policy from the requested model, authenticated API
key, account assignment, and operator configuration.

```text
ExecutionPolicy
  allowed_providers
  allowed_credential_kinds
  allowed_billing_pools
  allow_paid_fallback
  preferred_execution_location
  required_worker_capabilities

ResolvedExecution
  requested_model
  resolved_model
  provider_id
  provider_account_id
  worker_id
  billing_pool
```

Every terminal result and request log records `ResolvedExecution`. Cross-billing-pool
fallback is denied unless an explicit server-side policy enables it.

### 5. Credentials are bindings, not universal token columns

The control plane stores provider identities separately from opaque credential
locations:

```text
ProviderAccount
  id
  provider_key
  label
  external_account_id         nullable
  enabled
  metadata                    non-secret only
  legacy_source               nullable
  legacy_id                   nullable

CredentialBinding
  id
  provider_account_id
  worker_id                   nullable
  binding_key
  credential_type
  storage_kind                legacy_table | env | worker_vault | browser_session
  secret_ref                  opaque locator
  enabled
  expires_at                  nullable
  last_validated_at           nullable
  last_error                  nullable
  metadata                    non-secret only
```

Examples:

- Existing Google Flow cookie: `legacy://tokens/<id>/session` until a separate secure
  storage migration is implemented.
- ChatGPT Codex OAuth: reference to a worker-local OS keychain or Codex auth file.
- Logged-in ChatGPT Web: paired worker plus opaque Chrome profile reference; no token.

The current Flow, Runway, GeminiGen, and worker credential columns are plaintext at
rest. PostgreSQL backup encryption does not encrypt those live columns. This plan must
not relabel them as encrypted storage: a `CredentialResolver` initially resolves legacy
locators, while any new server-side secret store requires an explicit key-management,
encryption, rotation, migration, and recovery design.

Existing Runway, GeminiGen, and Google account tables remain supported. The generic
tables map to them rather than copying secrets, quotas, or concurrency state. They can
migrate behind `ProviderAccount` adapters after the model is proven.

### 6. ChatGPT is merged; chrome-use remains a runtime dependency

The `chatgpt-imagegen` MIT code is imported at a recorded upstream commit with its
license and attribution. Its CLI behavior and test suite are preserved while the
single script is split into an internal provider package.

`chrome-use` remains an external, version-pinned runtime dependency. The ChatGPT Web
provider talks to it through a small typed process adapter. This avoids absorbing a
separate Rust daemon, native messaging host, browser extension, release pipeline, and
security boundary into this repository.

### 7. UI-backed providers are local-first

The ChatGPT Web and Codex subscription providers run on `apps/image-worker`, even when
the API happens to run on the same machine. This keeps authentication topology stable
between local and hosted deployments and prevents later movement of secrets from the
server to a worker.

## Target layout

```text
sub2gen/
├── apps/
│   ├── api/
│   ├── admin-web/
│   ├── image-worker/
│   ├── agent-gateway/
│   ├── captcha-extension/
│   └── metadata-extension/
├── packages/
│   ├── api-contract/
│   ├── extension-core/
│   ├── provider-sdk-python/
│   ├── provider-google-flow/
│   ├── provider-chatgpt/
│   ├── provider-google-gemini/
│   └── worker-protocol/
├── infra/
├── docs/
├── pyproject.toml
└── package.json
```

Python packages join the root uv workspace and remain installable through the existing
setup command. Protocol schemas generate both Python and TypeScript types so message
definitions are not manually duplicated.

## Core contracts

The approved multi-provider spike determines final method names, but the provider SDK
must represent these concepts:

```python
ProviderCapabilities
GenerationRequest
ProviderExecutionContext
ProviderJob
ProviderEvent              # accepted, progress, artifact, warning, completed, failed
Artifact
ProviderResult
ProviderHealth
ResolvedExecution
ProviderError              # auth, quota, policy, transient, invalid-input, unavailable
```

Requirements:

- Text-to-image and image-to-image with multiple references.
- Streaming progress without requiring every provider to stream.
- Explicit cancellation and timeout propagation.
- Provider-native job IDs and resumability where available.
- No provider-specific credentials in `GenerationRequest`.
- Provider extensions isolated under a typed `provider_options` field.
- Artifacts returned as bytes or trusted local references and then committed through
  the existing `FileCache` boundary.

## Worker protocol v1

The two current protocols are unversioned, so this is the first canonical wire version;
the Python/TypeScript package itself has independent semantic versions.

Canonical envelope fields:

```text
protocol_version
message_id
message_type
correlation_id
job_id                     nullable outside job messages
job_kind                   nullable outside job messages
worker_id
sent_at
payload
```

Lifecycle messages:

```text
worker.hello
worker.challenge
worker.register
worker.registered
worker.capabilities
worker.heartbeat
job.offer
job.accept
job.reject
job.progress
job.result
job.error
job.cancel
job.cancelled
```

Delivery rules:

- Jobs have immutable IDs, attempt numbers, deadlines, lease IDs, and capability names.
- Delivery is at-least-once; workers deduplicate by job ID and attempt.
- An offer is not assigned until accepted.
- Leases expire after disconnect or missed heartbeats.
- Late results with stale lease IDs are rejected.
- Cancellation is best-effort but always audited.
- Large artifacts use the existing authenticated upload side channel rather than large
  WebSocket frames.
- Unknown protocol versions and capabilities fail closed.
- `worker_session_id` identifies a connection; provider values such as a CAPTCHA
  `solve_session_id` use distinct fields.
- Registration advertises `supported_versions` and capabilities; the server selects a
  version. A missing version list means the legacy dialect, and the server never emits
  v1 messages to a client that did not negotiate them.

## Local worker security

Initial pairing:

1. The worker generates a device key pair locally.
2. An administrator creates a short-lived, single-use pairing code.
3. The worker submits the code and public key over TLS.
4. The server records the device identity and issues a revocable credential.
5. Each connection proves possession of the device key and receives a short-lived
   session authorization.

Pairing authenticates the server relationship but does not make a compromised control
plane safe. The worker therefore enforces its own policy:

- Local provider and capability allowlist.
- Local model and account/profile allowlist.
- Maximum concurrency and optional daily job limits.
- Optional confirmation for newly paired servers or sensitive capabilities.
- Visible pause, disconnect, and revoke controls.
- Job audit containing server identity, API-key identity, provider, and model.
- No arbitrary shell command, arbitrary browser-eval, or arbitrary URL capability in
  the worker protocol.

Browser extensions use an equivalent device credential appropriate to extension
storage; mutual TLS is not required for the browser client.

API-key identity received from the control plane is asserted metadata, not an
independently authenticated caller identity. A worker may trust it for display only
unless the job includes a separately verifiable, narrowly scoped authorization claim.

## Persistence additions

Add focused repositories and ordered SQLite/PostgreSQL migrations for:

- `ProviderAccountRepository`
- `CredentialBindingRepository`
- `GenerationJobRepository`
- `GenerationAttemptRepository`
- A `CredentialResolver` that resolves secret material outside repositories.
- Typed registration, authentication, heartbeat, enablement, and capability methods on
  the existing `WorkerRepository`.

The worker record is a durable logical device with a public ID, kind, label, enabled
state, approved capabilities, hashed authentication key, and last-seen diagnostics.
WebSocket session IDs, sockets, in-flight counts, latency estimates, leases, and routing
cursors remain ephemeral in memory or Redis.

Generation job identity, idempotency keys, attempts, dispatch state, and terminal
execution audit are durable. They must survive API restarts even though individual
WebSocket leases do not.

Before adding new job or model tables, audit existing task, request-log, Runway,
GeminiGen, and worker-binding tables and reuse them where their lifecycle matches.
Generic repositories may delegate to existing database methods during transition, but
new provider behavior must not add more methods directly to `core/database.py` without
a repository boundary.

Repositories return metadata and opaque locators, not raw secrets, in list/admin models.
Worker-local secret references are unusable by the API host. A future
`encrypted_server` storage kind may be introduced only with the encryption design and
migration described above.

## Model and API policy

Initial namespaced models:

```text
google-flow/<existing-model-family>
chatgpt/gpt-image-web
chatgpt/gpt-image-codex
```

Compatibility aliases preserve every existing Flow model ID. Namespaced aliases are
added without changing current responses.

Initial API surfaces:

- Existing `/v1/chat/completions` behavior remains supported.
- Add or complete OpenAI-compatible `/v1/images/generations`.
- Add or complete OpenAI-compatible `/v1/images/edits` for reference images.
- Existing async jobs and polling remain the canonical long-running path.
- Provider, resolved model, billing pool, and worker information appear in admin logs;
  public responses expose only non-sensitive execution metadata under an optional
  extension field.

Automatic aliases such as `auto/image-fast` are deferred until execution-policy and
resolved-execution auditing have production coverage.

## Implementation phases

### Phase 0: Architecture baseline and decision record

- [x] Inventory current model routing, provider account tables, task lifecycle,
  artifact storage, extension worker messages, and agent-gateway messages.
- [x] Freeze both legacy wire dialects as codecs and fixtures: `/captcha_ws` uses
  `req_id`, partially untyped results, refresh, CAPTCHA, and HTTP relay messages, while
  `/ws/agents` uses `job_id` and solve-only typed messages.
- [x] Record protocol defects that v1 must resolve: inconsistent generation capability
  flags, missing timeout propagation, ping/pong behavior, gateway response ownership,
  and lifecycle feedback.
- [x] Record exact upstream commits and licenses for `chatgpt-imagegen` and `chrome-use`.
- [x] Record the personal, self-hosted deployment boundary and keep browser credentials
  on the machine that owns the logged-in session.
- [x] Record exact source provenance, versions, licenses, and technical execution
  boundaries for each imported or externally invoked browser component.
- [x] Write the worker threat model, trust boundaries, and prohibited capabilities.
- [x] Freeze sanitized worker and generation transcripts as characterization fixtures.
- [x] Record the pre-change OpenAPI document and model catalog.

Exit criteria:

- [x] Architecture decisions and trust assumptions are explicit.
- [x] Current contracts can be compared after every later phase.
- [x] No live credentials or browser data exist in fixtures.

### Phase 1: ChatGPT Web vertical spike

This phase is intentionally throwaway and is not exposed through a public HTTP route.
It probes real behavior without freezing a provider interface or adding a production
route.

- [x] Invoke the current `chatgpt-imagegen` CLI from a minimal async harness.
- [x] Generate one text-to-image result through a paired real Chrome session.
- [x] Generate one image-to-image result with a temporary reference file.
- [x] Verify project selection, conversation cleanup, output validation, and timeout.
- [x] Prove process-tree cancellation and temporary-file cleanup.
- [x] Capture sanitized success, authentication, quota, refusal, and browser-unavailable
  outcomes.
- [x] Measure actual latency and enforce ChatGPT Web concurrency at one.
- [x] Keep Codex OAuth outside this spike and make implicit fallback impossible with an
  explicit `--backend web` invocation.

Exit criteria:

- The browser path works from the intended local-worker environment.
- Failure and cancellation behavior are understood before an interface is frozen.
- No production endpoint or persistent credential model depends on spike code.

### Phase 1.5: `sub2gen` identity cutover

This is a deliberate breaking rename before the provider SDK and worker protocol are
created. It does not keep compatibility aliases for the former project identity.
Provider behavior, OpenAI-compatible HTTP shapes, databases, and existing generation
features remain in scope and are verified independently from the identity cutover.

- [x] Move the Git history to the standalone `agmmnn/sub2gen` repository and make it the
  only `origin`; the new repository must not remain in the former GitHub fork network.
- [x] Rename the Python distribution and import root to `sub2gen`, move all source/tests,
  and update setuptools discovery and package data without a namespace shim.
- [x] Make `uv run sub2gen` the sole primary executable and remove the former console
  script rather than leaving an alias.
- [x] Standardize environment variables on `SUB2GEN_*` and update configuration,
  scripts, Compose files, Dockerfiles, CI, and documentation without old-name fallback.
- [x] Standardize JavaScript package scopes on `@sub2gen/*` and update Bun workspace
  references and lockfiles.
- [x] Rename application titles, extension identities and copy, generated API metadata,
  container/service/image names, logs, and repository documentation to `sub2gen`.
- [x] Replace newly issued managed-key branding/prefixes with `s2g_live_` and define an
  explicit operator migration for existing local keys instead of accepting two branded
  key formats indefinitely.
- [x] Audit runtime paths and persisted identifiers. Keep neutral paths/schema fields;
  rename branded ones with a one-time migration, not a runtime compatibility branch.
- [x] Remove obsolete source names and assert that no old import, executable,
  environment prefix, npm scope, UI title, or container identity remains.
- [x] Update the architecture, setup, deployment, and extension documentation around
  `uv run setup` and `uv run sub2gen`.

Exit criteria:

- `uv run setup` and `uv run sub2gen` are the only documented local startup path.
- `import sub2gen` succeeds; the former import and executable do not.
- Fresh SQLite/PostgreSQL installs and a one-time migration of the current personal
  runtime pass without maintaining old-name branches in application code.
- Python, Bun, Docker/Compose, extension, OpenAPI, and live health checks use `sub2gen`.
- The GitHub repository is standalone and `origin` points only to `agmmnn/sub2gen`.

### Phase 2: Provider SDK and package workspace

- [x] Add `packages/provider-sdk-python` to the uv workspace.
- [x] Update root setuptools discovery, uv workspace/source mappings, and Docker build
  contexts so workspace provider packages are actually installable and editable.
- [x] Define provider requests, events, results, artifacts, health, errors, and execution
  context from Flow plus the materially different ChatGPT Web spike behavior.
- [x] Add a reusable provider conformance test suite.
- [x] Implement thin adapters for Google Flow and ChatGPT Web.
- [x] Prove streaming, non-streaming, cancellation, reference images, and artifact
  handling through fakes.
- [x] Keep current `FlowClient` and `GenerationHandler` public behavior unchanged.

Exit criteria:

- The interface is exercised by two materially different providers.
- No Flow-only fields appear in the universal contract.
- Provider packages can be tested without FastAPI or a real browser.

### Phase 3: Generic accounts, credential bindings, and workers

- [x] Add domain models for provider accounts, credential bindings, worker devices, and
  worker capabilities.
- [x] Add durable generation job and attempt models for idempotency, dispatch state,
  retries, and terminal execution audit before any local provider can receive work.
- [x] Add focused repositories rather than waiting for complete database extraction.
- [x] Add paired, checksum-safe `0003` migrations for SQLite and PostgreSQL; never edit
  the applied `0001` files.
- [x] Update the legacy SQLite adoption path to create/stamp existing revisions and then
  apply `0003`; update PostgreSQL identity and boolean-column metadata for the new tables.
- [x] Validate fresh databases, existing-schema adoption, upgrade, backup, and rollback.
- [x] Add adapters that describe existing Flow, Runway, and GeminiGen accounts without
  destructive data movement or copied credentials.
- [x] Add a `CredentialResolver`; keep provider-specific claim/release and quota logic
  until its transactional behavior has characterization coverage.
- [x] Redact credential locators from logs and public/admin API responses by default.

Exit criteria:

- ChatGPT browser-profile and OAuth identities fit without provider-specific columns.
- Existing deployments upgrade without changing existing provider behavior.
- Repository parity tests pass for SQLite and PostgreSQL.

### Phase 4: Worker protocol v1 and compatibility bridge

- [x] Add canonical JSON schemas and generated Python/TypeScript types.
- [x] Implement version negotiation, registration, capabilities, heartbeat, leases,
  progress, cancellation, results, and structured errors.
- [x] Add device pairing, challenge/response authentication, revocation, and expiry.
- [x] Add worker-side policy schemas and fail-closed capability validation.
- [x] Build a server-side compatibility adapter for current CAPTCHA-extension messages.
- [x] Build a compatibility adapter for the current agent-gateway message flow.
- [x] Deploy dual-reading servers before v1-writing clients; treat absent
  `supported_versions` as legacy and retain the existing endpoints and authentication.
- [x] Resolve generation capability, timeout, fingerprint/solve-session, gateway
  response ownership, ping/pong, and upstream-feedback semantics before freezing v1.
- [x] Define job-scoped artifact upload grants with short expiry, worker/job ownership,
  content-type and size limits, digest verification, single-use semantics, and cleanup;
  a worker credential must not imply general API upload access.
- [x] Add golden transcript tests in both Python and TypeScript.
- [x] Update root Bun/uv workspaces and all relevant Docker build contexts for generated
  bindings, including the standalone agent gateway.

Exit criteria:

- One canonical schema describes every new worker job.
- Existing extension and gateway clients still work unchanged through adapters.
- Replay, stale lease, unauthorized capability, revocation, and disconnect tests pass.

### Phase 5: Local image worker and ChatGPT provider import

- [x] Add `apps/image-worker` with a small CLI, configuration file, health command, and
  WebSocket client.
- [x] Pair the worker to the API and advertise only locally enabled capabilities.
- [x] Import `chatgpt-imagegen` at the recorded commit with MIT attribution and history.
- [x] Split web browser, Codex OAuth, prompt/reference, project/conversation, output, and
  style concerns into `packages/provider-chatgpt` modules.
- [x] Preserve and port the upstream test suite before changing behavior.
- [x] Wrap `chrome-use` behind a typed process adapter with a pinned minimum version and
  actionable health diagnostics.
- [x] Keep raw Chrome and Codex credentials local; expose only opaque account/profile
  references.
- [x] Return image bytes through the authenticated artifact-upload path.

Exit criteria:

- `chatgpt/gpt-image-web` completes through the generic worker protocol.
- Worker cancellation terminates browser work and cleans files/tabs best-effort.
- The API host cannot read or use the worker's ChatGPT credentials.
- Upstream behavior tests and provider conformance tests pass.

### Phase 6: Google Flow provider packaging and orchestration cleanup

- [x] Move reusable Google Flow provider code into `packages/provider-google-flow`.
- [x] Expose Google Flow only through the `sub2gen` provider package/import path; do not
  recreate the removed application namespace.
- [x] Make `GenerationHandler` an orchestrator over provider execution and existing
  image/video pipelines rather than a provider selector.
- [x] Keep project pinning, token selection, CAPTCHA, cache, logging, and streaming
  contracts unchanged.
- [x] Route provider artifacts through one artifact commit boundary.

Exit criteria:

- Existing Google Flow tests and live-compatible request contracts remain unchanged.
- ChatGPT and Google Flow use the same provider lifecycle without sharing credentials or
  transport details.
- No replacement provider or orchestration god module is introduced.

### Phase 7: Model registry, routing policy, and execution audit

- [x] Introduce namespaced model descriptors and compatibility aliases.
- [x] Resolve `ExecutionPolicy` only from trusted server configuration and authenticated
  caller context.
- [x] Enforce provider, account, worker, credential-kind, billing-pool, and capability
  constraints before dispatch.
- [x] Persist `ResolvedExecution` for success, failure, cancellation, and timeout.
- [x] Deny cross-billing fallback by default.
- [x] Add deterministic routing and concurrency tests for multiple workers/accounts.
- [x] Add quota and health signals without allowing one provider's limiter to poison
  another provider.

Exit criteria:

- Every request can be audited from requested model to exact execution target.
- ChatGPT Web never becomes ChatGPT Codex without an explicit operator policy.
- Existing API-key account/project isolation remains enforced.

### Phase 8: Public image API and asynchronous jobs

- [ ] Route existing chat-completion image requests through the provider orchestrator.
- [ ] Add or complete `/v1/images/generations` and `/v1/images/edits` contracts.
- [ ] Map provider events onto existing streaming and async-job semantics.
- [ ] Support repeated reference images and validated remote input URLs.
- [ ] Store results through `FileCache` and existing delivery modes.
- [ ] Add cancellation, timeout, retry classification, and idempotency tests.
- [ ] Update the generated TypeScript API contract.

Exit criteria:

- The same normalized request can target Flow or ChatGPT explicitly.
- Existing OpenAI-compatible request and response shapes remain compatible.
- Async polling survives API or worker restarts according to documented provider limits.

### Phase 9: Provider, account, worker, and model administration

- [ ] Replace provider-specific navigation growth with Providers, Accounts, Workers,
  Models, and Jobs views.
- [ ] Show credential location and health without returning raw secrets.
- [ ] Add pairing-code creation, worker revocation, capability policy, pause, and status.
- [ ] Show requested model, resolved model, provider, billing pool, account, and worker in
  request/job details.
- [ ] Add ChatGPT health diagnostics for Chrome relay, login state, Codex OAuth, and
  supported local tools.
- [ ] Add warnings for internal/unsupported provider APIs and consumer-account limits.

Exit criteria:

- Operators can diagnose routing without reading server logs.
- Sensitive local identities are represented by opaque labels and references.
- Destructive account/worker operations require confirmation and are audited.

### Phase 10: Existing worker migration

- [ ] Migrate the CAPTCHA extension to generated protocol-v1 types incrementally.
- [ ] Migrate agent-gateway registration and job lifecycle onto the canonical protocol.
- [ ] Preserve current endpoints behind a documented compatibility window.
- [ ] Add protocol-version metrics and operator warnings for legacy clients.
- [ ] Remove legacy adapters only after one release with zero observed legacy sessions or
  after an explicit breaking-release decision.

Exit criteria:

- New and migrated clients use one registration, heartbeat, lease, cancellation, and
  result contract.
- Legacy removal has deployment evidence and a rollback path.
- Different worker implementations remain independently deployable.

### Phase 11: Styles and additional providers

- [ ] Support local prompt presets and pinned reference assets first.
- [ ] Treat remote style/gallery packages as untrusted input with explicit opt-in,
  provenance, size/type validation, and local review.
- [ ] Disable automatic gallery updates in server and worker defaults.
- [ ] Prove a third provider through the SDK before declaring the plugin surface stable.
- [ ] If direct Google Gemini support remains a product goal, implement it as
  `packages/provider-google-gemini`; do not treat the existing GeminiGen service as the
  same provider.
- [ ] Document provider compatibility, quotas, rate limits, session lifetime, and
  execution requirements.

Exit criteria:

- Remote prompt/assets cannot silently enter generation requests.
- The provider SDK is validated beyond the two providers that shaped it.

### Phase 12: Release hardening and rollback

- [ ] Confirm the Phase 1.5 `sub2gen` identity remains consistent across every shipped
  application, package, extension, container, artifact, and document.
- [ ] Remove temporary spike code that was not promoted behind the provider SDK.
- [ ] Add final migration and diagnostic tooling for the unified provider architecture.
- [ ] Run fresh-install, existing-SQLite, PostgreSQL, local-worker, extension, image,
  video, cache, and rollback suites.
- [ ] Publish a provider/worker compatibility matrix and pre-upgrade backup procedure.

Exit criteria:

- No former project-identity alias has been reintroduced.
- The release contains only `sub2gen` commands, imports, environment names, and package
  identities.
- Code rollback and data rollback boundaries are documented and tested.

## Verification matrix

Every phase runs its focused tests plus the existing green gates. Before a production
release, require:

```bash
uv run ruff check .
uv run pyright
uv run pytest -q
bun run check
bun run build
```

Additional required suites:

- Provider conformance tests for Google Flow and ChatGPT.
- Sanitized provider transcript and error-classification tests.
- Protocol golden fixtures in Python and TypeScript.
- Worker pairing, replay, revocation, capability-denial, lease, and cancellation tests.
- SQLite and PostgreSQL migration/repository parity tests.
- Artifact upload, range delivery, retention, and cleanup tests.
- End-to-end API → fake worker → fake provider tests in CI.
- Opt-in local live smoke tests for operator-owned Flow and ChatGPT accounts.

Live subscription generation is never a required CI step and must clearly disclose
possible quota or credit consumption.

## Compatibility and rollback rules

1. Existing Flow routes and models remain the baseline compatibility contract.
2. New tables are additive until their owning migration phase; existing provider tables
   are not destructively rewritten for an identity-only change.
3. Legacy unversioned worker endpoints remain separate from protocol v1 until
   compatibility adapters and client rollout are verified.
4. Provider failures never trigger cross-billing fallback by accident.
5. Every migration phase starts from a clean, pushed `main` and produces an independently
   runnable state.
6. Database migrations are forward-only; rollback after a schema-writing release uses
   the paired pre-upgrade code revision and verified database backup.
7. Real credentials, browser profiles, signed asset URLs, and OAuth files never enter
   fixtures, commits, or diagnostic payloads.

## Implementation policy

Implementation proceeds one phase at a time. At each phase boundary:

1. Update this plan with completed checklist items and any approved deviations.
2. Run the phase-specific and repository-wide verification suites.
3. Review security, credential locality, billing-pool behavior, and compatibility.
4. From Phase 1.5 onward, confirm `uv run setup` and `uv run sub2gen` with no former
   command fallback.
5. Commit and push the completed phase to `main` only after verification.

The ChatGPT spike may be discarded. No later phase may depend on spike code that has not
been brought behind the provider SDK, persistence, worker security, and audit boundaries.
