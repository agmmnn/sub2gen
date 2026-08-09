"""DigitalOcean Spaces cache backend."""

from __future__ import annotations

import asyncio
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

import httpx

from .base import CacheObject, CacheRead


@dataclass(frozen=True)
class DigitalOceanSpacesSettings:
    access_key_id: str
    secret_access_key: str
    region: str
    bucket: str
    prefix: str = "sub2gen/cache"
    delivery_mode: str = "proxy"
    cdn_base_url: str = ""
    api_token: str = ""
    cdn_endpoint_id: str = ""

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.region}.digitaloceanspaces.com"

    def missing(self) -> list[str]:
        required = {
            "SUB2GEN_DO_SPACES_ACCESS_KEY_ID": self.access_key_id,
            "SUB2GEN_DO_SPACES_SECRET_ACCESS_KEY": self.secret_access_key,
            "SUB2GEN_DO_SPACES_REGION": self.region,
            "SUB2GEN_DO_SPACES_BUCKET": self.bucket,
        }
        if self.delivery_mode == "cdn":
            required.update({
                "SUB2GEN_DO_SPACES_CDN_BASE_URL": self.cdn_base_url,
                "SUB2GEN_DO_API_TOKEN": self.api_token,
                "SUB2GEN_DO_CDN_ENDPOINT_ID": self.cdn_endpoint_id,
            })
        return [name for name, value in required.items() if not value]


