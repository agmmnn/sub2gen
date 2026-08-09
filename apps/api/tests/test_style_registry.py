from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from sub2gen.generation.styles import StyleRegistry


def test_local_style_applies_prompt_and_pinned_reference(tmp_path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    (local / "reference.png").write_bytes(b"png")
    (local / "cinematic.json").write_text(
        json.dumps(
            {
                "id": "cinematic",
                "name": "Cinematic",
                "prompt_prefix": "cinematic still",
                "prompt_suffix": "soft natural light",
                "references": ["reference.png"],
            }
        )
    )

    registry = StyleRegistry(tmp_path)
    prompt, references = registry.apply(None, "a blue mug", ())

    assert prompt == "a blue mug"
    prompt, references = registry.apply("cinematic", "a blue mug", ())
    assert prompt == "cinematic still a blue mug soft natural light"
    assert references[0].read_bytes() == b"png"


def _package(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_remote_style_requires_checksum_review_and_explicit_runtime_enable(tmp_path) -> None:
    archive = _package(
        {
            "style.json": json.dumps({"id": "remote-look", "name": "Remote look"}).encode(),
        }
    )
    digest = hashlib.sha256(archive).hexdigest()
    registry = StyleRegistry(tmp_path)

    with pytest.raises(ValueError, match="checksum"):
        registry.stage_remote_package(archive, source_url="https://example.test/style.zip", expected_sha256="0" * 64)
    package_id = registry.stage_remote_package(
        archive,
        source_url="https://example.test/style.zip",
        expected_sha256=digest,
    )
    assert registry.list() == ()
    registry.approve_remote_package(package_id)
    assert registry.list() == ()

    enabled = StyleRegistry(tmp_path, remote_enabled=True)
    assert enabled.resolve("remote-look").source == f"remote:{digest}"


def test_remote_style_rejects_path_traversal(tmp_path) -> None:
    archive = _package({"style.json": b'{"id":"safe"}', "../escape.png": b"bad"})
    with pytest.raises(ValueError, match="unsafe"):
        StyleRegistry(tmp_path).stage_remote_package(
            archive,
            source_url="https://example.test/style.zip",
            expected_sha256=hashlib.sha256(archive).hexdigest(),
        )
