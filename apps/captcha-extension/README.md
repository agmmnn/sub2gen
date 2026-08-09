# sub2gen Worker extension

This Chrome Manifest V3 extension can connect as an end-user, CAPTCHA, or
token-bound refresh worker. End-user mode can also import and periodically sync
the Google account signed in to the current Chrome profile.

## Build and load

From the repository root:

```bash
bun install --frozen-lockfile
bun run --cwd apps/captcha-extension build
```

Open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**,
and select `apps/captcha-extension/dist/`. Reload the unpacked extension after
every source rebuild.

The source lives in `src/`; `dist/` is generated and intentionally ignored by
Git. `bun run --cwd apps/captcha-extension check` runs strict checks for the
extracted state modules, the worker/account tests, and the production bundle.

## Using the extension

Click the extension icon for everyday controls: connection status, account
sync, CAPTCHA testing, reconnect, automation switches, worker-tab control, and
recent activity. Use the gear button only for credentials, worker role, sync
intervals, and advanced CAPTCHA tuning. Choose **Open diagnostics** for the
complete runtime overview, CAPTCHA and generation job history, session-token
captures, and the full activity log.

## Modes

- **My account** authenticates with a managed API key. It can solve
  CAPTCHA jobs assigned to that key and, with the `tokens:import` scope, import
  or automatically synchronize the current Google account.
- **CAPTCHA only** authenticates with a server-side CAPTCHA worker key and
  accepts CAPTCHA jobs only.
- **Refresh only** binds one Chrome profile to an existing token ID and
  accepts session-token refresh jobs only.

One extension instance has one active mode. Use a second Chrome profile only
when you deliberately need independent modes online simultaneously. End-user
mode normally avoids that requirement because it supports both CAPTCHA work and
current-account synchronization.

## Runtime boundaries

Extension-specific state is split into storage, REST API, WebSocket,
account-sync, and worker-mode modules under `src/state/`. Only stable,
Chrome-independent JSON request, URL, and storage adapters are shared through
`@sub2gen/extension-core`.
