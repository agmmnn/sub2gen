import asyncio
import sqlite3
from pathlib import Path

from sub2gen.core.storage_errors import sqlite_operational_error_handler
from sub2gen.services import browser_metrics_cleanup as cleanup_module


def test_cleanup_removes_only_inactive_browser_metrics(tmp_path, monkeypatch):
    root = tmp_path / "browser_profiles"
    inactive = root / "token-1"
    active = root / "token-2"
    unrelated = root / "not-a-token"
    for profile in (inactive, active, unrelated):
        (profile / "BrowserMetrics").mkdir(parents=True)
        (profile / "BrowserMetrics" / "metrics.pma").write_bytes(b"x" * 128)
        (profile / "Cookies").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(
        cleanup_module,
        "profile_process_ids",
        lambda path: [123] if path.name == "token-2" else [],
    )
    stats = cleanup_module.cleanup_browser_metrics(root=root)

    assert stats.removed_directories == 1
    assert stats.skipped_active_profiles == 1
    assert stats.reclaimed_bytes == 128
    assert not (inactive / "BrowserMetrics").exists()
    assert (inactive / "Cookies").read_text(encoding="utf-8") == "keep"
    assert (active / "BrowserMetrics").is_dir()
    assert (unrelated / "BrowserMetrics").is_dir()


def test_cleanup_rejects_profile_outside_root(tmp_path):
    root = tmp_path / "browser_profiles"
    root.mkdir()
    outside = tmp_path / "token-9"
    (outside / "BrowserMetrics").mkdir(parents=True)
    stats = cleanup_module.cleanup_browser_metrics(root=root, profiles=[outside])
    assert stats.removed_directories == 0
    assert (outside / "BrowserMetrics").is_dir()


def test_sqlite_io_error_is_sanitized_as_503():
    error = sqlite3.OperationalError("disk I/O error at /secret/sub2gen.db")
    response = asyncio.run(sqlite_operational_error_handler(None, error))
    assert response.status_code == 503
    assert b"storage_unavailable" in response.body
    assert b"/secret/sub2gen.db" not in response.body


def test_sqlite_full_error_is_507():
    error = sqlite3.OperationalError("database or disk is full")
    response = asyncio.run(sqlite_operational_error_handler(None, error))
    assert response.status_code == 507
    assert b"storage_full" in response.body

