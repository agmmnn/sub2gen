"""Shared cache backend contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Protocol


@dataclass(frozen=True)
class CacheObject:
    name: str
    key: str
    size_bytes: int
    modified_at: datetime
    content_type: Optional[str] = None
    etag: Optional[str] = None


@dataclass
class CacheRead:
    body: AsyncIterator[bytes]
    status_code: int
    content_length: int
    content_type: str
    etag: Optional[str] = None
    last_modified: Optional[datetime] = None
    content_range: Optional[str] = None


class CacheBackend(Protocol):
    provider: str

    async def validate(self) -> dict[str, Any]: ...
    async def store_bytes(self, name: str, content: bytes, content_type: str) -> CacheObject: ...
    async def store_file(self, name: str, path: Path, content_type: str) -> CacheObject: ...
    async def stat(self, name: str) -> Optional[CacheObject]: ...
    async def read_bytes(self, name: str) -> bytes: ...
    async def open(self, name: str, range_header: Optional[str] = None) -> CacheRead: ...
    async def list(self) -> list[CacheObject]: ...
    async def delete(self, name: str) -> bool: ...
    async def clear(self) -> tuple[int, int]: ...
    async def cleanup_expired(self, timeout: int) -> tuple[int, int]: ...
    def public_url(self, name: str) -> Optional[str]: ...
    def location(self) -> dict[str, Any]: ...
