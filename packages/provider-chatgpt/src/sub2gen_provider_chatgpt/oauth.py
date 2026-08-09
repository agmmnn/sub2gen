from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LocalCredentialReference:
    """Opaque worker-local reference; never resolves on the API host."""

    account_ref: str
    profile_ref: str

    def __post_init__(self) -> None:
        if not self.account_ref or not self.profile_ref:
            raise ValueError("local account and profile references are required")
