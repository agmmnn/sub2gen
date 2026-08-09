"""Shared migration discovery and checksum validation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class MigrationError(RuntimeError):
    """Raised when migration history or a database schema is incompatible."""


@dataclass(frozen=True)
class MigrationFile:
    revision: str
    path: Path
    checksum: str
    sql_text: str


def discover_migrations(directory: Path, *, engine: str) -> list[MigrationFile]:
    migrations: list[MigrationFile] = []
    for path in sorted(directory.glob("*.sql")):
        revision = path.stem.split("_", 1)[0]
        if not revision.isdigit():
            raise MigrationError(f"Invalid {engine} migration filename: {path.name}")
        sql_text = path.read_text(encoding="utf-8")
        migrations.append(
            MigrationFile(
                revision=revision,
                path=path,
                checksum=hashlib.sha256(sql_text.encode("utf-8")).hexdigest(),
                sql_text=sql_text,
            )
        )
    if not migrations:
        raise MigrationError(f"No {engine} migrations found in {directory}")
    revisions = [item.revision for item in migrations]
    if len(revisions) != len(set(revisions)):
        raise MigrationError(f"Duplicate {engine} migration revisions")
    if revisions != sorted(revisions):
        raise MigrationError(f"Out-of-order {engine} migration revisions")
    return migrations
