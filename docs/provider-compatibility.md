# Provider compatibility

| Provider | Canonical model | Execution | Credential | References | Concurrency | Session / quota behavior |
| --- | --- | --- | --- | --- | --- | --- |
| Google Flow | `google-flow/*` | API host or paired browser worker | Flow session token | Model-dependent | Account/project limits apply | Session tokens expire and are refreshed by the paired extension; credits and upstream throttles are surfaced as provider failures. |
| ChatGPT Web | `chatgpt/gpt-image-web` | Local image worker | Logged-in browser profile | Up to 14 | 1 per browser surface | Browser login lifetime controls availability; the provider never switches to the Codex billing pool. |
| ChatGPT Codex | `chatgpt/gpt-image-codex` | Local image worker | Local OAuth credential | Provider-dependent | Worker-advertised | OAuth expiry and subscription quota are reported by the worker; the provider never switches to ChatGPT Web. |
| Google Gemini | `google-gemini/gemini-2.5-flash-image` | API host | `api_key` environment binding | Up to 14 | 2 by default | HTTP 429 is classified as quota/rate pressure; authentication and transient server failures remain distinct. |

Provider accounts and credential bindings are configured under **Platform** in the
admin interface. For direct Gemini, create a `google-gemini` account and an `api_key`
binding with storage kind `env` and a locator such as
`env://SUB2GEN_GOOGLE_GEMINI_API_KEY`, then assign that account to the calling managed
API key.

## Style presets

Local styles live under `.runtime/data/styles/local`. Each JSON manifest may define:

```json
{
  "id": "cinematic",
  "name": "Cinematic",
  "prompt_prefix": "cinematic still",
  "prompt_suffix": "soft natural light",
  "references": ["reference.png"]
}
```

The `style` field on `/v1/images/generations` selects a preset. Reference paths are
resolved inside the manifest directory and are pinned local PNG, JPEG, or WebP files.
Use `GET /v1/styles` to inspect the active set.

Remote ZIP packages are never fetched or activated automatically. The style registry
requires an expected SHA-256 digest, validates archive paths/types/sizes, stages the
package outside the active registry, and requires an explicit local review action. Even
reviewed packages remain disabled unless `SUB2GEN_ENABLE_REMOTE_STYLES=true` is set.
