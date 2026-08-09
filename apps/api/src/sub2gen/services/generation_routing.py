"""Build routing candidates exclusively from trusted persistence and runtime state."""

from __future__ import annotations

from datetime import datetime, timezone

from ..core.config import config as app_config
from ..core.api_key_manager import AuthContext
from ..generation.catalog import ModelRegistry
from ..generation.routing import (
    AuthenticatedCallerContext,
    ExecutionCandidate,
    ExecutionPolicy,
    GenerationRouter,
    TrustedRoutingConfig,
)
from ..generation.signals import RouteHealth, RouteSignal, RuntimeSignalRegistry


def trusted_routing_config_from_app() -> TrustedRoutingConfig:
    """Read operator-owned configuration; request payloads never enter this policy."""
    return TrustedRoutingConfig(
        allowed_providers=app_config.allowed_generation_providers,
        allowed_billing_pools=app_config.allowed_generation_billing_pools,
        allowed_credential_kinds=app_config.allowed_generation_credential_kinds,
        allow_cross_billing_fallback=app_config.allow_cross_billing_fallback,
    )


async def authenticated_caller_from_api_key(auth: AuthContext, repositories) -> AuthenticatedCallerContext:
    """Translate existing and generic API-key assignments into provider account IDs."""
    accounts = await repositories.provider_accounts.list(enabled_only=True)
    if auth.is_legacy:
        allowed = frozenset(account.id for account in accounts)
    else:
        allowed_ids = set(await repositories.provider_accounts.list_ids_for_api_key(auth.key_id)) if auth.key_id else set()
        allowed_ids.update(
            account.id
            for account in accounts
            if account.legacy_source == "tokens"
            and account.legacy_id is not None
            and int(account.legacy_id) in auth.allowed_accounts
        )
        allowed = frozenset(allowed_ids)
    return AuthenticatedCallerContext(
        api_key_id=auth.key_id,
        allowed_provider_account_ids=allowed,
        scopes=frozenset(auth.scopes),
    )


class PersistentGenerationRouter:
    def __init__(
        self,
        registry: ModelRegistry,
        router: GenerationRouter,
        repositories,
        signals: RuntimeSignalRegistry,
        worker_runtime=None,
    ):
        self.registry = registry
        self.router = router
        self.repositories = repositories
        self.signals = signals
        self.worker_runtime = worker_runtime

    async def resolve(
        self,
        *,
        requested_model: str,
        request_id: str,
        config: TrustedRoutingConfig,
        caller: AuthenticatedCallerContext,
    ):
        descriptor = self.registry.resolve(requested_model)
        policy = ExecutionPolicy.from_trusted_context(descriptor, config, caller)
        candidates = await self._candidates(descriptor)
        return self.router.resolve(
            requested_model=requested_model,
            request_id=request_id,
            policy=policy,
            candidates=candidates,
        )

    async def _candidates(self, descriptor) -> tuple[ExecutionCandidate, ...]:
        accounts = await self.repositories.provider_accounts.list(
            provider_key=descriptor.provider_id,
            enabled_only=True,
        )
        workers = {worker.id: worker for worker in await self.repositories.workers.list_devices()}
        candidates: list[ExecutionCandidate] = []
        for account in accounts:
            bindings = await self.repositories.credential_bindings.list_metadata(account.id)
            if account.legacy_source == "tokens" and "session_token" in descriptor.credential_kinds:
                candidates.append(self._candidate(descriptor, account.id, None, "session_token", descriptor.capability))
            for binding in bindings:
                if not binding.enabled or self._expired(binding.expires_at):
                    continue
                worker = workers.get(binding.worker_id) if binding.worker_id else None
                if binding.worker_id and (
                    worker is None or not worker.enabled or worker.revoked_at is not None
                ):
                    continue
                if descriptor.execution_location == "local-worker" and worker is None:
                    continue
                credential_kind = self._credential_kind(binding.credential_type, binding.storage_kind.value)
                capabilities = (
                    frozenset(worker.approved_capabilities)
                    if worker is not None
                    else frozenset({descriptor.capability})
                )
                candidates.append(
                    self._candidate(
                        descriptor,
                        account.id,
                        binding.worker_id,
                        credential_kind,
                        descriptor.capability,
                        capabilities=capabilities,
                        billing_pool=str(account.metadata.get("billing_pool") or descriptor.billing_pool),
                    )
                )
        return tuple(candidates)

    def _candidate(
        self,
        descriptor,
        account_id: str,
        worker_id: str | None,
        credential_kind: str,
        capability: str,
        *,
        capabilities: frozenset[str] | None = None,
        billing_pool: str | None = None,
    ) -> ExecutionCandidate:
        signal = self.signals.get(descriptor.provider_id, account_id, worker_id)
        if worker_id and self.worker_runtime is not None and not self.worker_runtime.is_connected(worker_id):
            signal = RouteSignal(
                health=RouteHealth.UNAVAILABLE,
                quota_remaining=signal.quota_remaining,
                available_slots=signal.available_slots,
            )
        return ExecutionCandidate(
            provider_id=descriptor.provider_id,
            provider_account_id=account_id,
            worker_id=worker_id,
            credential_kind=credential_kind,
            billing_pool=billing_pool or descriptor.billing_pool,
            capabilities=capabilities or frozenset({capability}),
            signal=signal,
        )

    @staticmethod
    def _credential_kind(credential_type: str, storage_kind: str) -> str:
        normalized = credential_type.strip().lower().replace("-", "_")
        if storage_kind == "browser_session" or normalized in {"browser_profile", "chatgpt_browser_profile"}:
            return "browser_session"
        return normalized

    @staticmethod
    def _expired(expires_at: str | None) -> bool:
        if not expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry <= datetime.now(timezone.utc)
