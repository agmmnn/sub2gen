# Runway Integration

sub2gen can route `runway-*` models to Runway web tasks while keeping Flow/Veo models on the existing Flow pipeline. The integration uses the Runway web-account task flow captured from the app, not the official API-secret flow.

## Admin Setup

1. Open the admin panel and go to `Runway`.
2. Enable Runway and keep the base URL as `https://api.runwayml.com/v1`.
3. Add a Runway account by pasting a working Runway JWT or full cookie string.
4. Confirm the decoded workspace/team values appear.
5. Use the health button to validate `GET /v1/profile/features`.
6. Use `Sync presets` to refresh the local Runway model manifest and live feature availability.
7. Enable only the `runway-*` models you want to expose to managed API keys.

The account flow sends:

- `Authorization: Bearer <jwt>`
- `Origin: https://app.runwayml.com`
- `Referer: https://app.runwayml.com/`
- `x-runway-workspace: <workspace_id>`

Rotate any Runway JWT that was previously committed, pasted into logs, or shared.

## Model Registry

The backend ships a versioned Runway manifest with typed builders for image, video, audio, and upscale flows. Each model row includes:

- public model id, display name, kind, upstream `taskType`, and builder key
- supported modes, media roles, option schema, limits, feature flags, and cost feature
- admin enabled toggle plus live availability/disabled reason from the active Runway account

Common enabled models include:

- `runway-nano-banana-2`
- `runway-nano-banana-pro`
- `runway-gen4-image`
- `runway-gen45-video`
- `runway-kling-3-pro`
- `runway-seedance-2`
- `runway-veo-3-1`
- `runway-veo-3`
- `runway-text-to-speech`
- `runway-speech-to-speech`
- `runway-sound-effects`
- `runway-image-upscale`
- `runway-video-upscale`
- `runway-talking-avatar`

Visible models without a confirmed typed builder are kept in the registry but marked unavailable.

## Native API

All native Runway routes require a managed API key with the `runway:generate` scope.

List models:

```bash
curl http://localhost:8000/v1/runway/models \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY"
```

List TTS voices:

```bash
curl http://localhost:8000/v1/runway/voices \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY"
```

Upload media to Runway and mirror it into sub2gen cache:

```bash
curl -X POST http://localhost:8000/v1/runway/uploads \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY" \
  -F "file=@reference.png" \
  -F "role=first_frame"
```

The upload response includes `asset_id`, `asset_url`, `upload_id`, `dataset`, `cached_url`, and a `data_url`. For exact Runway reference payloads, prefer passing `asset_id` plus `asset_url` back as task media.

Create an image task:

```bash
curl -X POST http://localhost:8000/v1/runway/tasks \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "runway-nano-banana-2",
    "prompt": "A mossy log on a clean white background",
    "aspect_ratio": "21:9",
    "image_size": "4K",
    "num_outputs": 1
  }'
```

Create an image-to-video task with a first frame:

```bash
curl -X POST http://localhost:8000/v1/runway/tasks \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "runway-gen45-video",
    "prompt": "Slow cinematic camera move",
    "aspect_ratio": "16:9",
    "duration": 5,
    "media": [
      {
        "role": "first_frame",
        "asset_id": "runway-asset-id",
        "url": "https://cdn.runwayml.com/reference.png"
      }
    ]
  }'
```

Create TTS audio:

```bash
curl -X POST http://localhost:8000/v1/runway/tasks \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "runway-text-to-speech",
    "prompt": "Welcome to the product launch.",
    "voice_id": "runway-voice-id"
  }'
```

Estimate credits:

```bash
curl -X POST http://localhost:8000/v1/runway/estimate \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"runway-gen45-video","prompt":"A wide desert shot","duration":5}'
```

Poll or cancel:

```bash
curl http://localhost:8000/v1/runway/tasks/runway-20260530-120000-abcd1234 \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY"

curl -X POST http://localhost:8000/v1/runway/tasks/runway-20260530-120000-abcd1234/cancel \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY"
```

## Media Roles

Use `media[].role` to tell sub2gen how to place uploaded assets in the Runway payload:

- `reference_image`
- `first_frame`
- `last_frame`
- `reference_video`
- `reference_audio`
- `character_image`
- `input_audio`
- `image_to_upscale`
- `video_to_upscale`

Advanced fields can be passed through `options`; model-specific structured fields include `mode`, `aspect_ratio`, `orientation`, `duration`, `resolution`, `image_size`, `num_outputs`, `seed`, `sound`, `fps`, `voice_id`, `multi_shot`, and `upscale`.

## OpenAI-Compatible API

Runway models appear in `/v1/models` only when Runway is globally enabled and the model is admin-enabled/live-available. OpenAI-compatible generation also requires `runway:generate`.

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "runway-gen45-video",
    "messages": [
      {"role": "user", "content": "A cinematic macro shot of moss on bark"}
    ],
    "stream": true,
    "duration": 5,
    "aspect_ratio": "16:9",
    "sound": false
  }'
```

Async OpenAI-compatible calls are also supported:

```bash
curl -X POST http://localhost:8000/v1/async/chat/completions \
  -H "Authorization: Bearer $SUB2GEN_MANAGED_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "runway-nano-banana-2",
    "messages": [{"role": "user", "content": "A product photo of a wooden log slice"}]
  }'
```

Poll with `/v1/jobs/{job_id}`.

## Outputs

Runway task responses include:

- `raw_artifact_urls`: upstream Runway artifact links
- `cached_artifact_urls`: sub2gen `/api/cache/blob/...` links when cache output is enabled
- `result_urls`: cached URLs first, raw URLs as fallback

sub2gen keeps the raw Runway URLs in task state even when caching is enabled.
