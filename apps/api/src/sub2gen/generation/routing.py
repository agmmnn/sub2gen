"""Trusted execution policy and deterministic target routing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sub2gen_provider_sdk import ResolvedExecution

from .catalog import ModelDescriptor, ModelRegistry
from .signals import RouteHealth, RouteSignal


class RoutingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedCallerContext:
    api_key_id: int | None
    allowed_provider_account_ids: frozenset[str]
    allowed_worker_ids: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset({"*"})


@dataclass(frozen=True, slots=True)
class TrustedRoutingConfig:
    allowed_providers: frozenset[str]
    allowed_billing_pools: frozenset[str]
    allowed_credential_kinds: frozenset[str]
    allowed_worker_ids: frozenset[str] = frozenset()
    allow_cross_billing_fallback: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    allowed_providers: frozenset[str]
    allowed_provider_account_ids: frozenset[str]
    allowed_worker_ids: frozenset[str]
    allowed_credential_kinds: frozenset[str]
    allowed_billing_pools: frozenset[str]
    required_capability: str
    allow_cross_billing_fallback: bool = False

    @classmethod
    def from_trusted_context(
        cls,
        descriptor: ModelDescriptor,
        config: TrustedRoutingConfig,
        caller: AuthenticatedCallerContext,
    ) -> "ExecutionPolicy":
        workers = caller.allowed_worker_ids
        if config.allowed_worker_ids:
            workers = workers & config.allowed_worker_ids if workers else config.allowed_worker_ids
        return cls(
            allowed_providers=config.allowed_providers,
            allowed_provider_account_ids=caller.allowed_provider_account_ids,
            allowed_worker_ids=workers,
            allowed_credential_kinds=config.allowed_credential_kinds,
            allowed_billing_pools=config.allowed_billing_pools,
            required_capability=descriptor.capability,
            allow_cross_billing_fallback=config.allow_cross_billing_fallback,
        )


@dataclass(frozen=True, slots=True)
class ExecutionCandidate:
    provider_id: str
    provider_account_id: str
    worker_id: str | None
    credential_kind: str
    billing_pool: str
    capabilities: frozenset[str]
    signal: RouteSignal = RouteSignal()


class GenerationRouter:
    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    def resolve(
        self,
        *,
        requested_model: str,
        request_id: str,
        policy: ExecutionPolicy,
        candidates: tuple[ExecutionCandidate, ...],
    ) -> ResolvedExecution:
        descriptor = self.registry.resolve(requested_model)
        if descriptor.provider_id not in policy.allowed_providers:
            raise RoutingError("model provider is not allowed")
        if descriptor.billing_pool not in policy.allowed_billing_pools:
            raise RoutingError("model billing pool is not allowed")
        eligible = [candidate for candidate in candidates if self._eligible(descriptor, policy, candidate)]
        if not eligible:
            raise RoutingError("no eligible execution target")
        eligible.sort(key=lambda candidate: self._rank(request_id, candidate))
        selected = eligible[0]
        return ResolvedExecution(
            requested_model=requested_model,
            resolved_model=descriptor.resolved_model,
            provider_id=selected.provider_id,
            billing_pool=selected.billing_pool,
            provider_account_id=selected.provider_account_id,
            worker_id=selected.worker_id,
        )

    @staticmethod
    def _eligible(descriptor: ModelDescriptor, policy: ExecutionPolicy, candidate: ExecutionCandidate) -> bool:
        if candidate.provider_id != descriptor.provider_id:
            return False
        if candidate.provider_account_id not in policy.allowed_provider_account_ids:
            return False
        if policy.allowed_worker_ids and candidate.worker_id not in policy.allowed_worker_ids:
            return False
        if candidate.credential_kind not in descriptor.credential_kinds:
            return False
        if candidate.credential_kind not in policy.allowed_credential_kinds:
            return False
        if descriptor.capability not in candidate.capabilities or policy.required_capability not in candidate.capabilities:
            return False
        if candidate.billing_pool not in policy.allowed_billing_pools:
            return False
        if candidate.billing_pool != descriptor.billing_pool and not policy.allow_cross_billing_fallback:
            return False
        if candidate.signal.health is RouteHealth.UNAVAILABLE:
            return False
        if candidate.signal.quota_remaining is not None and candidate.signal.quota_remaining <= 0:
            return False
        if candidate.signal.available_slots is not None and candidate.signal.available_slots <= 0:
            return False
        return True

    @staticmethod
    def _rank(request_id: str, candidate: ExecutionCandidate) -> tuple[int, int, int, str]:
        health_rank = 0 if candidate.signal.health is RouteHealth.READY else 1
        slots_rank = -(candidate.signal.available_slots or 0)
        quota_rank = -(candidate.signal.quota_remaining or 0)
        identity = f"{candidate.provider_account_id}:{candidate.worker_id or ''}"
        stable = hashlib.sha256(f"{request_id}:{identity}".encode()).hexdigest()
        return health_rank, slots_rank, quota_rank, stable
