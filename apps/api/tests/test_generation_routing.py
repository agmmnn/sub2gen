from __future__ import annotations

from dataclasses import replace

import pytest

from sub2gen.core.database import Database
from sub2gen.core.api_key_manager import AuthContext
from sub2gen.generation.catalog import ModelRegistry
from sub2gen.generation.routing import (
    AuthenticatedCallerContext,
    ExecutionCandidate,
    ExecutionPolicy,
    GenerationRouter,
    RoutingError,
    TrustedRoutingConfig,
)
from sub2gen.generation.signals import RouteHealth, RouteSignal, RuntimeSignalRegistry
from sub2gen.persistence import (
    CredentialBindingRecord,
    CredentialStorageKind,
    ProviderAccountRecord,
    Repositories,
    WorkerDeviceRecord,
)
from sub2gen.services.generation_routing import (
    PersistentGenerationRouter,
    authenticated_caller_from_api_key,
)


def registry() -> ModelRegistry:
    return ModelRegistry.for_platform(
        {
            "gemini-image-landscape": {"type": "image"},
            "veo-video-landscape": {"type": "video"},
        }
    )


def policy_for(model: str, *, accounts=frozenset({"account-a", "account-b"}), cross_billing=False):
    descriptor = registry().resolve(model)
    return ExecutionPolicy.from_trusted_context(
        descriptor,
        TrustedRoutingConfig(
            allowed_providers=frozenset({descriptor.provider_id}),
            allowed_billing_pools=frozenset({descriptor.billing_pool}),
            allowed_credential_kinds=descriptor.credential_kinds,
            allow_cross_billing_fallback=cross_billing,
        ),
        AuthenticatedCallerContext(api_key_id=7, allowed_provider_account_ids=accounts),
    )


def chatgpt_candidate(account="account-a", worker="worker-a", **signal):
    return ExecutionCandidate(
        provider_id="chatgpt-web",
        provider_account_id=account,
        worker_id=worker,
        credential_kind="browser_session",
        billing_pool="chatgpt:web-subscription",
        capabilities=frozenset({"image.generate:chatgpt-web"}),
        signal=RouteSignal(**signal),
    )


def test_namespaced_catalog_keeps_existing_flow_id_as_an_alias():
    models = registry()

    legacy = models.resolve("gemini-image-landscape")
    canonical = models.resolve("google-flow/gemini-image-landscape")

    assert legacy is canonical
    assert canonical.resolved_model == "gemini-image-landscape"
    assert canonical.model_id == "google-flow/gemini-image-landscape"


def test_routing_is_deterministic_and_respects_live_concurrency():
    router = GenerationRouter(registry())
    candidates = (
        chatgpt_candidate("account-a", "worker-a", available_slots=1),
        chatgpt_candidate("account-b", "worker-b", available_slots=1),
    )

    first = router.resolve(
        requested_model="chatgpt/gpt-image-web",
        request_id="request-42",
        policy=policy_for("chatgpt/gpt-image-web"),
        candidates=candidates,
    )
    second = router.resolve(
        requested_model="chatgpt/gpt-image-web",
        request_id="request-42",
        policy=policy_for("chatgpt/gpt-image-web"),
        candidates=tuple(reversed(candidates)),
    )
    assert first == second

    saturated = tuple(
        replace(candidate, signal=RouteSignal(available_slots=0))
        if candidate.worker_id == first.worker_id
        else candidate
        for candidate in candidates
    )
    rerouted = router.resolve(
        requested_model="chatgpt/gpt-image-web",
        request_id="request-42",
        policy=policy_for("chatgpt/gpt-image-web"),
        candidates=saturated,
    )
    assert rerouted.worker_id != first.worker_id


