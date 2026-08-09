from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from sub2gen.spikes.chatgpt_web import (
    ChatGPTWebSpikeRequest,
    SpikeOutcome,
    inspect_chatgpt_web_backend,
    run_chatgpt_web_spike,
)


_VALID_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


def _make_fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake-chatgpt-imagegen"
    script.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if len(sys.argv) > 1 and sys.argv[1] == "doctor":
    print("chatgpt-imagegen doctor")
    print("  [ok]    chrome-use 1.5.87")
    print("  [ok]    relay      connected")
    raise SystemExit(0)

args_path = os.environ.get("FAKE_ARGS_PATH")
if args_path:
    Path(args_path).write_text(json.dumps({{"argv": sys.argv[1:], "limit": os.environ.get("CHATGPT_IMAGEGEN_WEB_CONCURRENCY")}}))

mode = os.environ.get("FAKE_MODE", "success")
if mode in ("sleep", "cancel"):
    marker = os.environ.get("FAKE_CHILD_MARKER")
    subprocess.Popen([sys.executable, "-c", "import pathlib,time; time.sleep(0.6); pathlib.Path(" + repr(marker) + ").write_text('alive')"])
    time.sleep(30)
if mode == "serial":
    log = Path(os.environ["FAKE_SERIAL_LOG"])
    with log.open("a") as handle:
        handle.write("start\\n")
    time.sleep(0.2)
    with log.open("a") as handle:
        handle.write("end\\n")
if mode == "auth":
    print("Sign in to chatgpt.com before continuing", file=sys.stderr)
    raise SystemExit(1)
if mode == "quota":
    print("Too many requests; image quota reached", file=sys.stderr)
    raise SystemExit(1)
if mode == "refusal":
    print("ChatGPT finished without producing an image (refusal)", file=sys.stderr)
    raise SystemExit(1)
if mode == "browser":
    print("extension not connected; browser not reachable", file=sys.stderr)
    raise SystemExit(1)
if mode == "failure":
    print("unexpected upstream failure", file=sys.stderr)
    raise SystemExit(1)

