# Asynchronous generation and polling

sub2gen offers an asynchronous form of the OpenAI-compatible generation route
for callers that cannot keep a streaming request open.

Submit the same JSON body accepted by `/v1/chat/completions`:

```http
POST /v1/async/chat/completions
Authorization: Bearer <managed-api-key>
Content-Type: application/json
```

A successful submission returns HTTP 202 with `job_id` and `status`. Native Flow
responses also include the selected `project_id`. Poll with the same managed key:

```http
GET /v1/jobs/<job_id>
Authorization: Bearer <managed-api-key>
```

The job endpoint enforces API-key ownership. It returns persisted native Flow
status, and delegates provider-specific polling for Runway jobs. GeminiGen jobs
are completed by their background task and read from persisted state. Poll at a
moderate interval (for example 3–10 seconds) and stop on a terminal success or
error state.

Legacy global keys cannot submit asynchronous generation; use a managed API key
with the appropriate account assignment and provider scope. `project_id` is
supported only for native Flow models.
