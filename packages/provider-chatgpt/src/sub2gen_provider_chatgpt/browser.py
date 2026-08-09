"""Typed process boundary for chrome-use and browser-backed image generation."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

MINIMUM_CHROME_USE_VERSION = (1, 5, 87)


@dataclass(frozen=True, slots=True)
class ProcessHealth:
    ready: bool
    executable: str | None
    version: str | None
    detail: str


class ChromeUseProcessAdapter:
    def __init__(self, executable: str | Path | None = None) -> None:
        self.executable = str(executable) if executable else os.environ.get("SUB2GEN_CHROME_USE_CLI") or shutil.which("chrome-use")

    async def health(self) -> ProcessHealth:
        if not self.executable:
            return ProcessHealth(False, None, None, "chrome-use executable not found; install chrome-use >= 1.5.87")
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, TimeoutError) as exc:
            return ProcessHealth(False, self.executable, None, f"chrome-use health check failed: {exc}")
        raw = (stdout + stderr).decode(errors="replace").strip()
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
        if process.returncode != 0 or not match:
            return ProcessHealth(False, self.executable, None, "chrome-use version could not be detected")
        version_tuple = tuple(int(part) for part in match.groups())
        version = ".".join(match.groups())
        if version_tuple < MINIMUM_CHROME_USE_VERSION:
            return ProcessHealth(False, self.executable, version, "chrome-use 1.5.87 or newer is required")
        return ProcessHealth(True, self.executable, version, "chrome-use is available")
