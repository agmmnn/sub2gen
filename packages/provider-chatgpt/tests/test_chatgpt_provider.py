from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from sub2gen_provider_chatgpt import ChatGPTImagegenProcessBackend, ChatGPTWebProvider
from sub2gen_provider_chatgpt.browser import ProcessHealth
from sub2gen_provider_sdk import (
    Artifact,
    CancellationToken,
    GenerationRequest,
    ProviderError,
    ProviderErrorCode,
    ProviderExecutionContext,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderResult,
    ReferenceInput,
    ResolvedExecution,
)
from sub2gen_provider_sdk.testing import exercise_provider


class _ChatGPTBackend:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.active = 0
        self.maximum_active = 0
        self.cancelled: list[str] = []

    async def health(self) -> ProviderHealth:
        return ProviderHealth("chatgpt-web", ProviderHealthStatus.READY, "relay connected")

    async def generate(
        self,
        request: GenerationRequest,
        context: ProviderExecutionContext,
    ) -> ProviderResult:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            if self.block:
                await asyncio.Event().wait()
            await asyncio.sleep(0.01)
            reference_bytes = b"".join(reference.read_bytes() for reference in request.references)
            return ProviderResult(
                artifacts=(Artifact("image/png", data=b"chatgpt-image:" + reference_bytes),),
                resolved=context.resolved,
            )
        finally:
            self.active -= 1

    async def cancel(self, provider_job_id: str) -> None:
        self.cancelled.append(provider_job_id)


def _context() -> ProviderExecutionContext:
    return ProviderExecutionContext(
        resolved=ResolvedExecution(
            requested_model="chatgpt/gpt-image-web",
            resolved_model="chatgpt/gpt-image-web",
            provider_id="chatgpt-web",
            provider_account_id="browser-profile",
            worker_id="local-worker",
            billing_pool="chatgpt-subscription",
        ),
        cancellation=CancellationToken(),
        timeout_seconds=1,
    )


def _request(request_id: str = "request-1") -> GenerationRequest:
    return GenerationRequest(
        request_id=request_id,
        prompt="draw",
        model="chatgpt/gpt-image-web",
        references=(ReferenceInput("image/png", data=b"reference"),),
        provider_options={"project": "sub2gen"},
    )


@pytest.mark.asyncio
async def test_chatgpt_adapter_passes_conformance_as_terminal_local_provider() -> None:
    provider = ChatGPTWebProvider(_ChatGPTBackend())

    report = await exercise_provider(provider, _request(), _context())

    assert report.provider_id == "chatgpt-web"
    assert provider.capabilities.supports_streaming is False
    assert provider.capabilities.execution_location == "local-worker"
    assert provider.capabilities.credential_kinds == frozenset({"browser_session"})


@pytest.mark.asyncio
async def test_chatgpt_adapter_serializes_the_browser_surface() -> None:
    backend = _ChatGPTBackend()
    provider = ChatGPTWebProvider(backend)

    await asyncio.gather(
        provider.generate(_request("one"), _context()),
        provider.generate(_request("two"), _context()),
    )

    assert backend.maximum_active == 1


@pytest.mark.asyncio
async def test_chatgpt_adapter_propagates_cancellation_to_running_work() -> None:
    backend = _ChatGPTBackend(block=True)
    provider = ChatGPTWebProvider(backend)
    context = _context()
    task = asyncio.create_task(provider.generate(_request(), context))
    await asyncio.sleep(0)
    context.cancellation.cancel()

    with pytest.raises(ProviderError) as caught:
        await task
    assert caught.value.code is ProviderErrorCode.CANCELLED


class _HealthyChrome:
    async def health(self) -> ProcessHealth:
        return ProcessHealth(True, "chrome-use", "1.5.87", "ready")


def _fake_imagegen(tmp_path: Path, *, sleep: bool = False) -> Path:
    executable = tmp_path / "chatgpt-imagegen"
    delay = "import time; time.sleep(30)" if sleep else ""
    executable.write_text(
        f"""#!{sys.executable}
import base64, os, pathlib, sys
{delay}
args = sys.argv[1:]
pathlib.Path(os.environ['FAKE_IMAGEGEN_LOG']).write_text('\\n'.join(args))
out = pathlib.Path(args[args.index('--out') + 1])
out.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='))
print(out)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


@pytest.mark.asyncio
async def test_process_backend_forces_web_and_returns_validated_image(tmp_path, monkeypatch) -> None:
    log = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_IMAGEGEN_LOG", str(log))
    backend = ChatGPTImagegenProcessBackend(_fake_imagegen(tmp_path), chrome_use=_HealthyChrome())
    provider = ChatGPTWebProvider(backend)

    result = await provider.generate(_request(), _context())

    assert result.artifacts[0].media_type == "image/png"
    args = log.read_text().splitlines()
    assert args[args.index("--backend") + 1] == "web"
    assert "--ref" in args
    assert "--no-style" in args


@pytest.mark.asyncio
async def test_process_backend_cancellation_terminates_subprocess(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_IMAGEGEN_LOG", str(tmp_path / "args.txt"))
    backend = ChatGPTImagegenProcessBackend(
        _fake_imagegen(tmp_path, sleep=True), chrome_use=_HealthyChrome()
    )
    provider = ChatGPTWebProvider(backend)
    context = _context()
    task = asyncio.create_task(provider.generate(_request("cancel-process"), context))
    await asyncio.sleep(0.05)
    context.cancellation.cancel()

    with pytest.raises(ProviderError) as caught:
        await task
    assert caught.value.code is ProviderErrorCode.CANCELLED
