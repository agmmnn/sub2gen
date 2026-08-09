"""Local project setup managed by uv and Bun."""

from __future__ import annotations

import shutil
import subprocess

from .core.config import REPO_ROOT

PROJECT_ROOT = REPO_ROOT
FRONTEND_DIR = PROJECT_ROOT / "apps" / "admin-web"
STATIC_DIR = PROJECT_ROOT / "apps" / "api" / "static"


def build_frontend() -> None:
    """Install locked workspace dependencies and build the administration UI."""
    bun = shutil.which("bun")
    if not bun:
        raise SystemExit(
            "Bun is required to build the sub2gen frontend. Install it from "
            "https://bun.sh, then run `uv run setup` again."
        )

    print("[sub2gen] Syncing frontend dependencies with Bun...", flush=True)
    subprocess.run(
        [bun, "install", "--frozen-lockfile"],
        cwd=PROJECT_ROOT,
        check=True,
    )

    print("[sub2gen] Building frontend assets...", flush=True)
    subprocess.run(
        [
            bun,
            "run",
            "build",
            "--",
            "--outDir",
            str(STATIC_DIR),
            "--emptyOutDir",
        ],
        cwd=FRONTEND_DIR,
        check=True,
    )


def setup() -> None:
    """Install frontend dependencies and build the administration UI."""
    build_frontend()
    print(
        "[sub2gen] Setup complete. Start the server with `uv run sub2gen`.",
        flush=True,
    )
