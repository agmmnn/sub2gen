# Worker protocol v1

Protocol `1.0` is the canonical transport for new local workers. Its source of truth is
`packages/worker-protocol/schema/worker-protocol-v1.schema.json`; committed Python and
TypeScript types are generated from that schema and checked for drift in both test
suites.

## Negotiation and lifecycle

A new worker sends `worker.hello` with `supported_versions`. The server selects `1.0`,
issues a proof-of-possession challenge, validates `worker.register`, and replies with
`worker.registered`. A client with no version list is legacy and remains on its frozen
unversioned worker codec. Missing or unsupported versions are rejected.

Registered sessions use `worker_session_id`, while a provider-specific solve or browser
session stays inside the typed job input. Heartbeats carry the active lease IDs and free
slots. Offers are not assigned until `job.accept`; disconnects expire active offers and
leases so the durable job can be retried. Every progress, result, error, and cancellation
contains the job ID, attempt, and lease ID. A result for an old attempt or lease is
rejected.

## Device identity and local policy

Pairing codes are short-lived and single-use. The device generates an Ed25519 key pair,
stores the private key locally, and sends only the public key during pairing. The API
stores the durable device record and reloads it after restart. Every WebSocket connection
must sign a short-lived single-use challenge. Devices can be revoked or expire.

Workers validate the server, capability, model, provider account, concurrency, and daily
job limit against their local `WorkerPolicy`. Capabilities are explicit; there is no
shell, arbitrary browser evaluation, or arbitrary URL capability. An API-key identity in
a job is audit/display metadata, not independent caller authentication.

## Job and artifact semantics

- Generation and CAPTCHA work use the same offer/accept/progress/result lifecycle.
- Deadlines are transmitted in the offer instead of being an API-only timeout.
- `captcha.solve` uses a normal job ID; provider fingerprints and solve-session IDs are
  distinct typed input fields rather than overloaded correlation IDs.
- The server owns terminal-response persistence; a worker only emits one terminal frame.
- Both peers send explicit heartbeat messages; no dialect-specific ping behavior leaks
  into v1.
- Large artifacts use short-lived single-use grants bound to a worker and job. Grants
  enforce content type, maximum bytes, optional SHA-256, ownership, and cleanup. A device
  credential alone does not grant upload access.

The legacy translators are intentionally adapters: they map frozen frames into v1 for
internal lifecycle handling without adding fields to either legacy wire dialect.
