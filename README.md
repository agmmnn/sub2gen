# sub2gen

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.119.0-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

**A full-featured OpenAI-compatible API service that provides a unified interface for Flow**

</div>

## ✨ Core features

- 🎨 **Text-to-image** / **image-to-image**
- 🎬 **Text-to-video** / **image-to-video**
- 🎞️ **First-and-last-frame video generation**
- 🔄 **Automatic AT/ST refresh** - Refreshes expired access tokens automatically and renews expired session tokens through the browser in `personal` mode
- 📊 **Credit display** - Queries and displays VideoFX credits in real time
- 🚀 **Load balancing** - Multi-token rotation and concurrency control
- 🌐 **Proxy support** - Supports HTTP and SOCKS5 proxies
- 📱 **Web administration interface** - Intuitive token and configuration management
- 🎨 **Continuous image-generation conversations**
- 🧩 **Official Gemini request compatibility** - Supports `generateContent`, `streamGenerateContent`, `systemInstruction`, and `contents.parts.text/inlineData/fileData`
- ✅ **Verified official Gemini image output** - Tested with a real token to confirm that `/models/{model}:generateContent` returns `candidates[].content.parts[].inlineData`

## 🚀 Quick start

### Breaking identity cutover

`sub2gen` is the only supported project, package, command, environment-variable,
JavaScript-scope, extension-storage, and container identity. There are no compatibility
aliases for the former identity.

The `0002_sub2gen_identity` database migration disables managed API keys created before
the cutover because their plaintext secrets cannot be renamed. After the first upgraded
startup, create a new `s2g_live_...` key in **API key manager**, reload the unpacked Chrome
extensions, and save their connection again. The separately configured global API key is
not a managed key and is unchanged.

