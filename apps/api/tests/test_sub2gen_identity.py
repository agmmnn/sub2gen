from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess

import pytest

from sub2gen.core.api_key_manager import ApiKeyManager, MANAGED_API_KEY_PREFIX


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TEXT_SUFFIXES = {
    "",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


class _RejectingOldKeyRepository:
    def __init__(self) -> None:
        self.lookups = 0

    async def get_client_api_key_by_hash(self, _key_hash: str):
        self.lookups += 1
        return {
            "id": 1,
            "label": "old key",
            "key_prefix": "old_live_example",
            "is_active": True,
            "scopes": "*",
        }


def _tracked_source_candidates() -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [
        REPOSITORY_ROOT / relative_path.decode()
        for relative_path in output.split(b"\0")
        if relative_path
        and (REPOSITORY_ROOT / relative_path.decode()).is_file()
        and (REPOSITORY_ROOT / relative_path.decode()).suffix.lower() in TEXT_SUFFIXES
    ]


def test_distribution_and_import_identity_is_sub2gen_only() -> None:
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text()
    assert 'name = "sub2gen"' in pyproject
    assert 'sub2gen = "sub2gen.cli:main"' in pyproject
    assert importlib.util.find_spec("sub2gen") is not None
    assert importlib.util.find_spec("flow" + "2api") is None


def test_old_identity_does_not_exist_in_shipped_text_or_paths() -> None:
    forbidden_fragments = (
        "flow" + "2api",
        "Flow" + "2API",
        "FLOW" + "2API",
        "@flow" + "2api",
        "f" + "2a_live",
        "F" + "2A",
        "flow" + "2",
        "Flow" + "2",
    )
    violations: list[str] = []
    for path in _tracked_source_candidates():
        if "migrations" in path.parts and path.suffix == ".sql":
            # Applied SQL is immutable and the cutover migration names old columns.
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(fragment in text for fragment in forbidden_fragments):
            violations.append(str(path.relative_to(REPOSITORY_ROOT)))
        if any(fragment.lower() in path.name.lower() for fragment in ("flow" + "2api", "f" + "2a")):
            violations.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert violations == []


def test_new_managed_keys_use_sub2gen_prefix() -> None:
    raw_key, key_hash = ApiKeyManager.generate_key()

    assert raw_key.startswith(MANAGED_API_KEY_PREFIX)
    assert len(key_hash) == 64


@pytest.mark.asyncio
async def test_old_managed_key_namespace_is_not_looked_up() -> None:
    repository = _RejectingOldKeyRepository()
    manager = ApiKeyManager(
        db=object(),
        legacy_api_key_provider=lambda: "",
        repository=repository,  # type: ignore[arg-type]
    )

    with pytest.raises(PermissionError, match="Invalid API key"):
        await manager.authenticate(
            "old_live_example_secret",
            endpoint="/v1/models",
            enforce_rate_limits=False,
            touch_usage=False,
        )

    assert repository.lookups == 0
