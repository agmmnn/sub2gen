"""Safe reclamation of Chromium BrowserMetrics data from persistent profiles."""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

from ..core.config import get_runtime_data_dir


_STATS_LOCK = threading.Lock()


@dataclass
class BrowserMetricsCleanupStats:
    scanned_profiles: int = 0
    removed_directories: int = 0
    reclaimed_bytes: int = 0
    skipped_active_profiles: int = 0
    failures: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


_LAST_STATS = BrowserMetricsCleanupStats()


def browser_profile_root() -> Path:
    return (get_runtime_data_dir() / "browser_profiles").resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _directory_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def profile_process_ids(profile_path: Path) -> list[int]:
    """Return Chromium processes using exactly this persistent profile."""
    proc_root = Path("/proc")
    if os.name == "nt" or not proc_root.exists():
        return []
    resolved = os.path.normcase(str(profile_path.resolve()))
    process_ids: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = [
                value.decode("utf-8", errors="surrogateescape")
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
        except (OSError, ValueError):
            continue
        for index, arg in enumerate(args):
            candidate: Optional[str] = None
            if arg.startswith("--user-data-dir="):
                candidate = arg.split("=", 1)[1]
            elif arg == "--user-data-dir" and index + 1 < len(args):
                candidate = args[index + 1]
            if candidate and os.path.normcase(str(Path(candidate).resolve())) == resolved:
                process_ids.append(int(entry.name))
                break
    return process_ids


def _validated_metrics_path(profile_path: Path, root: Path) -> Optional[Path]:
    profile = profile_path.resolve()
    if profile.parent != root or not profile.name.startswith("token-"):
        return None
    metrics = (profile / "BrowserMetrics").resolve()
    if metrics.name != "BrowserMetrics" or metrics.parent != profile:
        return None
    if not _is_relative_to(metrics, root):
        return None
    return metrics


def cleanup_browser_metrics(
    *,
    root: Optional[Path] = None,
    profiles: Optional[Iterable[Path]] = None,
) -> BrowserMetricsCleanupStats:
    """Delete only inactive ``token-*/BrowserMetrics`` directories."""
    resolved_root = (root or browser_profile_root()).resolve()
    stats = BrowserMetricsCleanupStats()
    if not resolved_root.is_dir():
        _set_last_stats(stats)
        return stats

    candidates = list(profiles) if profiles is not None else list(resolved_root.glob("token-*"))
    for candidate in candidates:
        stats.scanned_profiles += 1
        metrics = _validated_metrics_path(Path(candidate), resolved_root)
        if metrics is None or not metrics.is_dir() or metrics.is_symlink():
            continue
        if profile_process_ids(metrics.parent):
            stats.skipped_active_profiles += 1
            continue
        size = _directory_size(metrics)
        try:
            shutil.rmtree(metrics)
        except OSError:
            stats.failures += 1
            continue
        stats.removed_directories += 1
        stats.reclaimed_bytes += size

    _set_last_stats(stats)
    return stats


def _set_last_stats(stats: BrowserMetricsCleanupStats) -> None:
    global _LAST_STATS
    with _STATS_LOCK:
        _LAST_STATS = BrowserMetricsCleanupStats(**stats.to_dict())


def get_last_browser_metrics_cleanup_stats() -> dict[str, int]:
    with _STATS_LOCK:
        return _LAST_STATS.to_dict()

