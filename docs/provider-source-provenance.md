# Provider Source Provenance

- Status: active engineering record
- Last verified: 2026-08-09

This document records the exact external source revisions used to design the local
ChatGPT image-generation adapter. Runtime behavior and operational constraints are
documented here so upgrades can be reviewed and reproduced.

## Pinned upstream revisions

| Component | Upstream | Inspected commit | Version | License | Relationship |
| --- | --- | --- | --- | --- | --- |
| `chatgpt-imagegen` | [`leeguooooo/chatgpt-imagegen`](https://github.com/leeguooooo/chatgpt-imagegen) | `5b1ccb6ded09997317d35717b4b0183c268c0e9b` | `0.21.2` | MIT, copyright 2026 leeguooooo | Pinned external CLI used by `packages/provider-chatgpt` |
| `chrome-use` | [`leeguooooo/chrome-use`](https://github.com/leeguooooo/chrome-use) | `a107f7e74ee014db68bdce8d0dd8c570f858afd0` | `1.5.87` | Apache-2.0 | Version-pinned external browser runtime; it is not vendored into sub2gen |

## `chatgpt-imagegen` behavior used by sub2gen

- The `web` backend invokes `chrome-use`, drives an already logged-in ChatGPT browser,
  selects or creates a ChatGPT Project, submits a prompt, retrieves image bytes, and
  normally deletes the generated conversation.
- The `codex` backend is a different execution and quota path. Phase 1 always passes
  `--backend web`; it never selects `auto` and cannot silently fall back to Codex.
- Web generation is cross-process serialized through the upstream file-lock mechanism.
  sub2gen also adds an in-process async lock and forces
  `CHATGPT_IMAGEGEN_WEB_CONCURRENCY=1`.
- The provider passes `--no-style`; sub2gen styles are resolved locally before provider
  execution and use no automatic online gallery input.
- The CLI emits the saved file path on stdout and diagnostics on stderr. Phase 1 treats
  those strings as an adapter seam behind the provider SDK.

The vendored source preserves the upstream MIT license, copyright notice, pinned
commit, and a record of local modifications.

## `chrome-use` runtime boundary

- The native CLI controls Chrome through its extension, native-messaging host, and
  per-session local daemon/socket.
- sub2gen invokes only the commands needed by the upstream image CLI plus a best-effort
  `close --session <id>` after wrapper timeout or cancellation.
- The provider does not expose arbitrary browser commands through HTTP or WebSocket
  routes.
- `chrome-use` remains independently installed and upgradeable. Before changing the
  pinned tested version, rerun doctor, text-to-image, image-to-image, timeout, and
  cancellation checks.

## Provider terminology

- **Google Flow** is the existing Labs/Flow project and artifact surface.
- **GeminiGen** is the repository's existing integration with the separate
  `geminigen.ai` service; it is not direct Google Gemini API support.
- **ChatGPT Web**, **ChatGPT Codex**, and the **OpenAI API** are distinct execution and
  quota surfaces. Model IDs and routing must keep them distinct.
- **Nano Banana** is a model/product nickname, not a credential or provider type. Its
  model ID must remain namespaced under the provider that executes it.
