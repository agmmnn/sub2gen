# Legacy worker wire fixtures

These fixtures characterize the two unversioned worker dialects that existed before
the canonical worker protocol. They are compatibility inputs, not the design for the
new protocol.

Every WebSocket frame round-trips through the executable compatibility codecs in
`sub2gen.workers.extension.legacy_codec` and `sub2gen_gateway.legacy_codec`. Current
runtime handlers are intentionally unchanged; protocol v1 will consume these codecs at
the compatibility boundary.

- `captcha-ws.legacy.json` freezes the `/captcha_ws` extension registration,
  CAPTCHA, session refresh, generation relay, upload side channel, heartbeat, and
  shutdown frames.
- `agent-gateway.legacy.json` freezes `/ws/agents` registration and solve job frames.

All credential, token, browser-fingerprint, generated ID, and upload-body values use
angle-bracket placeholders. Never replace them with captured browser traffic. A fixture
change means the legacy compatibility contract changed and must be reviewed alongside
the relevant runtime source listed in its `sources` field.
