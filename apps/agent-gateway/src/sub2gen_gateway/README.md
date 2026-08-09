# Agent Gateway (Phase 1)

FastAPI service that implements the same **HTTP** contract as sub2gen’s `remote_browser` client (`/api/v1/solve`, etc.) and accepts **WebSocket** connections from user machines at `/ws/agents`.

## Data model (MVP)

- **In-process only:** connected-agent pool dispatch (no token_id routing).
- **Redis** is in `infra/compose/docker-compose.agent.yml` (merge with `infra/compose/docker-compose.yml`); the gateway MVP does not require Redis logic yet.
- Optional Pydantic shapes: [`schemas.py`](schemas.py).

## Run (local)

```bash
export GATEWAY_SUB2GEN_BEARER=your-secret   # must match sub2gen remote_browser_api_key
export GATEWAY_SUB2GEN_BEARER_PREVIOUS=old-secret   # optional during rotation window
export GATEWAY_AGENT_AUTH_MODE=legacy         # legacy | keygen | dual
export GATEWAY_AGENT_DEVICE_TOKEN=agent-secret
uv run --group agent-gateway python -m sub2gen_gateway
```

Health: `GET http://127.0.0.1:9080/health`

## Docker

This directory contains the gateway runtime. Build it with
`infra/docker/Dockerfile.agent-gateway` from the repository root.

## WebSocket protocol

1. Connect to `ws://<host>:9080/ws/agents`.
2. Send one JSON line (`register`) in one of these modes:
   - Legacy: `{"type":"register","device_token":"<GATEWAY_AGENT_DEVICE_TOKEN>"}`
   - Keygen (jwt): `{"type":"register","agent_token":"<keygen-token>"}`
   - Keygen (introspection): `{"type":"register","agent_token":"<keygen-token>","agent_token_id":"<keygen-token-uuid>"}`
3. Receive `solve_job` messages; reply with `solve_result` or `solve_error`:

```json
{"type":"solve_result","job_id":"...","token":"...","session_id":"...","fingerprint":{}}
```

```json
{"type":"solve_error","job_id":"...","error":"reason"}
```

## Keygen mode (production)

Set `GATEWAY_AGENT_AUTH_MODE=keygen` (or `dual` during migration) and configure:

- `KEYGEN_VERIFY_MODE=jwt|introspection`
- `KEYGEN_PUBLIC_KEY` (jwt mode) or `KEYGEN_API_TOKEN` + `KEYGEN_API_URL` (introspection mode)
- `KEYGEN_ACCOUNT` (required for account-scoped introspection)

In Keygen mode, each agent sends `agent_token` and receives `registered` with identity metadata.

Auth boundary reminder:

- Keygen secures WS agent identity (`/ws/agents`).
- HTTP `/api/v1/*` is still protected by `GATEWAY_SUB2GEN_BEARER`.
