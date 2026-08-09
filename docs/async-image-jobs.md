# Asynchronous image jobs

`POST /v1/images/generations` accepts `"async": true` or `Prefer: respond-async`.
The returned `job_id` is polled through `GET /v1/jobs/{job_id}` and may be cancelled
through `POST /v1/jobs/{job_id}/cancel` while it is active in the current API process.

Job state, resolved execution identity, terminal errors, and committed artifact metadata
are durable. ChatGPT browser jobs are intentionally non-resumable: if the API process
restarts while one is queued or running, startup reconciliation records a terminal
`process_restart` failure. Completed artifacts remain readable after restart. A retry
therefore creates a new job or uses a new idempotency key.
