"""Local filesystem cache backend."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from .base import CacheObject, CacheRead


def _parse_range(value: str, size: int) -> tuple[int, int]:
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("Invalid range")
    start_text, end_text = value[6:].split("-", 1)
    if not start_text:
        length = int(end_text)
        if length <= 0:
            raise ValueError("Invalid range")
        return max(0, size - length), size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("Unsatisfiable range")
    return start, min(end, size - 1)


class LocalCacheBackend:
    provider = "local"

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        safe = Path(name).name
        path = (self.cache_dir / safe).resolve()
        path.relative_to(self.cache_dir.resolve())
        return path

    async def validate(self) -> dict[str, Any]:
        await asyncio.to_thread(self.cache_dir.mkdir, parents=True, exist_ok=True)
        return {"ok": True, **self.location()}

    async def store_bytes(self, name: str, content: bytes, content_type: str) -> CacheObject:
        path = self._path(name)
        temp = path.with_suffix(f"{path.suffix}.part")
        def _write() -> None:
            temp.write_bytes(content)
            temp.replace(path)
        await asyncio.to_thread(_write)
        result = await self.stat(name)
        assert result is not None
        return result

    async def store_file(self, name: str, path: Path, content_type: str) -> CacheObject:
        target = self._path(name)
        if path.resolve() != target:
            await asyncio.to_thread(os.replace, path, target)
        result = await self.stat(name)
        assert result is not None
        return result

    async def stat(self, name: str) -> Optional[CacheObject]:
        path = self._path(name)
        try:
            info = await asyncio.to_thread(path.stat)
        except FileNotFoundError:
            return None
        if not path.is_file():
            return None
        return CacheObject(
            name=path.name,
            key=path.name,
            size_bytes=info.st_size,
            modified_at=datetime.fromtimestamp(info.st_mtime, timezone.utc),
            content_type=mimetypes.guess_type(path.name)[0],
        )

    async def read_bytes(self, name: str) -> bytes:
        return await asyncio.to_thread(self._path(name).read_bytes)

    async def open(self, name: str, range_header: Optional[str] = None) -> CacheRead:
        path = self._path(name)
        info = await asyncio.to_thread(path.stat)
        start, end = 0, info.st_size - 1
        status = 200
        content_range = None
        if range_header:
            start, end = _parse_range(range_header, info.st_size)
            status = 206
            content_range = f"bytes {start}-{end}/{info.st_size}"

        async def _body() -> AsyncIterator[bytes]:
            handle = await asyncio.to_thread(path.open, "rb")
            try:
                await asyncio.to_thread(handle.seek, start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = await asyncio.to_thread(handle.read, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                await asyncio.to_thread(handle.close)

        return CacheRead(
            body=_body(),
            status_code=status,
            content_length=end - start + 1,
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            last_modified=datetime.fromtimestamp(info.st_mtime, timezone.utc),
            content_range=content_range,
        )

    async def list(self) -> list[CacheObject]:
        result: list[CacheObject] = []
        for path in await asyncio.to_thread(lambda: list(self.cache_dir.iterdir())):
            if path.is_symlink() or not path.is_file() or path.name.endswith(".part"):
                continue
            item = await self.stat(path.name)
            if item:
                result.append(item)
        return sorted(result, key=lambda item: item.modified_at, reverse=True)

    async def delete(self, name: str) -> bool:
        path = self._path(name)
        try:
            await asyncio.to_thread(path.unlink)
            return True
        except FileNotFoundError:
            return False

    async def clear(self) -> tuple[int, int]:
        objects = await self.list()
        removed = 0
        removed_bytes = 0
        for item in objects:
            if await self.delete(item.name):
                removed += 1
                removed_bytes += item.size_bytes
        return removed, removed_bytes

    async def cleanup_expired(self, timeout: int) -> tuple[int, int]:
        if timeout <= 0:
            return 0, 0
        cutoff = time.time() - timeout
        removed = 0
        removed_bytes = 0
        for item in await self.list():
            if item.modified_at.timestamp() < cutoff and await self.delete(item.name):
                removed += 1
                removed_bytes += item.size_bytes
        return removed, removed_bytes

    def public_url(self, name: str) -> Optional[str]:
        return None

    def location(self) -> dict[str, Any]:
        return {"provider": self.provider, "cache_dir": str(self.cache_dir.resolve())}
