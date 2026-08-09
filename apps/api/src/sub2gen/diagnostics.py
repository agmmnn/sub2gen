"""Secret-free release diagnostics for local installations."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .core.config import get_runtime_data_dir
from .generation.styles import StyleRegistry
from .persistence.migrations.sqlite import discover_sqlite_migrations


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    ok: bool
    detail: str


def _writable_directory(path: Path) -> DiagnosticCheck:
    try:
        path.mkdir(parents=True, exist_ok=True)
        descriptor, probe = tempfile.mkstemp(prefix=".sub2gen-doctor-", dir=path)
        os.close(descriptor)
        Path(probe).unlink()
    except OSError as exc:
        return DiagnosticCheck("runtime_data", False, f"not writable: {exc.__class__.__name__}")
    return DiagnosticCheck("runtime_data", True, "writable")


def collect_diagnostics(*, runtime_dir: Path | None = None) -> tuple[DiagnosticCheck, ...]:
    data_dir = runtime_dir or get_runtime_data_dir()
    checks: list[DiagnosticCheck] = [
        DiagnosticCheck("python", sys.version_info >= (3, 11), platform.python_version()),
        _writable_directory(data_dir),
    ]
    migrations = discover_sqlite_migrations()
    checks.append(DiagnosticCheck("sqlite_migrations", bool(migrations), f"latest={migrations[-1].revision}"))
    try:
        with sqlite3.connect(":memory:") as connection:
            for migration in migrations:
                connection.executescript(migration.sql_text)
        checks.append(DiagnosticCheck("fresh_schema", True, "all SQLite migrations apply"))
    except sqlite3.Error as exc:
        checks.append(DiagnosticCheck("fresh_schema", False, exc.__class__.__name__))
    for module in (
        "sub2gen_provider_sdk",
        "sub2gen_provider_google_flow",
        "sub2gen_provider_chatgpt",
        "sub2gen_provider_google_gemini",
        "sub2gen_worker_protocol",
    ):
        checks.append(DiagnosticCheck(f"module:{module}", importlib.util.find_spec(module) is not None, "available"))
    try:
        styles = StyleRegistry.for_runtime(data_dir / "styles").list()
        checks.append(DiagnosticCheck("styles", True, f"active={len(styles)}"))
    except (OSError, ValueError) as exc:
        checks.append(DiagnosticCheck("styles", False, exc.__class__.__name__))
    return tuple(checks)


def run_doctor(*, json_output: bool = False) -> int:
    checks = collect_diagnostics()
    if json_output:
        print(json.dumps({"ok": all(item.ok for item in checks), "checks": [asdict(item) for item in checks]}, indent=2))
    else:
        for item in checks:
            mark = "OK" if item.ok else "FAIL"
            print(f"[{mark}] {item.name}: {item.detail}")
    return 0 if all(item.ok for item in checks) else 1