class DigitalOceanSpacesBackend:
    provider = "digitalocean"

    def __init__(self, settings: DigitalOceanSpacesSettings):
        missing = settings.missing()
        if missing:
            raise ValueError("Missing DigitalOcean configuration: " + ", ".join(missing))
        try:
            import boto3
            from boto3.s3.transfer import TransferConfig
        except ImportError as exc:
            raise RuntimeError("boto3 is required for DigitalOcean Spaces cache") from exc
        self.settings = settings
        self._transfer_config = TransferConfig(multipart_threshold=8 * 1024 * 1024)
        self._client = boto3.client(
            "s3",
            region_name=settings.region,
            endpoint_url=settings.endpoint_url,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
        )

    def _key(self, name: str) -> str:
        safe = Path(name).name
        prefix = self.settings.prefix.strip("/")
        return str(PurePosixPath(prefix, safe)) if prefix else safe

    def _object(self, name: str, payload: dict[str, Any]) -> CacheObject:
        modified = payload.get("LastModified") or datetime.now(timezone.utc)
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=timezone.utc)
        return CacheObject(
            name=Path(name).name,
            key=self._key(name),
            size_bytes=int(payload.get("ContentLength", payload.get("Size", 0)) or 0),
            modified_at=modified,
            content_type=payload.get("ContentType") or mimetypes.guess_type(name)[0],
            etag=str(payload.get("ETag") or "").strip('"') or None,
        )

    @property
    def _acl(self) -> str:
        return "public-read" if self.settings.delivery_mode == "cdn" else "private"

    async def validate(self) -> dict[str, Any]:
        await asyncio.to_thread(self._client.head_bucket, Bucket=self.settings.bucket)
        return {"ok": True, **self.location()}

    async def store_bytes(self, name: str, content: bytes, content_type: str) -> CacheObject:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.settings.bucket,
            Key=self._key(name),
            Body=content,
            ContentType=content_type,
            ACL=self._acl,
        )
        result = await self.stat(name)
        if result is None:
            raise RuntimeError("Spaces upload completed but object was not found")
        return result

    async def store_file(self, name: str, path: Path, content_type: str) -> CacheObject:
        await asyncio.to_thread(
            self._client.upload_file,
            str(path),
            self.settings.bucket,
            self._key(name),
            ExtraArgs={"ContentType": content_type, "ACL": self._acl},
            Config=self._transfer_config,
        )
        result = await self.stat(name)
        if result is None:
            raise RuntimeError("Spaces upload completed but object was not found")
        return result

    async def stat(self, name: str) -> Optional[CacheObject]:
        try:
            payload = await asyncio.to_thread(
                self._client.head_object, Bucket=self.settings.bucket, Key=self._key(name)
            )
        except Exception as exc:
            response = getattr(exc, "response", {})
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(response, dict) else None
            if status == 404 or "Not Found" in str(exc) or "404" in str(exc):
                return None
            raise
        return self._object(name, payload)

    async def read_bytes(self, name: str) -> bytes:
        response = await asyncio.to_thread(
            self._client.get_object, Bucket=self.settings.bucket, Key=self._key(name)
        )
        body = response["Body"]
        try:
            return await asyncio.to_thread(body.read)
        finally:
            await asyncio.to_thread(body.close)

    async def open(self, name: str, range_header: Optional[str] = None) -> CacheRead:
        kwargs: dict[str, Any] = {"Bucket": self.settings.bucket, "Key": self._key(name)}
        if range_header:
            kwargs["Range"] = range_header
        response = await asyncio.to_thread(self._client.get_object, **kwargs)
        stream = response["Body"]

        async def _body() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await asyncio.to_thread(stream.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await asyncio.to_thread(stream.close)

        return CacheRead(
            body=_body(),
            status_code=206 if response.get("ContentRange") else 200,
            content_length=int(response.get("ContentLength", 0)),
            content_type=response.get("ContentType") or mimetypes.guess_type(name)[0] or "application/octet-stream",
            etag=str(response.get("ETag") or "").strip('"') or None,
            last_modified=response.get("LastModified"),
            content_range=response.get("ContentRange"),
        )

    async def list(self) -> list[CacheObject]:
        prefix = self.settings.prefix.strip("/")
        prefix = f"{prefix}/" if prefix else ""
        continuation: Optional[str] = None
        result: list[CacheObject] = []
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.settings.bucket, "Prefix": prefix}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            payload = await asyncio.to_thread(self._client.list_objects_v2, **kwargs)
            for row in payload.get("Contents", []):
                key = str(row.get("Key") or "")
                if not key or key.endswith("/"):
                    continue
                result.append(self._object(key.rsplit("/", 1)[-1], row))
            if not payload.get("IsTruncated"):
                break
            continuation = payload.get("NextContinuationToken")
        return sorted(result, key=lambda item: item.modified_at, reverse=True)

    async def delete(self, name: str) -> bool:
        existing = await self.stat(name)
        if existing is None:
            return False
        await asyncio.to_thread(
            self._client.delete_object, Bucket=self.settings.bucket, Key=self._key(name)
        )
        if self.settings.delivery_mode == "cdn":
            await self._purge([f"/{self._key(name)}"])
        return True

    async def clear(self) -> tuple[int, int]:
        objects = await self.list()
        if objects:
            for offset in range(0, len(objects), 1000):
                batch = objects[offset:offset + 1000]
                await asyncio.to_thread(
                    self._client.delete_objects,
                    Bucket=self.settings.bucket,
                    Delete={"Objects": [{"Key": item.key} for item in batch], "Quiet": True},
                )
        if self.settings.delivery_mode == "cdn":
            prefix = self.settings.prefix.strip("/")
            await self._purge([f"/{prefix}/*" if prefix else "*"])
        return len(objects), sum(item.size_bytes for item in objects)

    async def cleanup_expired(self, timeout: int) -> tuple[int, int]:
        if timeout <= 0:
            return 0, 0
        cutoff = datetime.now(timezone.utc).timestamp() - timeout
        expired = [item for item in await self.list() if item.modified_at.timestamp() < cutoff]
        for offset in range(0, len(expired), 1000):
            batch = expired[offset:offset + 1000]
            await asyncio.to_thread(
                self._client.delete_objects,
                Bucket=self.settings.bucket,
                Delete={"Objects": [{"Key": item.key} for item in batch], "Quiet": True},
            )
        if expired and self.settings.delivery_mode == "cdn":
            prefix = self.settings.prefix.strip("/")
            await self._purge([f"/{prefix}/*" if prefix else "*"])
        return len(expired), sum(item.size_bytes for item in expired)

    async def _purge(self, files: list[str]) -> None:
        url = f"https://api.digitalocean.com/v2/cdn/endpoints/{self.settings.cdn_endpoint_id}/cache"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                "DELETE",
                url,
                headers={"Authorization": f"Bearer {self.settings.api_token}"},
                json={"files": files},
            )
            response.raise_for_status()

    def public_url(self, name: str) -> Optional[str]:
        if self.settings.delivery_mode != "cdn":
            return None
        base = self.settings.cdn_base_url.rstrip("/")
        return f"{base}/{quote(self._key(name), safe='/')}"

    def location(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "region": self.settings.region,
            "bucket": self.settings.bucket,
            "prefix": self.settings.prefix.strip("/"),
            "cdn_base_url": self.settings.cdn_base_url.rstrip("/"),
        }
