# sub2gen worker protocol

This workspace package is the canonical wire contract for new sub2gen workers. The
JSON Schema in `schema/worker-protocol-v1.schema.json` generates the Python and
TypeScript message types consumed by the API, local workers, and browser extensions.

Protocol `1.0` uses explicit job IDs, attempts, deadlines, lease IDs, capability names,
structured errors, cancellation, progress, terminal results, and worker session IDs.
Unversioned clients are classified as legacy and remain on their existing codec; a
server never sends v1 frames until the client advertises and negotiates `1.0`.

Regenerate and verify bindings:

```bash
bun run --cwd packages/worker-protocol generate
bun run --cwd packages/worker-protocol check
uv run pytest packages/worker-protocol/tests
```

The runtime helpers also provide device proof-of-possession pairing, worker-side
allowlist checks, ephemeral at-least-once leases, and job-scoped single-use artifact
upload grants. API-key identity is display/audit metadata and is not treated as an
independently authenticated caller identity by a worker.
