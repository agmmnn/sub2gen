from __future__ import annotations

import asyncio
from dataclasses import fields

import pytest

from sub2gen_provider_sdk import (
    Artifact,
    CancellationToken,
    GenerationRequest,
    ProviderError,
    ProviderErrorCode,
    ProviderExecutionContext,
    ReferenceInput,
    ResolvedExecution,
    await_with_execution_context,
)


def _context(timeout: float = 1.0) -> ProviderExecutionContext:
    return ProviderExecutionContext(
        resolved=ResolvedExecution(
            requested_model="model",
            resolved_model="model",
            provider_id="fake",
            billing_pool="subscription",
        ),
        cancellation=CancellationToken(),
        timeout_seconds=timeout,
    )


def test_generation_request_is_provider_neutral_and_supports_repeated_references() -> None:
    request_fields = {item.name for item in fields(GenerationRequest)}
    assert request_fields == {
        "request_id",
        "prompt",
        "model",
        "kind",
        "references",
        "count",
        "provider_options",
    }
    assert not request_fields.intersection({"token", "cookie", "project_id", "flow_id", "credential"})

    request = GenerationRequest(
        request_id="request-1",
        prompt="draw",
        model="image-model",
        references=(
            ReferenceInput(media_type="image/png", data=b"first"),
            ReferenceInput(media_type="image/jpeg", data=b"second"),
        ),
    )
    assert [reference.read_bytes() for reference in request.references] == [b"first", b"second"]

    with pytest.raises(ValueError, match="must not contain credentials"):
        GenerationRequest(
            request_id="request-2",
            prompt="draw",
            model="image-model",
            provider_options={"session_token": "must-not-enter-the-request"},
        )


def test_artifact_requires_exactly_one_storage_representation(tmp_path) -> None:
    local = tmp_path / "result.png"
    local.write_bytes(b"png")

    assert Artifact(media_type="image/png", data=b"png").read_bytes() == b"png"
    assert Artifact(media_type="image/png", local_path=local).read_bytes() == b"png"
    with pytest.raises(ValueError, match="exactly one"):
        Artifact(media_type="image/png")
    with pytest.raises(ValueError, match="exactly one"):
        Artifact(media_type="image/png", data=b"png", local_path=local)


@pytest.mark.asyncio
async def test_execution_helper_propagates_cancellation() -> None:
    context = _context()

    async def operation() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(await_with_execution_context(operation(), context))
    await asyncio.sleep(0)
    context.cancellation.cancel()

    with pytest.raises(ProviderError) as caught:
        await task
    assert caught.value.code is ProviderErrorCode.CANCELLED


@pytest.mark.asyncio
async def test_execution_helper_enforces_timeout() -> None:
    context = _context(timeout=0.01)

    async def operation() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ProviderError) as caught:
        await await_with_execution_context(operation(), context)
    assert caught.value.code is ProviderErrorCode.TIMEOUT
    assert caught.value.retryable is True
