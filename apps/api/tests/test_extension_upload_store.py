from __future__ import annotations

import json

import pytest

from sub2gen.workers.extension.uploads import GenerationUploadStore


@pytest.mark.asyncio
async def test_generation_upload_store_authenticates_and_resolves_json() -> None:
    store = GenerationUploadStore()
    upload_id, secret = await store.register(
        req_id="request-1", max_body_bytes=1024, ttl_seconds=60
    )

    assert await store.ingest(upload_id, "wrong", b"{}") == (
        False,
        "invalid_upload_secret",
    )
    payload = json.dumps({"operation": "IMAGE_GENERATION", "ok": True}).encode()
    assert await store.ingest(upload_id, secret, payload) == (True, "")
    assert await store.ingest(upload_id, secret, payload) == (False, "duplicate_upload")

    resolved = await store.resolve(
        req_id="request-1",
        upload_id=upload_id,
        base_payload={"status": 200},
    )
    assert resolved == {
        "status": 200,
        "response_text": payload.decode(),
        "response_json": {"operation": "IMAGE_GENERATION", "ok": True},
        "upload_status": "uploaded",
    }


@pytest.mark.asyncio
async def test_generation_upload_store_enforces_body_limit() -> None:
    store = GenerationUploadStore()
    upload_id, secret = await store.register(
        req_id="request-2", max_body_bytes=3, ttl_seconds=60
    )

    assert await store.ingest(upload_id, secret, b"four") == (False, "body_too_large")
