"""Resolve opaque credential bindings only inside the owning execution location."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .domain import CredentialBindingRecord, CredentialStorageKind
from .unified_repositories import CredentialBindingRepository


class CredentialResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    credential_type: str
    secret: str = field(repr=False)
    binding_id: str


LegacyCredentialLoader = Callable[[CredentialBindingRecord], Awaitable[str]]


class CredentialResolver:
    """Internal resolver; list/admin APIs should use binding views, never this class."""

    def __init__(
        self,
        bindings: CredentialBindingRepository,
        *,
        legacy_loaders: dict[str, LegacyCredentialLoader] | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        self._bindings = bindings
        self._legacy_loaders = dict(legacy_loaders or {})
        self._environment = environment if environment is not None else os.environ

    async def resolve_for_api_host(self, binding_id: str) -> ResolvedCredential:
        binding = await self._bindings.get_for_resolution(binding_id)
        if binding is None or not binding.enabled:
            raise CredentialResolutionError("credential binding is unavailable")
        if binding.expires_at and self._expired(binding.expires_at):
            raise CredentialResolutionError("credential binding has expired")
        if binding.storage_kind in {
            CredentialStorageKind.WORKER_VAULT,
            CredentialStorageKind.BROWSER_SESSION,
        }:
            raise CredentialResolutionError("credential is available only on its assigned worker")

        try:
            if binding.storage_kind is CredentialStorageKind.ENV:
                secret = self._resolve_environment(binding.secret_ref)
            elif binding.storage_kind is CredentialStorageKind.LEGACY_TABLE:
                source = urlparse(binding.secret_ref).netloc
                loader = self._legacy_loaders.get(source)
                if loader is None:
                    raise CredentialResolutionError("no legacy credential loader is registered")
                secret = await loader(binding)
            else:
                raise CredentialResolutionError("unsupported credential storage kind")
        except CredentialResolutionError as exc:
            await self._bindings.record_validation(binding.id, error=str(exc))
            raise
        if not secret:
            await self._bindings.record_validation(binding.id, error="resolved credential is empty")
            raise CredentialResolutionError("resolved credential is empty")
        await self._bindings.record_validation(binding.id)
        return ResolvedCredential(binding.credential_type, secret, binding.id)

    def _resolve_environment(self, secret_ref: str) -> str:
        parsed = urlparse(secret_ref)
        if parsed.scheme != "env" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise CredentialResolutionError("invalid environment credential locator")
        value = self._environment.get(parsed.netloc, "")
        if not value:
            raise CredentialResolutionError("environment credential is not configured")
        return value

    @staticmethod
    def _expired(raw: str) -> bool:
        normalized = raw.replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError:
            raise CredentialResolutionError("credential expiry is invalid")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value <= datetime.now(timezone.utc)
