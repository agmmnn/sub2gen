# Worker Threat Model

- Status: Phase 0 security baseline
- Last reviewed: 2026-08-09

This document defines the minimum security boundary for sub2gen's browser extension,
local image worker, and future provider workers. All execution workers use the canonical
protocol-v1 `/worker_ws` endpoint.

## Scope and security objectives

The system has three logical planes:

```text
API clients
    |
    v
Control plane -----> Artifact plane
    |                      ^
    v                      |
Execution plane -----------+
    |
    v
Provider services and local browser sessions
```

The objectives are:

- a job runs only on an explicitly paired, enabled worker with a locally allowed
  capability, model, account/profile, and server;
- a compromised client cannot turn the control plane or worker into a shell, browser
  proxy, SSRF primitive, or general file-transfer service;
- a compromised control plane cannot silently expand the worker's local authority;
- provider credentials stay at their approved execution location;
- replayed, duplicated, expired, cancelled, or stale-lease jobs cannot create
  unbounded provider usage or overwrite artifacts;
- prompts, reference assets, outputs, credentials, and caller identity are disclosed
  only to the components required to execute and deliver the job; and
- every security-relevant decision is auditable without logging secrets or sensitive
  content by default.

This model does not claim to protect a browser profile from malware or an administrator
already controlling the worker host. OS account separation, disk encryption, endpoint
security, browser updates, and physical access controls remain operator responsibilities.

## Assets

| Asset | Required protection |
| --- | --- |
| Provider cookies, OAuth/access/refresh tokens, session state, and browser profiles | Never returned to the control plane; least-privilege local storage and redaction |
| Worker device private key and revocable credential | Non-exportable OS/extension storage where available; never in logs or job payloads |
| Pairing codes, connection challenges, session tokens, lease IDs, upload grants | Short lived, audience/purpose bound, replay resistant, and redacted |
| Prompts, reference images, generated media, and provider project/conversation state | Confidentiality, integrity, bounded retention, explicit deletion semantics |
| Provider account, credits, quotas, subscriptions, and billing pool | No silent provider/account/billing fallback; bounded concurrency and rate |
| Worker capability policy and approved server identity | Local integrity; remote messages cannot widen it |
| Job state, result metadata, caller attribution, and audit records | Tamper evidence, correlation, minimal retention, no secret material |
| Artifact-store objects and signing keys | Object-scoped authorization, integrity validation, expiry, and revocation |
| Provider adapters, worker executable, extension, and protocol schemas | Version pinning, provenance, code-signing/release integrity where available |

## Actors and trust boundaries

### Expected actors

- **Operator/admin:** pairs devices, approves capabilities/accounts, sets local policy,
  and may revoke either side.
- **API client:** authenticates to the control plane and submits generation requests.
  It is not trusted to select credentials, workers, or billing pools directly.
- **Control plane:** authenticates clients, resolves policy, schedules jobs, grants
  artifact access, and records audit data.
- **Execution worker:** authenticates the server, enforces local policy, obtains local
  credentials, executes one typed capability, and reports typed progress/results.
- **Browser extension/native bridge:** has powerful browser-local authority and is
  trusted only for its allowlisted provider operations.
- **Artifact service:** stores references and results but is not trusted to execute
  jobs or receive provider credentials.
- **Provider service:** receives user content and consumes provider quota according to
  its technical behavior and security controls.

### Boundary A: API client to control plane

The client is untrusted. Authentication, request size/type validation, model-policy
resolution, rate limits, and content authorization happen before a job is offered.
Caller-supplied provider IDs, worker IDs, account locators, file paths, billing pools,
URLs, or capabilities are requests at most; they are never authority.

### Boundary B: control plane to execution plane

This is a mutually authenticated, encrypted, replay-resistant channel. TLS authenticates
the network endpoint, while pairing binds a durable worker device key to an approved
server relationship. The worker treats the control plane as a job source, not as a
fully trusted local administrator, and applies its own immutable-per-job policy checks.

