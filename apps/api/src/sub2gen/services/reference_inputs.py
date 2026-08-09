"""Validated remote reference-image loading."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 3


class ReferenceInputError(ValueError):
    pass


async def load_remote_reference(url: str) -> tuple[str, bytes]:
    current = url
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _validate_public_http_url(current)
            async with client.stream("GET", current, headers={"accept": "image/*"}) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ReferenceInputError("reference redirect has no location")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if not media_type.startswith("image/"):
                    raise ReferenceInputError("remote reference is not an image")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_REFERENCE_BYTES:
                        raise ReferenceInputError("remote reference exceeds 20 MiB")
                if not content:
                    raise ReferenceInputError("remote reference is empty")
                return media_type, bytes(content)
        raise ReferenceInputError("remote reference has too many redirects")


async def _validate_public_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ReferenceInputError("reference URL must be public HTTP(S) without credentials")
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ReferenceInputError("reference host could not be resolved") from exc
    for address in {item[4][0] for item in addresses}:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ReferenceInputError("reference URL resolves to a non-public address")