The SQLite file and encrypted-backup manifest also use the new identity. Stop the server
before upgrading an existing checkout, rename `.runtime/data/flow.db` (and any matching
`-wal`/`-shm` companions) to `.runtime/data/sub2gen.db`, then start the new command. Create
a fresh Google Drive backup after the upgrade; pre-cutover backup archives are intentionally
not accepted by the new backup format.

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Bun](https://bun.sh/) for the shortest local setup. uv installs Python 3.11 when necessary.
- Docker and Docker Compose only when you want container deployment.

### Repository layout

- `apps/api/`: the installable `sub2gen` Python package and backend tests
- `apps/admin-web/`: the React administration interface
- `apps/captcha-extension/`: the Chrome CAPTCHA/account-sync worker
- `apps/metadata-extension/`: the TypeScript metadata extension
- `infra/docker/` and `infra/compose/`: container images and Compose stacks
- `packages/api-contract/`: TypeScript contracts generated from the backend OpenAPI snapshot
- `packages/extension-core/`: stable browser-independent extension transport/storage primitives
- `.runtime/`: local databases, browser profiles, generated files, and cache data

If you are upgrading a checkout created before this layout, stop sub2gen and
move the old local runtime directories once:

```bash
mkdir -p .runtime
mv data .runtime/data
mv tmp .runtime/tmp
```

Skip a move when its legacy directory does not exist. Do not overwrite an
existing `.runtime/data`; back up both directories and reconcile them first.

Flow now requires an additional CAPTCHA. You can solve it through a browser or a third-party service.

- To use YesCaptcha, [register here](https://yescaptcha.com/i/13Xd8K), obtain an API key, and enter it in the **YesCaptcha API key** field on the system settings page.
- The admin UI supports these YesCaptcha task types: `RecaptchaV3TaskProxyless`, `RecaptchaV3TaskProxylessM1`, `RecaptchaV3TaskProxylessM1S7`, and `RecaptchaV3TaskProxylessM1S9`. `M1S9` is currently recommended by default. S7 and S9 force `minScore` values of 0.7 and 0.9 respectively.
- The default `infra/compose/docker-compose.yml` is intended for third-party solvers such as YesCaptcha, CapMonster, EzCaptcha, or CapSolver. For headed `browser` or `personal` solving inside Docker, use `infra/compose/docker-compose.headed.yml` below.
- To test `remote_browser` mode locally, run the Node mock solver on the host. It verifies HTTP and authentication only and does not produce real reCAPTCHA tokens. See [`tools/remote-browser-mock/`](./tools/remote-browser-mock/).
- For asynchronous submission and polling through `/v1/async/chat/completions` and `/v1/jobs/{job_id}`, see [`docs/async-polling.md`](./docs/async-polling.md).
- Runway web-task integration is available through the admin `Runway` tab, `runway-*` models, and `/v1/runway/*` routes. See [`docs/runway.md`](./docs/runway.md). It includes a manifest-backed model registry, live feature sync, real Runway uploads/datasets, image/video/audio/upscale task builders, OpenAI-compatible dispatch, voices, estimates, cancel, async polling, and cache mirroring.
- Production performance, Railway Redis, WebSocket events, and seven-day retention are documented in [`docs/performance-redis-rollout.md`](./docs/performance-redis-rollout.md). The PostgreSQL 16 bridge, migration, encrypted Google Drive backup, cutover, and rollback procedure is in [`docs/postgres-migration-runbook.md`](./docs/postgres-migration-runbook.md).

- The bundled [`apps/captcha-extension/`](./apps/captcha-extension/) supports CAPTCHA work, current-account import, scheduled ST/cookie synchronization, and token-bound refresh workers. Build it with `bun run --cwd apps/captcha-extension build`, then load `apps/captcha-extension/dist/` in Chrome.

### Chrome Extension per-key isolation setup

When using captcha method `extension`, sub2gen keeps one global captcha mode but isolates end-user workers per managed API key.

1. Run `bun install --frozen-lockfile` and `bun run --cwd apps/captcha-extension build`, then load [`apps/captcha-extension/dist/`](./apps/captcha-extension/) as an unpacked Chrome extension.
2. Create a managed API key in **API key manager**. Add `tokens:import` if this extension may add or refresh the Google account signed in to its Chrome profile.
3. Open the extension settings, select **My account**, enter the WebSocket URL and managed API key, then save.
4. Click the extension icon and choose **Sync Google account** to create or update the dashboard token by Google email. The imported account is automatically assigned to that managed key, uses `protocol` refresh, and stores the required Google cookies.
5. Optionally enable **Account sync** in the popup and choose its interval under settings. This keeps the account current without a separate Refresh-only profile.

Use **CAPTCHA only** for server-wide CAPTCHA capacity, or **Refresh only** when binding a dedicated Chrome profile to an already existing Token ID.

If a managed key has no matching extension worker online, requests wait up to `extension_queue_wait_timeout_seconds` and then fail (no gateway fallback).

### Option 1: Docker deployment (recommended)

#### Standard mode (without a proxy)

```bash
# Clone the project
git clone https://github.com/agmmnn/sub2gen.git
cd sub2gen

# Start the service
docker compose -f infra/compose/docker-compose.yml up -d

# Follow the logs
docker compose -f infra/compose/docker-compose.yml logs -f
```

> Compose mounts `.runtime/tmp` by default. Setting the cache timeout to `0` means files do not expire automatically. Keep this mount if cached files must survive container recreation.

#### WARP mode (with a proxy)

```bash
# Start with the WARP proxy
docker compose -f infra/compose/docker-compose.proxy.yml up -d

# Follow the logs
docker compose -f infra/compose/docker-compose.proxy.yml logs -f
```

#### Headed CAPTCHA mode in Docker (`browser` / `personal`)

> Use this mode when you need a virtual desktop and headed browser-based CAPTCHA solving inside the container.
> It starts `Xvfb + Fluxbox` for an internal visual desktop and sets `ALLOW_DOCKER_HEADED_CAPTCHA=true`.
> Only the application port is exposed; no remote desktop port is provided.
> The built-in `personal` browser now starts headed by default. Set `PERSONAL_BROWSER_HEADLESS=true` to temporarily switch it back to headless mode.

```bash
# Start headed mode (use --build on the first run)
docker compose -f infra/compose/docker-compose.headed.yml up -d --build

# Follow the logs
docker compose -f infra/compose/docker-compose.headed.yml logs -f
```

- API port: `8000`
- In the admin interface, set the CAPTCHA method to `browser` or `personal`

#### Cloudflare Tunnel (public API and separate admin hostnames)

Run [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) in Docker and expose the OpenAI-compatible API (`/v1/...`) and web administration interface (`/` and `/api/...`) through two public subdomains backed by the same internal service. Both hostnames proxy to `http://<service-name>:8000` inside Docker; the application remains a single process.

1. In [Cloudflare Zero Trust](https://one.dash.cloudflare.com/), open **Networks** → **Tunnels**, create a named tunnel, and copy the **TUNNEL_TOKEN** from the `cloudflared` installation command.
2. Configure two **Public hostnames** on the same tunnel (replace these examples with your own domains):
   - **API only** (no admin UI or frontend): `https://api.example.com` → `http://sub2gen:8000`
   - **Admin UI and frontend**: `https://admin.example.com` → `http://sub2gen:8000`
   Docker resolves `sub2gen` to the application container on the shared network. Do not use a host-mapped port such as `38000` as the tunnel origin.
3. Run `cp .env.example .env` in the repository root, set `TUNNEL_TOKEN=...`, and set `SUB2GEN_API_ONLY_HOST` to the API hostname. On that hostname, `ApiOnlyHostMiddleware` exposes the generation API, cache files, `/openapi.json`, `/health`, and the protocol-v1 worker socket. It blocks the web UI and administration routes.
4. Start the stack with `docker compose -f infra/compose/docker-compose.yml up -d`. To build the main image from source, run `docker build -t sub2gen:local -f infra/docker/Dockerfile .`, set the service image to `sub2gen:local`, and run `up`.
5. Open the admin hostname for administration and use the API hostname as the OpenAI-compatible API base URL, for example `https://api.example.com/v1/...`. Pair remote browser workers through **Platform → Workers**; every worker connects directly to `/worker_ws`.
6. Set `[cache].base_url` in `config/setting.toml` to the public API URL, for example `base_url = "https://api.example.com"`. See the comments in `config/setting_example.toml`.
7. Configure `SUB2GEN_API_ONLY_HOST` as an environment variable. It is empty by default for local use; Docker Compose reads the root `.env` file.

**If `/login` or another UI page remains accessible on the API-only hostname:** build and deploy the current source with `docker build -t sub2gen:local -f infra/docker/Dockerfile .`, set the Compose service image to `sub2gen:local`, and run `up -d` again. Confirm that the startup log contains `API-only host(s)`. The environment variable can also be set when running `uv run sub2gen` directly. If the current image is deployed but the old page remains, disable aggressive HTML caching for that hostname or purge the Cloudflare cache.

For headed CAPTCHA solving, use `infra/compose/docker-compose.headed.yml`, which already includes Cloudflare Tunnel and `sub2gen-headed`:

```bash
docker compose -f infra/compose/docker-compose.headed.yml up -d
```

In Zero Trust, set both public hostnames' origin to `http://sub2gen-headed:8000`, matching the service name in `infra/compose/docker-compose.headed.yml`.

### Option 2: Local deployment

```bash
# Clone the project
git clone https://github.com/agmmnn/sub2gen.git
cd sub2gen

# Create/sync the environment and build the frontend
uv run setup

# Start sub2gen
uv run sub2gen
```

`uv run setup` installs Python 3.11 if needed, creates `.venv`, installs the exact versions from `uv.lock`, installs the locked workspace dependencies with Bun, and builds the admin UI into `apps/api/static/`. Run it after cloning or when frontend dependencies change.

`uv run sub2gen` starts the backend without rebuilding the frontend. For development, update Python dependencies with `uv add`/`uv remove`, then commit both `pyproject.toml` and `uv.lock`.

For upgrades, database adoption, compatibility guarantees, verification evidence,
and rollback boundaries, see [`docs/architecture-migration-release.md`](./docs/architecture-migration-release.md).

The proposed roadmap for evolving sub2gen into a unified, local-first generation
gateway is documented in
[`docs/unified-generation-platform-plan.md`](./docs/unified-generation-platform-plan.md).
Its Phase 0 baselines are the
[`current architecture inventory`](./docs/current-architecture-baseline.md),
[`provider source provenance`](./docs/provider-source-provenance.md),
and [`worker threat model`](./docs/worker-threat-model.md).
The completed private ChatGPT browser spike and measured behavior are recorded in
[`docs/phase1-chatgpt-web-spike.md`](./docs/phase1-chatgpt-web-spike.md).

### First visit

After startup, open the administration interface at **http://localhost:8000**. Change the default password immediately after your first login.

- **Username**: `admin`
- **Password**: `admin`

## 📈 Monitoring endpoints

- `GET /health`: Public health check with service status and summaries for active, expiring, expired, and rate-limited tokens
- `GET /metrics`: Prometheus metrics
- `GET /api/tokens`: Admin endpoint with token state such as `at_expires`, `at_expired`, `at_expiring_within_1h`, `ban_reason`, and `consecutive_error_count`

Prometheus can scrape `/metrics` directly. For Kubernetes deployments, scrape it only inside the cluster and restrict external access at the Ingress or Gateway layer.

### Model test page

Open **http://localhost:8000/test** to use the built-in model test page. It supports:

- Browsing available models by category, including image generation, text/image-to-video, reference-image video, and video upscaling
- One-click prompt testing with streamed generation progress
- Image uploads for image-to-image and image-to-video requests
- Direct image or video previews after generation

## 📋 Supported models

### Image generation

| Model | Description | Orientation |
|---------|--------|--------|
| `gemini-2.5-flash-image-landscape` | Text/image-to-image | Landscape |
| `gemini-2.5-flash-image-portrait` | Text/image-to-image | Portrait |
| `gemini-3.0-pro-image-landscape` | Text/image-to-image | Landscape |
| `gemini-3.0-pro-image-portrait` | Text/image-to-image | Portrait |
| `gemini-3.0-pro-image-square` | Text/image-to-image | Square |
| `gemini-3.0-pro-image-four-three` | Text/image-to-image | Landscape 4:3 |
| `gemini-3.0-pro-image-three-four` | Text/image-to-image | Portrait 3:4 |
| `gemini-3.0-pro-image-landscape-2k` | Text/image-to-image (2K) | Landscape |
| `gemini-3.0-pro-image-portrait-2k` | Text/image-to-image (2K) | Portrait |
| `gemini-3.0-pro-image-square-2k` | Text/image-to-image (2K) | Square |
| `gemini-3.0-pro-image-four-three-2k` | Text/image-to-image (2K) | Landscape 4:3 |
| `gemini-3.0-pro-image-three-four-2k` | Text/image-to-image (2K) | Portrait 3:4 |
| `gemini-3.0-pro-image-landscape-4k` | Text/image-to-image (4K) | Landscape |
| `gemini-3.0-pro-image-portrait-4k` | Text/image-to-image (4K) | Portrait |
| `gemini-3.0-pro-image-square-4k` | Text/image-to-image (4K) | Square |
| `gemini-3.0-pro-image-four-three-4k` | Text/image-to-image (4K) | Landscape 4:3 |
| `gemini-3.0-pro-image-three-four-4k` | Text/image-to-image (4K) | Portrait 3:4 |
| `imagen-4.0-generate-preview-landscape` | Text/image-to-image | Landscape |
| `imagen-4.0-generate-preview-portrait` | Text/image-to-image | Portrait |
| `gemini-3.1-flash-image-landscape` | Text/image-to-image | Landscape |
| `gemini-3.1-flash-image-portrait` | Text/image-to-image | Portrait |
| `gemini-3.1-flash-image-square` | Text/image-to-image | Square |
| `gemini-3.1-flash-image-four-three` | Text/image-to-image | Landscape 4:3 |
| `gemini-3.1-flash-image-three-four` | Text/image-to-image | Portrait 3:4 |
| `gemini-3.1-flash-image-landscape-2k` | Text/image-to-image (2K) | Landscape |
| `gemini-3.1-flash-image-portrait-2k` | Text/image-to-image (2K) | Portrait |
| `gemini-3.1-flash-image-square-2k` | Text/image-to-image (2K) | Square |
| `gemini-3.1-flash-image-four-three-2k` | Text/image-to-image (2K) | Landscape 4:3 |
| `gemini-3.1-flash-image-three-four-2k` | Text/image-to-image (2K) | Portrait 3:4 |
| `gemini-3.1-flash-image-landscape-4k` | Text/image-to-image (4K) | Landscape |
| `gemini-3.1-flash-image-portrait-4k` | Text/image-to-image (4K) | Portrait |
| `gemini-3.1-flash-image-square-4k` | Text/image-to-image (4K) | Square |
| `gemini-3.1-flash-image-four-three-4k` | Text/image-to-image (4K) | Landscape 4:3 |
| `gemini-3.1-flash-image-three-four-4k` | Text/image-to-image (4K) | Portrait 3:4 |

### Video generation

#### Text-to-video (T2V)
⚠️ **Image uploads are not supported**

| Model | Description | Orientation |
|---------|---------|--------|
| `veo_3_1_t2v_fast_portrait` | Text-to-video | Portrait |
| `veo_3_1_t2v_fast_landscape` | Text-to-video | Landscape |
| `veo_3_1_t2v_fast_portrait_ultra` | Text-to-video | Portrait |
| `veo_3_1_t2v_fast_ultra` | Text-to-video | Landscape |
| `veo_3_1_t2v_fast_portrait_ultra_relaxed` | Text-to-video | Portrait |
| `veo_3_1_t2v_fast_ultra_relaxed` | Text-to-video | Landscape |
| `veo_3_1_t2v_portrait` | Text-to-video | Portrait |
| `veo_3_1_t2v_landscape` | Text-to-video | Landscape |
| `veo_3_1_t2v_lite_portrait` | Text-to-video Lite | Portrait |
| `veo_3_1_t2v_lite_landscape` | Text-to-video Lite | Landscape |
| `veo_3_1_t2v_landscape_4s` | Text-to-video, 4 seconds | Landscape |
| `veo_3_1_t2v_portrait_4s` | Text-to-video, 4 seconds | Portrait |
| `veo_3_1_t2v_landscape_6s` | Text-to-video, 6 seconds | Landscape |
| `veo_3_1_t2v_portrait_6s` | Text-to-video, 6 seconds | Portrait |
| `veo_3_1_t2v_fast_landscape_4s` | Fast text-to-video, 4 seconds | Landscape |
| `veo_3_1_t2v_fast_portrait_4s` | Fast text-to-video, 4 seconds | Portrait |
| `veo_3_1_t2v_fast_landscape_6s` | Fast text-to-video, 6 seconds | Landscape |
| `veo_3_1_t2v_fast_portrait_6s` | Fast text-to-video, 6 seconds | Portrait |
| `veo_3_1_t2v_lite_4s_portrait` | Text-to-video Lite, 4 seconds | Portrait |
| `veo_3_1_t2v_lite_4s_landscape` | Text-to-video Lite, 4 seconds | Landscape |
| `veo_3_1_t2v_lite_6s_portrait` | Text-to-video Lite, 6 seconds | Portrait |
| `veo_3_1_t2v_lite_6s_landscape` | Text-to-video Lite, 6 seconds | Landscape |

#### First/last-frame models (I2V - Image to Video)
📸 **Supports one or two images: one image is the first frame; two images are the first and last frames**

> 💡 **Automatic selection:** the system chooses the appropriate `model_key` from the image count.
> - **Single-frame mode** (one image): generates a video from the first frame
> - **Two-frame mode** (two images): generates a transition between the first and last frames
> - `veo_3_1_i2v_lite_*` supports only **one** first-frame image
> - `veo_3_1_interpolation_lite_*` supports exactly **two** first/last-frame images

| Model | Description | Orientation |
|---------|---------|--------|
| `veo_3_1_i2v_s_fast_portrait_fl` | Image-to-video | Portrait |
| `veo_3_1_i2v_s_fast_fl` | Image-to-video | Landscape |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl` | Image-to-video | Portrait |
| `veo_3_1_i2v_s_fast_ultra_fl` | Image-to-video | Landscape |
| `veo_3_1_i2v_s_fast_portrait_ultra_relaxed` | Image-to-video | Portrait |
| `veo_3_1_i2v_s_fast_ultra_relaxed` | Image-to-video | Landscape |
| `veo_3_1_i2v_s_portrait` | Image-to-video | Portrait |
| `veo_3_1_i2v_s_landscape` | Image-to-video | Landscape |
| `veo_3_1_i2v_lite_portrait` | Image-to-video Lite (first frame only) | Portrait |
| `veo_3_1_i2v_lite_landscape` | Image-to-video Lite (first frame only) | Landscape |
| `veo_3_1_interpolation_lite_portrait` | Image-to-video Lite (first/last-frame transition) | Portrait |
| `veo_3_1_interpolation_lite_landscape` | Image-to-video Lite (first/last-frame transition) | Landscape |
| `veo_3_1_i2v_s_landscape_4s` | Image-to-video, 4 seconds | Landscape |
| `veo_3_1_i2v_s_portrait_4s` | Image-to-video, 4 seconds | Portrait |
| `veo_3_1_i2v_s_landscape_6s` | Image-to-video, 6 seconds | Landscape |
| `veo_3_1_i2v_s_portrait_6s` | Image-to-video, 6 seconds | Portrait |
| `veo_3_1_i2v_s_fast_landscape_4s_fl` | Fast image-to-video, 4 seconds | Landscape |
| `veo_3_1_i2v_s_fast_portrait_4s_fl` | Fast image-to-video, 4 seconds | Portrait |
| `veo_3_1_i2v_s_fast_landscape_6s_fl` | Fast image-to-video, 6 seconds | Landscape |
| `veo_3_1_i2v_s_fast_portrait_6s_fl` | Fast image-to-video, 6 seconds | Portrait |
| `veo_3_1_i2v_lite_4s_portrait` | Image-to-video Lite, 4 seconds (first frame only) | Portrait |
| `veo_3_1_i2v_lite_4s_landscape` | Image-to-video Lite, 4 seconds (first frame only) | Landscape |
| `veo_3_1_i2v_lite_6s_portrait` | Image-to-video Lite, 6 seconds (first frame only) | Portrait |
| `veo_3_1_i2v_lite_6s_landscape` | Image-to-video Lite, 6 seconds (first frame only) | Landscape |
| `veo_3_1_interpolation_lite_4s_portrait` | Image-to-video Lite, 4 seconds (first/last-frame transition) | Portrait |
| `veo_3_1_interpolation_lite_4s_landscape` | Image-to-video Lite, 4 seconds (first/last-frame transition) | Landscape |
| `veo_3_1_interpolation_lite_6s_portrait` | Image-to-video Lite, 6 seconds (first/last-frame transition) | Portrait |
| `veo_3_1_interpolation_lite_6s_landscape` | Image-to-video Lite, 6 seconds (first/last-frame transition) | Landscape |

#### Reference images to video (R2V)
🖼️ **Supports multiple reference images**

> **2026-03-06 update**
>
> - Synchronized with the latest upstream `R2V` video request body
> - Replaced `textInput` with `structuredPrompt.parts`
> - Added top-level `mediaGenerationContext.batchId`
> - Added top-level `useV2ModelConfig: true`
> - Landscape and portrait `R2V` models now share the same request body
> - The upstream landscape `videoModelKey` now uses the `*_landscape` form
> - The current upstream protocol accepts at most **three** `referenceImages`

| Model | Description | Orientation |
|---------|---------|--------|
| `veo_3_1_r2v_fast_portrait` | Reference-image-to-video | Portrait |
| `veo_3_1_r2v_fast_landscape` | Reference-image-to-video | Landscape |
| `veo_3_1_r2v_fast_portrait_ultra` | Reference-image-to-video | Portrait |
| `veo_3_1_r2v_fast_landscape_ultra` | Reference-image-to-video | Landscape |
| `veo_3_1_r2v_fast_portrait_ultra_relaxed` | Reference-image-to-video | Portrait |
| `veo_3_1_r2v_fast_landscape_ultra_relaxed` | Reference-image-to-video | Landscape |

#### Video upscaling models

These models first generate a video with the corresponding standard Veo 3.1 model and then submit a 1080p or 4K upscale request. They do not call an upstream upscaler model key directly.

| Model | Description | Output |
|---------|---------|--------|
| `veo_3_1_t2v_landscape_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_portrait_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_landscape_1080p` | Text-to-video upscale | 1080p |
| `veo_3_1_t2v_portrait_1080p` | Text-to-video upscale | 1080p |
| `veo_3_1_t2v_landscape_4s_4k` | 4-second text-to-video upscale | 4K |
| `veo_3_1_t2v_portrait_4s_4k` | 4-second text-to-video upscale | 4K |
| `veo_3_1_t2v_landscape_4s_1080p` | 4-second text-to-video upscale | 1080p |
| `veo_3_1_t2v_portrait_4s_1080p` | 4-second text-to-video upscale | 1080p |
| `veo_3_1_t2v_landscape_6s_4k` | 6-second text-to-video upscale | 4K |
| `veo_3_1_t2v_portrait_6s_4k` | 6-second text-to-video upscale | 4K |
| `veo_3_1_t2v_landscape_6s_1080p` | 6-second text-to-video upscale | 1080p |
| `veo_3_1_t2v_portrait_6s_1080p` | 6-second text-to-video upscale | 1080p |
| `veo_3_1_t2v_fast_portrait_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_fast_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_fast_portrait_ultra_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_fast_ultra_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_fast_portrait_1080p` | Text-to-video upscale | 1080p |
| `veo_3_1_t2v_fast_1080p` | Text-to-video upscale | 1080p |
| `veo_3_1_t2v_fast_portrait_ultra_1080p` | Text-to-video upscale | 1080p |
| `veo_3_1_t2v_fast_ultra_1080p` | Text-to-video upscale | 1080p |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_4k` | Image-to-video upscale | 4K |
| `veo_3_1_i2v_s_fast_ultra_fl_4k` | Image-to-video upscale | 4K |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p` | Image-to-video upscale | 1080p |
| `veo_3_1_i2v_s_fast_ultra_fl_1080p` | Image-to-video upscale | 1080p |
| `veo_3_1_i2v_s_landscape_4k` | Image-to-video upscale | 4K |
| `veo_3_1_i2v_s_portrait_4k` | Image-to-video upscale | 4K |
| `veo_3_1_i2v_s_landscape_1080p` | Image-to-video upscale | 1080p |
| `veo_3_1_i2v_s_portrait_1080p` | Image-to-video upscale | 1080p |
| `veo_3_1_i2v_s_landscape_4s_4k` | 4-second image-to-video upscale | 4K |
| `veo_3_1_i2v_s_portrait_4s_4k` | 4-second image-to-video upscale | 4K |
| `veo_3_1_i2v_s_landscape_4s_1080p` | 4-second image-to-video upscale | 1080p |
| `veo_3_1_i2v_s_portrait_4s_1080p` | 4-second image-to-video upscale | 1080p |
| `veo_3_1_i2v_s_landscape_6s_4k` | 6-second image-to-video upscale | 4K |
| `veo_3_1_i2v_s_portrait_6s_4k` | 6-second image-to-video upscale | 4K |
| `veo_3_1_i2v_s_landscape_6s_1080p` | 6-second image-to-video upscale | 1080p |
| `veo_3_1_i2v_s_portrait_6s_1080p` | 6-second image-to-video upscale | 1080p |
| `veo_3_1_r2v_fast_portrait_ultra_4k` | Reference-image video upscale | 4K |
| `veo_3_1_r2v_fast_landscape_ultra_4k` | Reference-image video upscale | 4K |
| `veo_3_1_r2v_fast_portrait_ultra_1080p` | Reference-image video upscale | 1080p |
| `veo_3_1_r2v_fast_landscape_ultra_1080p` | Reference-image video upscale | 1080p |

## 📡 API examples (streaming required)

> In addition to the OpenAI-compatible examples below, the service supports the official Gemini format:
> - `POST /v1beta/models/{model}:generateContent`
> - `POST /models/{model}:generateContent`
> - `POST /v1beta/models/{model}:streamGenerateContent`
> - `POST /models/{model}:streamGenerateContent`
>
> Official Gemini requests support these authentication methods:
> - `Authorization: Bearer <api_key>`
> - `x-goog-api-key: <api_key>`
> - `?key=<api_key>`
>
> Official Gemini image requests support:
> - `systemInstruction`
> - `contents[].parts[].text`
> - `contents[].parts[].inlineData`
> - `contents[].parts[].fileData.fileUri`
> - `generationConfig.responseModalities`
> - `generationConfig.imageConfig.aspectRatio`
> - `generationConfig.imageConfig.imageSize`

### Official Gemini `generateContent` (text-to-image)

> Verified with a real token. For streaming output, replace the path suffix with `:streamGenerateContent?alt=sse`.

```bash
curl -X POST "http://localhost:8000/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "systemInstruction": {
      "parts": [
        {
          "text": "Return an image only."
        }
      ]
    },
    "contents": [
      {
        "role": "user",
        "parts": [
          {
            "text": "A red apple on a wooden table, studio lighting, minimalist background"
          }
        ]
      }
    ],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {
        "aspectRatio": "1:1",
        "imageSize": "1K"
      }
    }
  }'