### Boundary C: worker to browser/provider credentials

The worker crosses into the highest-value local boundary. A provider adapter receives
an opaque local credential/profile reference after local policy resolution. Raw
cookies, OAuth tokens, browser storage, arbitrary page data, and generic debugger
access never enter protocol messages.

### Boundary D: control/execution planes to artifact plane

Reference uploads and generated outputs cross a separate data path. Authorization is
object and operation specific. Neither a WebSocket job credential nor an artifact
grant confers general bucket, filesystem, URL-fetch, or control-plane access.

### Boundary E: worker and artifact plane to provider networks

Outbound destinations are provider-adapter-owned and allowlisted. Redirects, DNS
rebinding, signed-CDN URLs, content type, and size are validated. A job cannot supply a
new destination or proxy arbitrary bytes through the worker.

## Threat scenarios and required controls

| Scenario | Required controls | Residual risk / response |
| --- | --- | --- |
| Stolen or guessed pairing code enrolls an attacker's worker | Admin authentication; at least 128 bits of random value; single use; short TTL; server/audience binding; attempt limit; show device fingerprint and requested capabilities before approval | Revoke device, terminate sessions, rotate server pairing secret, audit enrollment source |
| Worker pairs with an impersonated control plane | Valid TLS with hostname verification; explicit server URL; display/pin server identity during pairing; device key proof; never accept plaintext `ws://` except loopback development | A compromised trusted CA or local host remains an operator-level risk |
| Captured registration or job is replayed | Server challenge nonce; signed proof bound to protocol/session; unique message ID; bounded timestamp skew; replay cache; job ID + attempt + lease ID; deadline | Deduplicate provider side effects when possible and flag repeated IDs |
| Disconnected worker submits a late result | Reject expired/revoked session and stale lease IDs; terminal job state is monotonic; artifact commit is compare-and-set | Provider action may already have consumed quota; audit orphaned results |
| Compromised API client selects another account or paid fallback | Server-owned execution policy; API-key scope/account assignment; no caller-controlled secret/profile/worker/billing fields; explicit paid-fallback policy | Audit requested and resolved execution separately |
| Compromised control plane asks a local worker to do more than expected | Worker-side server, capability, provider, model, profile, concurrency, daily-use, and optional confirmation policies; visible pause/revoke; no remote policy widening | A permitted generation can still consume quota or submit harmful content; keep limits and provider safeguards |
| Forged caller identity is used to satisfy local policy | Caller claims are advisory unless cryptographically signed by a configured issuer and bound to the job/session/audience; even verified claims cannot expand local capability/account policy | A compromised issuer can lie. Use claims for audit or narrowing, never as the sole grant for sensitive capability |
| Arbitrary shell/browser/file command is smuggled in a job | Closed typed schemas; reject unknown fields/types/versions; adapter-owned command templates; no shell interpolation; no `eval`, generic script, debugger, filesystem path, executable, environment, or command capability | Provider UI changes may tempt unsafe escape hatches; add a new reviewed capability instead |
| Job turns worker into SSRF/open proxy | No caller URL fetch; reference inputs are artifact IDs, inline bounded bytes, or provider-owned signed references; egress allowlist; resolve/validate redirects and IP class; deny local/link-local/metadata destinations | Provider CDNs change; update an explicit allowlist rather than accept arbitrary URLs |
| Malicious reference or output exhausts disk/memory or exploits a parser | Streaming size limits; MIME plus magic-byte validation; image dimension/frame limits; quarantine temp directory; safe decoder; filename replacement; process/time limits; cleanup on every terminal path | Complex media decoders remain a supply-chain risk; patch and sandbox where practical |
| Artifact grant is stolen or overbroad | HTTPS; short TTL; single job/attempt/object; one operation; maximum byte count and expected MIME/hash; random object key; no list/delete; consume or revoke after commit | A bearer grant can be used until expiry; keep TTL shorter than job lease where practical |
| Result object is replaced or attached to another job | Worker reports size/hash/type; artifact plane verifies them; job/attempt/lease encoded in authorization and metadata; immutable object names; atomic result commit | Retain audit hash and reject duplicate terminal commits |
| Prompt/reference/output leaks through logs or telemetry | Structured allowlist logging; IDs and hashes by default; explicit diagnostic mode with bounded retention; redact credentials, URLs, query strings, headers, cookies, prompts, base64, local paths, and provider responses | Operators may intentionally enable content logging; UI must warn and expire it |
| Worker/extension update is compromised | Pin source/version/hash; signed releases where available; dependency review; least-privilege extension permissions; explicit update channel; rollback | External runtime compromise can control the browser; pause workers and revoke credentials immediately |
| Provider rate/safety controls are bypassed | Never solve or bypass provider account challenges as a generic capability; respect rate/credit/safety outcomes; bounded retries with jitter and budgets; no profile rotation to evade limits | Provider can suspend the account or change behavior; fail closed and surface the error |
| Worker is left online after ownership changes | Server and worker both support disable/revoke; credential/session expiry; last-seen inventory; local "forget server" action wipes relationship material | Revoke provider sessions separately if browser/OAuth compromise is suspected |

