# Phase 1 ChatGPT Web Spike

- Status: complete
- Run date: 2026-08-09
- Scope: private CLI harness; no FastAPI route or persistent credential model

## What was implemented

The spike lives in `sub2gen.spikes.chatgpt_web` and is available as:

```bash
uv run sub2gen-chatgpt-spike doctor

uv run sub2gen-chatgpt-spike generate \
  "A product photo of a ceramic mug" \
  --out /tmp/mug.png \
  --project sub2gen \
  --profile relay
```

The harness:

- invokes `chatgpt-imagegen` asynchronously with `--backend web` and no fallback;
- selects an explicit ChatGPT project and leaves conversation cleanup enabled;
- copies local references into a per-run temporary directory;
- validates the output with Pillow before atomically publishing it;
- reports byte count, dimensions, MIME type, SHA-256, latency, project-selection
  evidence, and conversation-cleanup evidence;
- classifies authentication, quota, refusal, browser-unavailable, timeout,
  invalid-output, and unknown failures without returning prompts or local paths;
- enforces one in-process web job and the upstream one-slot cross-process lock; and
- terminates the subprocess group, closes the browser session best-effort, and removes
  temporary files on timeout or caller cancellation.

`SUB2GEN_CHATGPT_IMAGEGEN_CLI` and `SUB2GEN_CHROME_USE_CLI` may override executable
discovery. The CLI also accepts `--cli` and `--chrome-use` explicitly.

## Live measurements

The local extension relay and `chrome-use 1.5.87` were connected. Both live runs used
the pinned `chatgpt-imagegen 0.21.2` checkout and the `sub2gen` project.

| Path | Result | Latency | Output | Project selected | Conversation deleted |
| --- | --- | ---: | --- | --- | --- |
| Text-to-image | success | 43.67 s | PNG, 1402×1122, 1,573,539 bytes | yes | yes |
| Image-to-image using the first result as a temporary reference | success | 69.31 s | PNG, 1402×1122, 1,748,772 bytes | yes | yes |

Both images were decoded and visually inspected. Generated smoke-test files remained in
an external temporary directory and were not added to the repository.

## Characterized behavior

- Effective web concurrency is one. The harness async lock is covered by overlapping
  invocation tests, and the child receives `CHATGPT_IMAGEGEN_WEB_CONCURRENCY=1` for the
  upstream cross-process lock.
- A strict wrapper timeout kills the entire POSIX subprocess group. A descendant test
  proves that a spawned child cannot outlive the timed-out or cancelled job.
- Temporary reference and output files disappear after success, failure, timeout, and
  cancellation. Destination files appear only after image validation.
- Project and conversation-cleanup evidence currently comes from upstream diagnostic
  text. This is sufficient for the throwaway spike but must become typed provider events
  when Phase 2 extracts the provider SDK.

## Known engineering constraints for the next phase

- Browser selectors and internal ChatGPT page behavior can change independently of this
  repository.
- The current CLI returns one terminal image and does not expose typed progress events.
- The Phase 1 harness accepts local reference files only; URL fetching belongs behind
  the provider SDK's typed input and artifact boundaries.
- The shared browser surface remains single-concurrency. Worker routing must treat this
  as provider/account capacity rather than queueing unlimited local subprocesses.