```

### Text-to-image

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": "A cute cat playing in a garden"
      }
    ],
    "stream": true
  }'
```

### Optional Flow project pinning

Native Flow image and video requests use automatic project rotation by default. To keep generated assets in a specific tracked project, give the managed API key the `projects:read` scope, list its available projects, and pass `project_id` with the generation request:

```bash
curl "http://localhost:8000/v1/projects?limit=100" \
  -H "Authorization: Bearer <managed-api-key>"

curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer <managed-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "project_id": "<flow-project-id>",
    "messages": [{"role": "user", "content": "A cute cat playing in a garden"}],
    "stream": true
  }'
```

The project must be active, belong to that API key, and use an account assigned to the key. Omit `project_id` to retain automatic routing. Project pinning applies only to native Flow models, not Runway or GeminiGen providers.

### Image-to-image

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Transform this image into a watercolor painting"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<base64_encoded_image>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### Text-to-video

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_t2v_fast_landscape",
    "messages": [
      {
        "role": "user",
        "content": "A kitten chasing butterflies across a meadow"
      }
    ],
    "stream": true
  }'
```

### First-and-last-frame video

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_i2v_s_fast_fl_landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Transition smoothly from the first image to the second image"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<first_frame_base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<last_frame_base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### Reference-images-to-video

> The server assembles the current `R2V` request body automatically; callers continue to use OpenAI-compatible input.
> Landscape `R2V` requests are mapped to the latest upstream `*_landscape` model key.
> A request can currently include up to **three reference images**.

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_r2v_fast_portrait",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Create a portrait video with a smooth camera push based on the characters and setting in these three reference images"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<reference_image_1_base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<reference_image_2_base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<reference_image_3_base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

---

## 📄 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgements

- [PearNoDec](https://github.com/PearNoDec) for the YesCaptcha integration
- [raomaiping](https://github.com/raomaiping) for the headless CAPTCHA solution

Thanks to every contributor and user for their support.

---

## 📞 Contact

- Report an issue: [GitHub Issues](https://github.com/agmmnn/sub2gen/issues)

---

**⭐ If this project helps you, please give it a star!**

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=agmmnn/sub2gen&type=date&legend=top-left)](https://www.star-history.com/#agmmnn/sub2gen&type=date&legend=top-left)