## Pairing and connection protocol

Minimum pairing flow:

1. The worker creates a device key pair locally and displays its fingerprint.
2. An authenticated administrator creates a random, short-lived, single-use pairing
   code for one server and worker kind.
3. Over TLS, the worker sends the code, public key, worker kind, protocol versions, and
   requested capability names. It sends no provider secret.
4. The administrator confirms the device fingerprint and grants a subset of requested
   capabilities. Requested capabilities are never automatically approved.
5. The server stores the device public key and issues a revocable relationship
   credential scoped to the worker and server.
6. The worker stores the server identity, granted relationship, and its **local** policy.
   The server cannot mutate the local policy through a later job or registration.

Every connection then:

1. validates TLS and the configured server identity;
2. negotiates one supported protocol version and fails closed on an unknown version;
3. proves possession of the device key over a fresh server challenge bound to the
   connection, audience, and expiry;
4. receives a short-lived session authorization bound to the worker and granted
   capabilities; and
5. rotates/ends that authorization on reconnect, revoke, or protocol downgrade.

Browser extensions may use extension-appropriate key storage and signed challenges
rather than mutual TLS. A static API key in extension storage is insufficient as the
long-term design.

## Job authorization and replay rules

Before acceptance, the worker validates:

- negotiated protocol version and exact typed `job_kind`;
- authenticated server and non-revoked worker relationship;
- job ID, attempt number, fresh lease ID, issued time, deadline, and maximum runtime;
- approved capability plus the local server/provider/model/account/profile policy;
- concurrency, daily-use, artifact-size, and optional human-confirmation limits; and
- reference-asset descriptors and upload authorization shape.

Delivery is at-least-once. The worker persists a bounded deduplication record for
`(server, job_id, attempt)` through the retry window. Repeated offers return the prior
accept/reject/terminal state rather than repeating the provider action. A new attempt
requires a new lease. A stale lease can never publish a terminal result.

Cancellation is best effort: stop the process tree/browser action, revoke unused
artifact grants, clean temporary files, and emit one terminal cancellation event. If a
provider operation cannot be cancelled, the worker marks it orphaned and must not
quietly schedule another paid attempt.

## Capability design

Capabilities describe one business operation, such as:

```text
captcha.solve
session.refresh:google-flow
image.generate:google-flow
image.generate:chatgpt-web
image.generate:chatgpt-codex
```

They do not describe implementation primitives. The following remote capabilities are
prohibited:

- arbitrary shell commands, executables, arguments, environment variables, or package
  installation;
- arbitrary JavaScript/`eval`, Chrome DevTools Protocol commands, selectors, page
  reads, cookie reads, screenshots, tabs, or browser navigation;
- arbitrary URLs, HTTP methods/headers/bodies, redirects, WebSockets, DNS targets, or
  proxying;
