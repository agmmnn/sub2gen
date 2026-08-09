"""Async, web-only harness for the Phase 1 ChatGPT image-generation spike.

This module intentionally has no FastAPI route and does not define the future provider
SDK. It exists to measure the real browser integration before that interface is frozen.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image


DEFAULT_PROJECT = "sub2gen"
DEFAULT_SESSION = "sub2gen-phase1"
DEFAULT_TIMEOUT_SECONDS = 300.0
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_WEB_SLOT = asyncio.Lock()


class SpikeOutcome(StrEnum):
    """Sanitized terminal outcomes observed from the upstream CLI."""

    SUCCESS = "success"
    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    REFUSAL = "refusal"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ChatGPTWebSpikeRequest:
    """Input accepted by the throwaway Phase 1 harness."""

    prompt: str
    output_path: Path
    references: tuple[Path, ...] = ()
    project: str = DEFAULT_PROJECT
    profile: str = "relay"
    web_model: str = "Instant,Auto"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    cli_path: Path | None = None
    chrome_use_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ChatGPTWebSpikeResult:
    """Technical measurements and sanitized evidence from one invocation."""

    outcome: SpikeOutcome
    elapsed_seconds: float
    output_path: str | None = None
    media_type: str | None = None
    byte_count: int | None = None
    width: int | None = None
    height: int | None = None
    sha256: str | None = None
    project_selected: bool = False
    conversation_deleted: bool = False
    detail: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is SpikeOutcome.SUCCESS

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        payload["succeeded"] = self.succeeded
        return payload


@dataclass(frozen=True, slots=True)
class ChatGPTWebDoctorResult:
    cli_path: str | None
    chrome_use_ready: bool
    relay_connected: bool
    web_ready: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _resolve_executable(explicit: Path | None, env_name: str, command: str) -> str | None:
    candidate = str(explicit) if explicit else os.environ.get(env_name)
    if candidate:
        path = Path(candidate).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(command)


def _validate_request(request: ChatGPTWebSpikeRequest) -> None:
    if not request.prompt.strip():
        raise ValueError("prompt must not be empty")
    if request.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if not request.project.strip():
        raise ValueError("project must not be empty for the Phase 1 spike")
    for reference in request.references:
        if not reference.is_file():
            raise ValueError(f"reference image does not exist: {reference}")


def _sanitize_detail(
    raw: str,
    *,
    prompt: str = "",
    references: Sequence[Path] = (),
    temp_root: Path | None = None,
) -> str:
    value = _ANSI_ESCAPE.sub("", raw).strip()
    replacements = [(str(Path.home()), "<home>")]
    if prompt:
        replacements.append((prompt, "<prompt>"))
    if temp_root:
        replacements.append((str(temp_root), "<temp>"))
    replacements.extend((str(path), "<reference>") for path in references)
    for original, replacement in replacements:
        value = value.replace(original, replacement)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return " / ".join(lines[-5:])[:800]


def _classify_failure(stderr: str, stdout: str) -> SpikeOutcome:
    message = f"{stderr}\n{stdout}".lower()
    if any(
        marker in message
        for marker in (
            "sign in to chatgpt.com",
            "isn't signed in",
            "is not signed in",
            "no accesstoken",
            "authentication",
            "unauthorized",
        )
    ):
        return SpikeOutcome.AUTHENTICATION
    if any(
        marker in message
        for marker in (
            "quota",
            "rate-limited",
            "rate limited",
            "too many requests",
            "requests too quickly",
        )
    ):
        return SpikeOutcome.QUOTA
    if any(
        marker in message
        for marker in (
            "finished without producing an image",
            "refusal",
            "refused",
            "content policy",
        )
    ):
        return SpikeOutcome.REFUSAL
    if any(
        marker in message
        for marker in (
            "chrome-use` is not installed",
            "chrome-use is not installed",
            "extension not connected",
            "relay isn't connected",
            "relay is not connected",
            "browser not reachable",
            "composer never appeared",
            "no logged-in chrome profile",
        )
    ):
        return SpikeOutcome.BROWSER_UNAVAILABLE
    if any(marker in message for marker in ("timed out", "timeout", "stalled")):
        return SpikeOutcome.TIMEOUT
    return SpikeOutcome.FAILED


def _inspect_image(data: bytes) -> tuple[str, int, int]:
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
        image_format = (image.format or "").upper()
        width, height = image.size
    media_types = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "WEBP": "image/webp",
    }
    if image_format not in media_types or width <= 0 or height <= 0:
        raise ValueError("output is not a supported non-empty image")
    return media_types[image_format], width, height


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
        return
    except TimeoutError:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        process.kill()
    await process.wait()


async def _close_browser_session(chrome_use_path: str | None, session: str) -> None:
    if chrome_use_path is None:
        return
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            chrome_use_path,
            "close",
            "--session",
            session,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        await asyncio.wait_for(process.wait(), timeout=10.0)
    except TimeoutError:
        if process is not None:
            await _terminate_process_tree(process)
    except OSError:
        return


async def _publish_output(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        await asyncio.to_thread(shutil.copyfile, source, partial)
        await asyncio.to_thread(os.replace, partial, destination)
    finally:
        partial.unlink(missing_ok=True)


async def inspect_chatgpt_web_backend(
    *,
    cli_path: Path | None = None,
    timeout_seconds: float = 30.0,
) -> ChatGPTWebDoctorResult:
    """Run the upstream read-only doctor and summarize web readiness."""

    resolved_cli = _resolve_executable(cli_path, "SUB2GEN_CHATGPT_IMAGEGEN_CLI", "chatgpt-imagegen")
    if resolved_cli is None:
        return ChatGPTWebDoctorResult(None, False, False, False, "chatgpt-imagegen executable not found")
    try:
        process = await asyncio.create_subprocess_exec(
            resolved_cli,
            "doctor",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await _terminate_process_tree(process)
        return ChatGPTWebDoctorResult(resolved_cli, False, False, False, "doctor timed out")
    except OSError as error:
        return ChatGPTWebDoctorResult(resolved_cli, False, False, False, str(error))

    output = f"{stdout_bytes.decode(errors='replace')}\n{stderr_bytes.decode(errors='replace')}"
    clean = _sanitize_detail(output)
    chrome_use_ready = bool(re.search(r"\[ok\]\s+chrome-use\b", output))
    relay_connected = bool(re.search(r"\[ok\]\s+relay\b", output))
    web_ready = process.returncode == 0 and chrome_use_ready and relay_connected
    return ChatGPTWebDoctorResult(resolved_cli, chrome_use_ready, relay_connected, web_ready, clean)


async def run_chatgpt_web_spike(request: ChatGPTWebSpikeRequest) -> ChatGPTWebSpikeResult:
    """Run one serialized ChatGPT Web generation through the upstream CLI."""

    _validate_request(request)
    started = time.monotonic()
    resolved_cli = _resolve_executable(
        request.cli_path,
        "SUB2GEN_CHATGPT_IMAGEGEN_CLI",
        "chatgpt-imagegen",
    )
    resolved_chrome_use = _resolve_executable(
        request.chrome_use_path,
        "SUB2GEN_CHROME_USE_CLI",
        "chrome-use",
    )
    if resolved_cli is None:
        return ChatGPTWebSpikeResult(
            outcome=SpikeOutcome.BROWSER_UNAVAILABLE,
            elapsed_seconds=time.monotonic() - started,
            detail="chatgpt-imagegen executable not found",
        )

    session = DEFAULT_SESSION
    async with _WEB_SLOT:
        with tempfile.TemporaryDirectory(prefix="sub2gen-chatgpt-web-") as temp_name:
            temp_root = Path(temp_name)
            temp_output = temp_root / "generated.png"
            copied_references: list[Path] = []
            for index, reference in enumerate(request.references):
                reference_bytes = await asyncio.to_thread(reference.read_bytes)
                try:
                    _inspect_image(reference_bytes)
                except Exception as error:
                    raise ValueError(f"invalid reference image {reference}: {error}") from error
                copied = temp_root / f"reference-{index}{reference.suffix.lower()}"
                await asyncio.to_thread(copied.write_bytes, reference_bytes)
                copied_references.append(copied)

            command = [
                resolved_cli,
                request.prompt,
                "--backend",
                "web",
                "--profile",
                request.profile,
                "--project",
                request.project,
                "--web-model",
                request.web_model,
                "--session",
                session,
                "--timeout",
                str(max(1, int(request.timeout_seconds))),
                "--format",
                "png",
                "--no-style",
                "--quiet",
                "--out",
                str(temp_output),
            ]
            for reference in copied_references:
                command.extend(("--ref", str(reference)))

            environment: Mapping[str, str] = {
                **os.environ,
                "CHATGPT_IMAGEGEN_BACKEND": "web",
                "CHATGPT_IMAGEGEN_WEB_CONCURRENCY": "1",
            }
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                    start_new_session=os.name == "posix",
                )
            except OSError as error:
                return ChatGPTWebSpikeResult(
                    outcome=SpikeOutcome.BROWSER_UNAVAILABLE,
                    elapsed_seconds=time.monotonic() - started,
                    detail=_sanitize_detail(str(error), prompt=request.prompt, references=request.references),
                )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=request.timeout_seconds,
                )
            except TimeoutError:
                await _terminate_process_tree(process)
                await _close_browser_session(resolved_chrome_use, session)
                return ChatGPTWebSpikeResult(
                    outcome=SpikeOutcome.TIMEOUT,
                    elapsed_seconds=time.monotonic() - started,
                    detail=f"generation exceeded {request.timeout_seconds:g}s",
                )
            except asyncio.CancelledError:
                await _terminate_process_tree(process)
                await asyncio.shield(_close_browser_session(resolved_chrome_use, session))
                raise

            stdout = stdout_bytes.decode(errors="replace")
            stderr = stderr_bytes.decode(errors="replace")
            project_selected = f"using project {request.project!r}" in stderr
            conversation_deleted = "conversation deleted (no history kept)" in stderr
            if process.returncode != 0:
                return ChatGPTWebSpikeResult(
                    outcome=_classify_failure(stderr, stdout),
                    elapsed_seconds=time.monotonic() - started,
                    project_selected=project_selected,
                    conversation_deleted=conversation_deleted,
                    detail=_sanitize_detail(
                        stderr or stdout,
                        prompt=request.prompt,
                        references=request.references,
                        temp_root=temp_root,
                    ),
                )
            if not temp_output.is_file():
                return ChatGPTWebSpikeResult(
                    outcome=SpikeOutcome.INVALID_OUTPUT,
                    elapsed_seconds=time.monotonic() - started,
                    project_selected=project_selected,
                    conversation_deleted=conversation_deleted,
                    detail="upstream CLI exited successfully without an output file",
                )

            image_bytes = await asyncio.to_thread(temp_output.read_bytes)
            try:
                media_type, width, height = _inspect_image(image_bytes)
            except Exception as error:
                return ChatGPTWebSpikeResult(
                    outcome=SpikeOutcome.INVALID_OUTPUT,
                    elapsed_seconds=time.monotonic() - started,
                    project_selected=project_selected,
                    conversation_deleted=conversation_deleted,
                    detail=_sanitize_detail(str(error), temp_root=temp_root),
                )
            await _publish_output(temp_output, request.output_path)
            return ChatGPTWebSpikeResult(
                outcome=SpikeOutcome.SUCCESS,
                elapsed_seconds=time.monotonic() - started,
                output_path=str(request.output_path),
                media_type=media_type,
                byte_count=len(image_bytes),
                width=width,
                height=height,
                sha256=hashlib.sha256(image_bytes).hexdigest(),
                project_selected=project_selected,
                conversation_deleted=conversation_deleted,
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Private ChatGPT Web vertical-spike harness")
    parser.add_argument("--cli", type=Path, default=None, help="Path to chatgpt-imagegen")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check the local web backend")
    doctor.add_argument("--timeout", type=float, default=30.0)

    generate = subparsers.add_parser("generate", help="Run one web-only image generation")
    generate.add_argument("prompt")
    generate.add_argument("--out", type=Path, required=True)
    generate.add_argument("--ref", type=Path, action="append", default=[])
    generate.add_argument("--project", default=DEFAULT_PROJECT)
    generate.add_argument("--profile", default="relay")
    generate.add_argument("--web-model", default="Instant,Auto")
    generate.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    generate.add_argument("--chrome-use", type=Path, default=None)
    return parser


async def _async_main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "doctor":
        result = await inspect_chatgpt_web_backend(cli_path=args.cli, timeout_seconds=args.timeout)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0 if result.web_ready else 1
    request = ChatGPTWebSpikeRequest(
        prompt=args.prompt,
        output_path=args.out,
        references=tuple(args.ref),
        project=args.project,
        profile=args.profile,
        web_model=args.web_model,
        timeout_seconds=args.timeout,
        cli_path=args.cli,
        chrome_use_path=args.chrome_use,
    )
    result = await run_chatgpt_web_spike(request)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.succeeded else 1


def main() -> None:
    raise SystemExit(asyncio.run(_async_main()))


if __name__ == "__main__":
    main()