@pytest.mark.parametrize(
    "candidate",
    [
        chatgpt_candidate(account="foreign"),
        replace(chatgpt_candidate(), credential_kind="oauth"),
        replace(chatgpt_candidate(), capabilities=frozenset({"captcha.solve"})),
        replace(chatgpt_candidate(), billing_pool="chatgpt:codex-subscription"),
        chatgpt_candidate(health=RouteHealth.UNAVAILABLE),
        chatgpt_candidate(quota_remaining=0),
        chatgpt_candidate(available_slots=0),
    ],
)
def test_constraints_are_enforced_before_dispatch(candidate):
    with pytest.raises(RoutingError, match="no eligible"):
        GenerationRouter(registry()).resolve(
            requested_model="chatgpt/gpt-image-web",
            request_id="request",
            policy=policy_for("chatgpt/gpt-image-web", accounts=frozenset({"account-a"})),
            candidates=(candidate,),
        )


def test_chatgpt_web_never_silently_routes_to_codex():
    codex = replace(
        chatgpt_candidate(),
        provider_id="chatgpt-codex",
        credential_kind="oauth",
        billing_pool="chatgpt:codex-subscription",
        capabilities=frozenset({"image.generate:chatgpt-codex"}),
    )
    with pytest.raises(RoutingError, match="no eligible"):
        GenerationRouter(registry()).resolve(
            requested_model="chatgpt/gpt-image-web",
            request_id="request",
            policy=policy_for("chatgpt/gpt-image-web"),
            candidates=(codex,),
        )


def test_provider_signals_do_not_poison_other_providers():
    signals = RuntimeSignalRegistry()
    signals.update(
        provider_id="chatgpt-web",
        provider_account_id="account-a",
        worker_id="worker-a",
        signal=RouteSignal(health=RouteHealth.UNAVAILABLE, quota_remaining=0),
    )

    assert signals.get("chatgpt-web", "account-a", "worker-a").health is RouteHealth.UNAVAILABLE
    assert signals.get("google-flow", "account-a", None) == RouteSignal()


async def test_persistent_router_builds_targets_from_accounts_bindings_and_workers(tmp_path):
    database = Database(str(tmp_path / "routing.db"))
    await database.init_db()
    repositories = Repositories.from_database(database)
    account = await repositories.provider_accounts.create(
        ProviderAccountRecord(id="account-a", provider_key="chatgpt-web", label="ChatGPT")
    )
    worker = await repositories.workers.register_device(
        WorkerDeviceRecord(
            id="worker-a",
            kind="image-worker",
            label="Mac",
            approved_capabilities=("image.generate:chatgpt-web",),
        )
    )
    await repositories.credential_bindings.create(
        CredentialBindingRecord(
            provider_account_id=account.id,
            worker_id=worker.id,
            binding_key="browser",
            credential_type="chatgpt-browser-profile",
            storage_kind=CredentialStorageKind.BROWSER_SESSION,
            secret_ref="browser-session://profile/default",
        )
    )
    api_key_id = await repositories.api_keys.create_client_api_key(
        client_name="test",
        label="test",
        key_prefix="s2g_test",
        key_plaintext=None,
        key_hash="1" * 64,
        scopes="generation:create",
        account_ids=[],
        endpoint_limits={},
        expires_at=None,
    )
    await repositories.provider_accounts.assign_api_key(account.id, api_key_id)
    models = registry()
    router = PersistentGenerationRouter(models, GenerationRouter(models), repositories, RuntimeSignalRegistry())
    config = TrustedRoutingConfig(
        allowed_providers=frozenset({"chatgpt-web"}),
        allowed_billing_pools=frozenset({"chatgpt:web-subscription"}),
        allowed_credential_kinds=frozenset({"browser_session"}),
    )

    caller = await authenticated_caller_from_api_key(
        AuthContext(
            key_id=api_key_id,
            key_label="test",
            is_legacy=False,
            allowed_accounts=set(),
            scopes={"generation:create"},
        ),
        repositories,
    )
    resolution = await router.resolve(
        requested_model="chatgpt/gpt-image-web",
        request_id="request",
        config=config,
        caller=caller,
    )

    assert resolution.provider_account_id == account.id
    assert resolution.worker_id == worker.id
    assert resolution.billing_pool == "chatgpt:web-subscription"
    assert caller.allowed_provider_account_ids == frozenset({account.id})