- arbitrary local paths, file reads/writes/deletes, globbing, archive extraction, or
  OS keychain queries; and
- disabling safety controls, solving generic access challenges, rotating accounts to
  evade limits, or changing the worker's own allowlist.

Provider adapters may internally perform the minimum fixed operations required for an
approved capability. Their command, URL, browser-origin, and filesystem boundaries are
code-owned and reviewable. A new primitive requires a new narrowly named capability,
schema, threat review, and explicit local approval.

## Artifact grants and media handling

Large media travels outside the worker WebSocket. The control plane issues separate
reference-download and result-upload grants that are:

- scoped to one authenticated worker, job, attempt, lease, object key, direction, and
  operation;
- short lived and revocable, with maximum bytes, expected media class/MIME, and an
  optional expected digest;
- unable to list a bucket, choose a path, overwrite another object, fetch an arbitrary
  URL, or delete data; and
- committed only after server-side size, type, digest, and current-lease verification.

Reference downloads must not reveal storage credentials or internal filesystem paths.
Generated artifacts are first written to a worker-owned random temporary directory,
validated, uploaded, and then removed. The worker never trusts a filename from a
provider, client, `Content-Disposition`, or archive member.

## Logging, diagnostics, and redaction

Normal logs may contain:

- timestamp, server/worker/job/attempt/lease IDs;
- protocol version, capability, provider, requested and resolved model;
- opaque provider-account and API-key IDs;
- state transitions, durations, byte counts, hashes, and stable error codes; and
- policy decision and revocation reason.

Normal logs must not contain:

- pairing codes, private keys, relationship/session credentials, upload grants;
- Authorization/Cookie headers, access/refresh/session tokens, raw browser storage;
- prompts, reference or output bytes/base64, full provider responses;
- signed asset URLs or URL query strings; or
- local usernames, profile paths, home paths, environment dumps, or command lines that
  could contain secrets.

Errors are mapped to stable typed codes before crossing the worker boundary. Provider
response excerpts require explicit local diagnostic mode, visible warning, size/time
limits, automatic expiry, and another redaction pass. Sanitized characterization
fixtures use synthetic IDs and content; search the fixture tree for known token, cookie,
home-path, email, and signed-URL patterns before commit.

## Revocation and incident response

Revocation exists on both sides:

- **Server-side disable:** stop new offers, reject heartbeats/results, revoke sessions
  and artifact grants, and mark active leases cancelled.
- **Worker-side forget/pause:** immediately close the channel, reject queued jobs,
  remove the server relationship and local grants, and optionally retain a redacted
  audit export.
- **Capability/account revoke:** takes effect before the next acceptance and cancels
  locally queued work; running work follows the configured sensitive-operation policy.
- **Key compromise:** rotate the device key and create a new pairing relationship;
  never reuse the old device identity as proof.

If provider credentials or the browser may be compromised, worker revocation is not
enough. The operator must also sign out/revoke the provider session, rotate OAuth/API
credentials, inspect provider account history, and quarantine generated artifacts.

Security events that require an audit entry include pairing, approval, policy change,
failed proof/replay, capability rejection, repeated/stale result, artifact validation
failure, diagnostic-mode enablement, and every revoke/rotate action.

## Phase exit checks

Before protocol v1 or a new local image worker can leave development:

- pairing, proof-of-possession, expiry, replay, revoke, and protocol-negotiation tests
  pass;
- an adversarial schema suite confirms unknown fields/types and prohibited primitives
  fail closed;
- duplicate, stale-lease, late-result, cancellation, and reconnect tests pass;
- artifact tests cover cross-job access, oversize/type/hash mismatch, expiry, redirect,
  local-network URL, overwrite, and cleanup;
- logs and fixtures pass automated secret/path/content scans;
- local capability/account/model policies are tested against a malicious or compromised
  control-plane simulator; and
- an operator can visibly pause, forget, and re-pair a worker without editing storage.
