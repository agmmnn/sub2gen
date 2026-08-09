# CLIProxyAPI Railway service

Create a second Railway service from this directory and attach a persistent
volume at `/data`. The image is pinned to CLIProxyAPI `v7.2.120`.

Required variables on the CLIProxy service:

- `CLIPROXY_CLIENT_API_KEY`: a URL-safe key used only by sub2gen inference.
- `MANAGEMENT_PASSWORD`: a separate strong management key. CLIProxy reads it
  from the environment and never writes it to the volume.

Required variables on the sub2gen service:

- `SUB2GEN_CLIPROXY_BASE_URL=http://cliproxy.railway.internal:8317`
- `SUB2GEN_CLIPROXY_PUBLIC_URL=https://<cliproxy-public-domain>`
- `SUB2GEN_CLIPROXY_API_KEY=<same client key>`
- `SUB2GEN_CLIPROXY_MANAGEMENT_KEY=<same management key>`
- `SUB2GEN_CLIPROXY_VERSION=v7.2.120` (optional display override)

The bootstrap creates `/data/config.yaml` only once. Later management changes,
OAuth credentials, account status, aliases, and exclusions remain on the
volume. Inference uses Railway private networking. Expose port `8317` publicly
only for OAuth callbacks and protected break-glass access to
`/management.html`.

Image and video generation are disabled, while image inputs to chat completion
requests remain available for prompt cloning and metadata analysis.

## Import all Codex or Antigravity accounts from Cockpit Tools

Cockpit Tools v1.3.15 can export the selected Codex accounts as one portable
JSON array. In Cockpit, select all required accounts, click Export, keep the
`Cockpit Tools` format, and save the JSON file. Then open sub2gen **Manage →
AI Gateway → Add account → Credential file**, select **Codex**, choose that one
file, and click **Import account(s)**. sub2gen converts both portable Cockpit
records and older nested `tokens` records, strips unrelated sensitive note
fields, and imports up to 100 accounts in one action.

The same workflow applies on Cockpit's Antigravity account page: select the
accounts, export the single JSON file, choose **Gemini / Antigravity** in
sub2gen, and import it once. Cockpit's Antigravity export contains each
account's refresh token; CLIProxy refreshes the access token and discovers the
Google Cloud project on the first request.

After confirming the imported accounts are healthy, stop Cockpit's local API
Service for those accounts. Running both gateways against the same rotating
OAuth refresh tokens can invalidate a refreshed session in the other gateway.
