import io
import json
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sub2gen.services.google_drive_backup import (
    GoogleDriveBackupError,
    GoogleDriveBackupService,
    _build_archive,
    _validate_and_extract_archive,
)


def _make_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE admin_config (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE tokens (id INTEGER PRIMARY KEY)")
        connection.commit()


def test_archive_preserves_profile_data_and_excludes_runtime_metrics(tmp_path):
    database = tmp_path / "sub2gen.db"
    _make_database(database)
    profiles = tmp_path / "browser_profiles"
    profile = profiles / "token-1"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(b"private-cookie-data")
    (profile / "Default" / "Cache").write_bytes(b"normal-cache")
    (profile / "BrowserMetrics").mkdir()
    (profile / "BrowserMetrics" / "BrowserMetrics-test.pma").write_bytes(b"metrics")
    (profile / "SingletonLock").write_bytes(b"lock")
    (profile / "download.part").write_bytes(b"partial")

    work = tmp_path / "work"
    work.mkdir()
    archive = work / "backup.tar.gz"
    manifest = _build_archive(
        database,
        profiles,
        work,
        archive,
        backup_id="backup-1",
        backup_type="manual",
        app_version="test",
    )

    names = {entry["path"] for entry in manifest["files"]}
    assert "database/sub2gen.db" in names
    assert "browser_profiles/token-1/Default/Cookies" in names
    assert "browser_profiles/token-1/Default/Cache" not in names
    assert all("BrowserMetrics" not in name for name in names)
    assert all("Singleton" not in name for name in names)
    assert all(not name.endswith(".part") for name in names)

    extracted = tmp_path / "extracted"
    restored_manifest = _validate_and_extract_archive(archive, extracted)
    assert restored_manifest["backup_id"] == "backup-1"
    assert (extracted / "browser_profiles/token-1/Default/Cookies").read_bytes() == b"private-cookie-data"


def test_restore_rejects_archive_traversal(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    payload = b"escape"
    with tarfile.open(archive, "w:gz") as handle:
        member = tarfile.TarInfo("../outside")
        member.size = len(payload)
        handle.addfile(member, io.BytesIO(payload))
    with pytest.raises(GoogleDriveBackupError, match="unsafe path"):
        _validate_and_extract_archive(archive, tmp_path / "extract")


def test_restore_rejects_checksum_mismatch(tmp_path):
    database = tmp_path / "sub2gen.db"
    _make_database(database)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    archive = work / "backup.tar.gz"
    _build_archive(
        database,
        profiles,
        work,
        archive,
        backup_id="backup-2",
        backup_type="automatic",
        app_version="test",
    )

    unpacked = tmp_path / "unpacked"
    with tarfile.open(archive, "r:gz") as handle:
        handle.extractall(unpacked)
    manifest_path = unpacked / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    altered = tmp_path / "altered.tar.gz"
    with tarfile.open(altered, "w:gz") as handle:
        handle.add(manifest_path, arcname="manifest.json")
        handle.add(unpacked / "database/sub2gen.db", arcname="database/sub2gen.db")

    with pytest.raises(GoogleDriveBackupError, match="checksum"):
        _validate_and_extract_archive(altered, tmp_path / "restore")


def _postgres_restore_service(tmp_path: Path) -> GoogleDriveBackupService:
    service = object.__new__(GoogleDriveBackupService)
    service.tmp_root = tmp_path / "jobs"
    service.tmp_root.mkdir()
    service.profiles_root = tmp_path / "profiles"
    service.profiles_root.mkdir()
    service.database = SimpleNamespace()
    service._job = {"status": "running", "stage": "starting"}
    service.list_backups = AsyncMock(
        return_value=[{"id": "selected", "database_backend": "postgres"}]
    )
    service._wait_for_restore_drain = AsyncMock(return_value={})
    service._perform_backup = AsyncMock(return_value={"id": "safety"})
    return service


@pytest.mark.asyncio
async def test_postgres_restore_clears_maintenance_when_validation_fails_before_restore(
    tmp_path,
):
    service = _postgres_restore_service(tmp_path)
    service._download_backup = AsyncMock(side_effect=GoogleDriveBackupError("bad download"))
    service._restore_downloaded_postgres_archive = AsyncMock()
    redis = SimpleNamespace(
        ready=True,
        set_maintenance=AsyncMock(),
        client=SimpleNamespace(set=AsyncMock()),
    )

    with patch("sub2gen.services.google_drive_backup.redis_runtime", redis), patch(
        "sub2gen.services.google_drive_backup._schedule_restore_restart"
    ) as schedule_restart:
        await service._run_postgres_restore("job-1", "selected")

    assert service._job["stage"] == "failed"
    assert service._job["restart_required"] is False
    assert redis.set_maintenance.await_count == 2
    assert redis.set_maintenance.await_args_list[0].args == (True,)
    assert redis.set_maintenance.await_args_list[1].args == (False,)
    service._restore_downloaded_postgres_archive.assert_not_awaited()
    redis.client.set.assert_not_awaited()
    schedule_restart.assert_not_called()


@pytest.mark.asyncio
async def test_postgres_restore_keeps_maintenance_until_rollback_restart(tmp_path):
    service = _postgres_restore_service(tmp_path)
    service._download_backup = AsyncMock()
    calls = 0

    async def restore_archive(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            kwargs["on_database_restore_start"]()
            raise GoogleDriveBackupError("selected restore failed")
        return {"backup_id": "safety"}

    service._restore_downloaded_postgres_archive = AsyncMock(side_effect=restore_archive)
    redis = SimpleNamespace(
        ready=True,
        set_maintenance=AsyncMock(),
        client=SimpleNamespace(set=AsyncMock()),
    )

    with patch("sub2gen.services.google_drive_backup.redis_runtime", redis), patch(
        "sub2gen.services.google_drive_backup.BrowserProfileService.get_existing_instance",
        return_value=None,
    ), patch(
        "sub2gen.services.google_drive_backup._schedule_restore_restart"
    ) as schedule_restart:
        await service._run_postgres_restore("job-2", "selected")

    assert service._job["stage"] == "rollback_restart_pending"
    assert service._job["restart_required"] is True
    assert redis.set_maintenance.await_count == 1
    assert redis.set_maintenance.await_args.args == (True,)
    assert service._download_backup.await_count == 2
    assert service._restore_downloaded_postgres_archive.await_count == 2
    status_payload = redis.client.set.await_args.args[1]
    assert '"status":"rollback_restart_pending"' in status_payload
    schedule_restart.assert_called_once_with()
