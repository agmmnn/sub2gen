"""Local-first prompt presets and reviewed style packages."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from sub2gen_provider_sdk import ReferenceInput


_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_MAX_PACKAGE_BYTES = 50 * 1024 * 1024
_MAX_ASSET_BYTES = 20 * 1024 * 1024
_MAX_FILES = 64


@dataclass(frozen=True, slots=True)
class StylePreset:
    style_id: str
    name: str
    prompt_prefix: str = ""
    prompt_suffix: str = ""
    references: tuple[ReferenceInput, ...] = ()
    source: str = "local"

    def apply(self, prompt: str) -> str:
        return " ".join(part.strip() for part in (self.prompt_prefix, prompt, self.prompt_suffix) if part.strip())


class StyleRegistry:
    """Loads local presets and, only when enabled, explicitly reviewed remote packages."""

    def __init__(self, root: Path, *, remote_enabled: bool = False) -> None:
        self.root = root
        self.local_dir = root / "local"
        self.pending_dir = root / "remote" / "pending"
        self.approved_dir = root / "remote" / "approved"
        self.remote_enabled = remote_enabled
        self._presets: dict[str, StylePreset] = {}
        self.reload()

    @classmethod
    def for_runtime(cls, root: Path) -> "StyleRegistry":
        enabled = (os.environ.get("SUB2GEN_ENABLE_REMOTE_STYLES") or "").strip().lower() in {
            "1", "true", "yes", "on"
        }
        return cls(root, remote_enabled=enabled)

    def reload(self) -> None:
        presets: dict[str, StylePreset] = {}
        for manifest in sorted(self.local_dir.glob("*.json")):
            preset = self._load_manifest(manifest, source="local")
            if preset.style_id in presets:
                raise ValueError(f"duplicate style ID: {preset.style_id}")
            presets[preset.style_id] = preset
        if self.remote_enabled:
            for package in sorted(path for path in self.approved_dir.iterdir() if path.is_dir()) if self.approved_dir.exists() else ():
                manifest = package / "style.json"
                if not manifest.is_file() or not (package / ".reviewed").is_file():
                    continue
                preset = self._load_manifest(manifest, source=f"remote:{package.name}")
                if preset.style_id in presets:
                    raise ValueError(f"duplicate style ID: {preset.style_id}")
                presets[preset.style_id] = preset
        self._presets = presets

    def list(self) -> tuple[StylePreset, ...]:
        return tuple(self._presets[key] for key in sorted(self._presets))

    def resolve(self, style_id: str) -> StylePreset:
        try:
            return self._presets[style_id]
        except KeyError as exc:
            raise KeyError(f"unknown style: {style_id}") from exc

    def apply(
        self,
        style_id: str | None,
        prompt: str,
        references: tuple[ReferenceInput, ...],
    ) -> tuple[str, tuple[ReferenceInput, ...]]:
        if not style_id:
            return prompt, references
        preset = self.resolve(style_id)
        return preset.apply(prompt), (*preset.references, *references)

    def stage_remote_package(self, archive: bytes, *, source_url: str, expected_sha256: str) -> str:
        """Validate and stage a package. Staging never makes it available to generation."""

        if not archive or len(archive) > _MAX_PACKAGE_BYTES:
            raise ValueError("style package must be between 1 byte and 50 MiB")
        digest = hashlib.sha256(archive).hexdigest()
        if digest != expected_sha256.lower():
            raise ValueError("style package checksum does not match")
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            members = [member for member in bundle.infolist() if not member.is_dir()]
            if len(members) > _MAX_FILES:
                raise ValueError("style package contains too many files")
            if sum(member.file_size for member in members) > _MAX_PACKAGE_BYTES:
                raise ValueError("expanded style package is too large")
            allowed = {".json", *_IMAGE_TYPES}
            for member in members:
                path = Path(member.filename)
                if path.is_absolute() or ".." in path.parts or path.suffix.lower() not in allowed:
                    raise ValueError(f"unsafe style package entry: {member.filename}")
            target = self.pending_dir / digest
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
            for member in members:
                destination = target / member.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(bundle.read(member))
        if not (target / "style.json").is_file():
            shutil.rmtree(target)
            raise ValueError("style package must contain style.json")
        self._load_manifest(target / "style.json", source=f"remote:{digest}")
        (target / "provenance.json").write_text(
            json.dumps({"source_url": source_url, "sha256": digest}, indent=2) + "\n",
            encoding="utf-8",
        )
        return digest

    def approve_remote_package(self, digest: str) -> None:
        """Mark a previously staged package as locally reviewed."""

        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid style package digest")
        source = self.pending_dir / digest
        if not source.is_dir():
            raise KeyError("style package is not staged")
        target = self.approved_dir / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        source.replace(target)
        (target / ".reviewed").write_text("reviewed\n", encoding="utf-8")
        self.reload()

    def _load_manifest(self, manifest: Path, *, source: str) -> StylePreset:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid style manifest: {manifest}") from exc
        style_id = str(payload.get("id") or "").strip()
        if not style_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in style_id):
            raise ValueError("style ID must contain lowercase letters, numbers, hyphens, or underscores")
        references: list[ReferenceInput] = []
        root = manifest.parent.resolve()
        for relative in payload.get("references") or ():
            asset = (root / str(relative)).resolve()
            if root not in asset.parents or not asset.is_file():
                raise ValueError(f"style reference is missing or escapes its package: {relative}")
            media_type = _IMAGE_TYPES.get(asset.suffix.lower())
            if media_type is None or asset.stat().st_size > _MAX_ASSET_BYTES:
                raise ValueError(f"unsupported or oversized style reference: {relative}")
            references.append(ReferenceInput(media_type, local_path=asset, name=asset.name))
        return StylePreset(
            style_id=style_id,
            name=str(payload.get("name") or style_id).strip(),
            prompt_prefix=str(payload.get("prompt_prefix") or ""),
            prompt_suffix=str(payload.get("prompt_suffix") or ""),
            references=tuple(references),
            source=source,
        )
