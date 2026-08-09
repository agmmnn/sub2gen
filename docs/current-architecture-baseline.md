# Current architecture baseline

sub2gen is a local-first modular monolith with independently runnable browser workers.

## Applications

- `apps/api`: FastAPI control plane, OpenAI-compatible API, routing, jobs, audit, and
  server-executed providers.
- `apps/admin-web`: operator UI for providers, accounts, workers, models, and jobs.
- `apps/image-worker`: local ChatGPT image execution worker.
- `apps/captcha-extension`: paired Chrome worker for Google Flow CAPTCHA, session
  refresh, account import, and page-origin HTTP relay.
- `apps/metadata-extension`: Adobe metadata browser integration.

## Packages

- `packages/provider-sdk-python`: provider request/result/error contracts.
- `packages/provider-google-flow`: Google Flow provider implementation.
- `packages/provider-chatgpt`: ChatGPT provider implementation.
- `packages/worker-protocol`: generated Python and TypeScript protocol-v1 contracts,
  pairing, leases, cancellation, heartbeats, and artifact grants.
- `packages/api-contract`: generated TypeScript API types.
- `packages/extension-core`: shared extension transport and storage helpers.

## Execution path

1. The API authenticates a request and resolves its requested model through the model
   catalog.
2. Trusted routing selects an enabled provider account, credential kind, billing pool,
   and, when required, a connected paired worker.
3. Server providers execute in-process. Browser-backed providers receive a leased
   protocol-v1 `job.offer` through `/worker_ws`.
4. Artifacts are committed once through the cache boundary and their metadata is stored
   with the generation job.
5. Requested and resolved execution identities are visible in the control plane and the
   durable audit record.

## Worker boundary

`/worker_ws` is the only worker WebSocket. A worker is paired once with an Ed25519 public
key, proves possession for every connection, negotiates protocol `1.0`, advertises its
approved capabilities, sends heartbeats, and follows offer/accept/result or error/cancel
lifecycle messages. Unversioned worker endpoints and compatibility codecs are not
shipped.

## Persistence

SQLite and PostgreSQL share numbered migrations and repository contracts for provider
accounts, credential bindings, worker devices, generation jobs, attempts, artifacts,
API-key assignments, and operator audit events. List-facing records never include raw
secret references or device authentication material.
