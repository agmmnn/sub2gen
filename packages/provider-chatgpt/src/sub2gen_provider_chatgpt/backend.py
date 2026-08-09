"""Subprocess backend around the history-linked chatgpt-imagegen CLI."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from sub2gen_provider_sdk import (
    GenerationRequest,
    ProviderError,
    ProviderErrorCode,
    ProviderExecutionContext,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderResult,
)

from .browser import ChromeUseProcessAdapter
from .outputs import read_image_artifact
from .projects import ConversationTarget
from .prompts import materialized_references
from .styles import LocalStyleSelection


class ChatGPTImagegenProcessBackend:
    def __init__(
        self,
        executable: str | Path,
        *,
        chrome_use: ChromeUseProcessAdapter | None = None,
        profile: str = "relay",
        target: ConversationTarget = ConversationTarget(),
    ) -> None:
        self.executable = str(executable)
        self.chrome_use = chrome_use or ChromeUseProcessAdapter()
        self.profile = profile
        self.target = target
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def health(self) -> ProviderHealth:
        if not Path(self.executable).is_file() or not os.access(self.executable, os.X_OK):
            return ProviderHealth("chatgpt-web", ProviderHealthStatus.UNAVAILABLE, "chatgpt-imagegen executable not found")
        chrome = await self.chrome_use.health()
        status = ProviderHealthStatus.READY if chrome.ready else ProviderHealthStatus.UNAVAILABLE
        return ProviderHealth("chatgpt-web", status, chrome.detail, {"chrome_use_version": chrome.version or ""})

    async def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> ProviderResult:
        with tempfile.TemporaryDirectory(prefix="sub2gen-chatgpt-output-") as directory, materialized_references(request) as refs:
            output = Path(directory) / "output.png"
            options = request.provider_options
            target = ConversationTarget(project=str(options.get("project") or self.target.project))
            command = [
                self.executable,
                request.prompt,
                "--backend", "web",
                "--profile", self.profile,
                "--out", str(output),
                "--format", "png",
                "--timeout", str(max(1, int(context.timeout_seconds))),
                "--quiet", "--no-progress",
                *target.cli_args(),
                *LocalStyleSelection().cli_args(),
            ]
            for reference in refs:
                command.extend(("--ref", str(reference)))
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            self._processes[request.request_id] = process
            try:
                stdout, stderr = await process.communicate()
            except asyncio.CancelledError:
                await self._terminate(process)
                raise
            finally:
                self._processes.pop(request.request_id, None)
            if process.returncode != 0:
                detail = stderr.decode(errors="replace")[-600:]
                code = ProviderErrorCode.AUTHENTICATION if "sign in" in detail.lower() else ProviderErrorCode.TRANSIENT
                raise ProviderError(code, detail or "chatgpt-imagegen failed", retryable=code is ProviderErrorCode.TRANSIENT)
            if not output.is_file():
                raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, stdout.decode(errors="replace")[-300:])
            return ProviderResult((read_image_artifact(output),), context.resolved)

    async def cancel(self, provider_job_id: str) -> None:
        process = self._processes.get(provider_job_id)
        if process is not None:
            await self._terminate(process)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.kill()
            await process.wait()
