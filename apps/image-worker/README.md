# sub2gen image worker

The image worker runs on the computer that owns the logged-in Chrome session. It keeps
the Ed25519 device key, Chrome profile reference, and any Codex OAuth state local; the
API receives only an opaque account/profile reference and generated artifacts.

```bash
uv run sub2gen-image-worker init
uv run sub2gen-image-worker pair PAIRING_CODE
uv run sub2gen-image-worker health
uv run sub2gen-image-worker run
```

Create the one-time pairing code with a managed API key that has `workers:pair` (or
`*`) scope:

```bash
curl -X POST http://127.0.0.1:8000/api/workers/pairing-code \
  -H "Authorization: Bearer $SUB2GEN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"ttl_seconds":300}'
```

Configuration defaults to `~/.config/sub2gen/image-worker.json`; the private device key
is stored beside it with mode `0600`. Set `imagegen_executable` to the history-linked
`vendor/chatgpt-imagegen/chatgpt-imagegen` executable and `chrome_use_executable` to
`chrome-use` 1.5.87 or newer. `health` reports missing executables, version mismatches,
and browser readiness without exposing browser or OAuth credentials.