out = Path(sys.argv[sys.argv.index("--out") + 1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(bytes({list(_VALID_PNG)!r}) if mode != "invalid" else b"not an image")
project = sys.argv[sys.argv.index("--project") + 1]
print(f"using project {{project!r}}", file=sys.stderr)
print("conversation deleted (no history kept)", file=sys.stderr)
print(out)
"""
    )
    script.chmod(0o755)
    return script


def _request(cli: Path, output: Path, **overrides: object) -> ChatGPTWebSpikeRequest:
    values: dict[str, object] = {
        "prompt": "a test image",
        "output_path": output,
        "cli_path": cli,
        "timeout_seconds": 3.0,
    }
    values.update(overrides)
    return ChatGPTWebSpikeRequest(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_doctor_detects_connected_web_backend(tmp_path: Path) -> None:
    cli = _make_fake_cli(tmp_path)

    result = await inspect_chatgpt_web_backend(cli_path=cli)

    assert result.web_ready is True
    assert result.chrome_use_ready is True
    assert result.relay_connected is True


@pytest.mark.asyncio
async def test_success_is_web_only_isolates_reference_and_publishes_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _make_fake_cli(tmp_path)
    reference = tmp_path / "reference.png"
    reference.write_bytes(_VALID_PNG)
    args_path = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS_PATH", str(args_path))

    output = tmp_path / "published" / "result.png"
    result = await run_chatgpt_web_spike(
        _request(cli, output, references=(reference,), project="sub2gen Assets")
    )

    assert result.outcome is SpikeOutcome.SUCCESS
    assert result.media_type == "image/png"
    assert (result.width, result.height) == (1, 1)
    assert result.byte_count == len(_VALID_PNG)
    assert result.sha256 is not None and len(result.sha256) == 64
    assert result.project_selected is True
    assert result.conversation_deleted is True
    assert output.read_bytes() == _VALID_PNG
    assert list(output.parent.glob("*.part")) == []

    invocation = json.loads(args_path.read_text())
    argv = invocation["argv"]
    assert argv[argv.index("--backend") + 1] == "web"
    assert "--no-style" in argv
    assert "--keep-conversation" not in argv
    assert invocation["limit"] == "1"
    copied_reference = Path(argv[argv.index("--ref") + 1])
    assert copied_reference != reference
    assert copied_reference.exists() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        ("auth", SpikeOutcome.AUTHENTICATION),
        ("quota", SpikeOutcome.QUOTA),
        ("refusal", SpikeOutcome.REFUSAL),
        ("browser", SpikeOutcome.BROWSER_UNAVAILABLE),
        ("failure", SpikeOutcome.FAILED),
    ],
)
async def test_failure_outcomes_are_structured_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    outcome: SpikeOutcome,
) -> None:
    cli = _make_fake_cli(tmp_path)
    monkeypatch.setenv("FAKE_MODE", mode)
    prompt = "private prompt text"

    result = await run_chatgpt_web_spike(_request(cli, tmp_path / "result.png", prompt=prompt))

    assert result.outcome is outcome
    assert result.output_path is None
    assert result.detail is not None
    assert prompt not in result.detail
    assert str(Path.home()) not in result.detail


@pytest.mark.asyncio
async def test_invalid_output_is_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_fake_cli(tmp_path)
    monkeypatch.setenv("FAKE_MODE", "invalid")
    output = tmp_path / "result.png"

    result = await run_chatgpt_web_spike(_request(cli, output))

    assert result.outcome is SpikeOutcome.INVALID_OUTPUT
    assert output.exists() is False


@pytest.mark.asyncio
async def test_invalid_reference_is_rejected_before_starting_upstream(tmp_path: Path) -> None:
    cli = _make_fake_cli(tmp_path)
    reference = tmp_path / "reference.png"
    reference.write_text("not an image")

    with pytest.raises(ValueError, match="invalid reference image"):
        await run_chatgpt_web_spike(
            _request(cli, tmp_path / "result.png", references=(reference,))
        )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process-group assertion is POSIX-specific")
async def test_timeout_kills_the_process_tree_and_cleans_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _make_fake_cli(tmp_path)
    marker = tmp_path / "child-survived"
    temp_parent = tmp_path / "temps"
    temp_parent.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_parent))
    monkeypatch.setenv("FAKE_MODE", "sleep")
    monkeypatch.setenv("FAKE_CHILD_MARKER", str(marker))

    result = await run_chatgpt_web_spike(
        _request(cli, tmp_path / "result.png", timeout_seconds=0.15, chrome_use_path=Path("/bin/true"))
    )
    await asyncio.sleep(0.8)

    assert result.outcome is SpikeOutcome.TIMEOUT
    assert marker.exists() is False
    assert list(temp_parent.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process-group assertion is POSIX-specific")
async def test_cancellation_kills_the_process_tree_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _make_fake_cli(tmp_path)
    marker = tmp_path / "child-survived"
    monkeypatch.setenv("FAKE_MODE", "cancel")
    monkeypatch.setenv("FAKE_CHILD_MARKER", str(marker))
    task = asyncio.create_task(
        run_chatgpt_web_spike(
            _request(cli, tmp_path / "result.png", timeout_seconds=10, chrome_use_path=Path("/bin/true"))
        )
    )
    await asyncio.sleep(0.1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.8)

    assert marker.exists() is False


@pytest.mark.asyncio
async def test_in_process_web_runs_are_serialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_fake_cli(tmp_path)
    serial_log = tmp_path / "serial.log"
    monkeypatch.setenv("FAKE_MODE", "serial")
    monkeypatch.setenv("FAKE_SERIAL_LOG", str(serial_log))

    first, second = await asyncio.gather(
        run_chatgpt_web_spike(_request(cli, tmp_path / "first.png")),
        run_chatgpt_web_spike(_request(cli, tmp_path / "second.png")),
    )

    assert first.succeeded and second.succeeded
    assert serial_log.read_text().splitlines() == ["start", "end", "start", "end"]
