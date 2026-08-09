from __future__ import annotations

import sqlite3
import base64
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sub2gen.core.database import Database
from sub2gen.core.postgres_migrations import baseline_schema_signature, discover_migrations as discover_postgres
from sub2gen.persistence import (
    CredentialBindingRecord,
    CredentialResolutionError,
    CredentialResolver,
    CredentialStorageKind,
    GenerationAttemptRecord,
    GenerationAttemptStatus,
    GenerationJobRecord,
    GenerationJobStatus,
    LegacyAccountCatalog,
    ProviderAccountRecord,
    Repositories,
    WorkerDeviceRecord,
)
from sub2gen.persistence.migrations.sqlite import discover_sqlite_migrations
from sub2gen.services.worker_protocol import PersistentDevicePairing


@pytest.mark.asyncio
async def test_provider_accounts_credentials_workers_and_jobs_survive_reopen(tmp_path) -> None:
    path = tmp_path / "unified.db"
    database = Database(str(path))
    await database.init_db()
    repositories = Repositories.from_database(database)

    worker = await repositories.workers.register_device(
        WorkerDeviceRecord(
            kind="image-worker",
            label="Mac worker",
            approved_capabilities=("image.generate:chatgpt-web",),
            auth_key_hash="worker-auth-hash",
            public_key="device-public-key",
        )
    )
    account = await repositories.provider_accounts.create(
        ProviderAccountRecord(
            provider_key="chatgpt-web",
            label="Personal ChatGPT",
            metadata={"profile_label": "Default"},
        )
    )
    browser_binding = await repositories.credential_bindings.create(
        CredentialBindingRecord(
            provider_account_id=account.id,
            worker_id=worker.id,
            binding_key="browser",
            credential_type="chatgpt-browser-profile",
            storage_kind=CredentialStorageKind.BROWSER_SESSION,
            secret_ref="browser-session://profile/default",
        )
    )
    job = await repositories.generation_jobs.create(
        GenerationJobRecord(
            request_id="request-1",
            idempotency_key="idem-1",
            job_kind="image.generate",
            requested_model="chatgpt/gpt-image-web",
        )
    )
    resolved = {
        "requested_model": "chatgpt/gpt-image-web",
        "resolved_model": "chatgpt/gpt-image-web",
        "provider_id": "chatgpt-web",
        "provider_account_id": account.id,
        "worker_id": worker.id,
        "billing_pool": "chatgpt-subscription",
    }
    assert await repositories.generation_jobs.transition(
        job.id,
        expected=(GenerationJobStatus.QUEUED,),
        status=GenerationJobStatus.RUNNING,
        provider_account_id=account.id,
        worker_id=worker.id,
        resolved_execution=resolved,
    )
    attempt = await repositories.generation_attempts.create(
        GenerationAttemptRecord(
            job_id=job.id,
            attempt=1,
            status=GenerationAttemptStatus.RUNNING,
            lease_id="lease-1",
            resolved_execution=resolved,
        )
    )
    assert not await repositories.generation_attempts.finish(
        attempt.id,
        expected_lease_id="stale-lease",
        status=GenerationAttemptStatus.SUCCEEDED,
    )
    assert await repositories.generation_attempts.finish(
        attempt.id,
        expected_lease_id="lease-1",
        status=GenerationAttemptStatus.SUCCEEDED,
        provider_job_id="upstream-job",
    )
    assert await repositories.generation_jobs.transition(
        job.id,
        expected=(GenerationJobStatus.RUNNING,),
        status=GenerationJobStatus.SUCCEEDED,
        resolved_execution=resolved,
        terminal=True,
    )

    reopened = Repositories.from_database(Database(str(path)))
    restored_job = await reopened.generation_jobs.get_by_idempotency_key("idem-1")
    restored_attempts = await reopened.generation_attempts.list_for_job(job.id)
    binding_views = await reopened.credential_bindings.list_metadata(account.id)
    worker_views = await reopened.workers.list_devices()

    assert restored_job is not None and restored_job.status is GenerationJobStatus.SUCCEEDED
    assert restored_job.resolved_execution == resolved
    assert len(restored_attempts) == 1
    assert restored_attempts[0].provider_job_id == "upstream-job"
    assert binding_views[0].id == browser_binding.id
    assert not hasattr(binding_views[0], "secret_ref")
    assert worker_views[0].id == worker.id
    assert not hasattr(worker_views[0], "auth_key_hash")
    assert "browser-session" not in repr(binding_views[0])
    assert "worker-auth-hash" not in repr(worker_views[0])
    assert "browser-session://profile/default" not in repr(browser_binding)
    assert "worker-auth-hash" not in repr(worker)
    assert "device-public-key" not in repr(worker)


@pytest.mark.asyncio
async def test_credential_resolver_obeys_execution_location_and_records_validation(tmp_path) -> None:
    database = Database(str(tmp_path / "credentials.db"))
    await database.init_db()
    repositories = Repositories.from_database(database)
    account = await repositories.provider_accounts.create(
        ProviderAccountRecord(provider_key="test", label="Test")
    )
    env_binding = await repositories.credential_bindings.create(
        CredentialBindingRecord(
            provider_account_id=account.id,
            binding_key="server",
            credential_type="api-key",
            storage_kind=CredentialStorageKind.ENV,
            secret_ref="env://PROVIDER_TEST_KEY",
        )
    )
    worker_binding = await repositories.credential_bindings.create(
        CredentialBindingRecord(
            provider_account_id=account.id,
            binding_key="worker",
            credential_type="browser-profile",
            storage_kind=CredentialStorageKind.WORKER_VAULT,
            secret_ref="worker-vault://profile/default",
        )
    )
    resolver = CredentialResolver(
        repositories.credential_bindings,
        environment={"PROVIDER_TEST_KEY": "resolved-secret"},
    )

    resolved = await resolver.resolve_for_api_host(env_binding.id)
    assert resolved.secret == "resolved-secret"
    assert "resolved-secret" not in repr(resolved)
    assert "env://PROVIDER_TEST_KEY" not in repr(env_binding)
    with pytest.raises(CredentialResolutionError, match="assigned worker"):
        await resolver.resolve_for_api_host(worker_binding.id)

    views = await repositories.credential_bindings.list_metadata(account.id)
    assert all(not hasattr(view, "secret_ref") for view in views)


@pytest.mark.asyncio
async def test_legacy_account_catalog_describes_without_copying_credentials() -> None:
    fake = SimpleNamespace(
        get_all_tokens=lambda: None,
    )

    async def flow_accounts():
        return [
            SimpleNamespace(
                id=1,
                email="flow@example.com",
                name="Flow",
                is_active=True,
                user_paygate_tier="tier-one",
                auth_mode="session_token",
                st="must-not-copy",
            )
        ]

    async def runway_accounts():
        return [
            SimpleNamespace(
                id=2,
                label="Runway",
                workspace_id="workspace",
                team_id="team",
                concurrency_limit=1,
                is_active=True,
                raw_credential="must-not-copy",
            )
        ]

    async def geminigen_accounts():
        return [
            SimpleNamespace(
                id=3,
                label="GeminiGen",
                profile_email="gemini@example.com",
                profile_uuid="profile",
                plan_name="Max",
                available_credit=10,
                is_active=True,
                raw_cookie="must-not-copy",
            )
        ]

    fake.get_all_tokens = flow_accounts
    fake.list_runway_accounts = runway_accounts
    fake.list_geminigen_accounts = geminigen_accounts
    catalog = LegacyAccountCatalog(fake)

    records = [
        *(await catalog.describe_google_flow()),
        *(await catalog.describe_runway()),
        *(await catalog.describe_geminigen()),
    ]
    serialized = repr(records)

    assert [record.provider_key for record in records] == ["google-flow", "runway", "geminigen"]
    assert "must-not-copy" not in serialized


def test_sqlite_and_postgres_0003_define_the_same_provider_tables() -> None:
    postgres = next(migration for migration in discover_postgres() if migration.revision == "0003")
    assert postgres.revision == "0003"
    postgres_tables, _ = baseline_schema_signature(postgres)

    connection = sqlite3.connect(":memory:")
    try:
        for migration in discover_sqlite_migrations():
            connection.executescript(migration.sql_text)
        sqlite_tables = {
            str(row[0]): {
                str(column[1])
                for column in connection.execute(f'PRAGMA table_info("{row[0]}")').fetchall()
            }
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        connection.close()

    expected = {
        "provider_accounts",
        "credential_bindings",
        "worker_devices",
        "generation_jobs",
        "generation_attempts",
    }
    assert set(postgres_tables) == expected
    for table in expected:
        assert postgres_tables[table] == sqlite_tables[table]


@pytest.mark.asyncio
async def test_paired_device_auth_survives_service_restart_and_can_be_revoked(tmp_path) -> None:
    database = Database(str(tmp_path / "paired-worker.db"))
    await database.init_db()
    repositories = Repositories.from_database(database)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    pairing = PersistentDevicePairing(repositories.workers.devices)
    code = pairing.create_pairing_code()
    await pairing.pair(
        code=code,
        worker_id="durable-worker",
        kind="image-worker",
        label="Durable worker",
        public_key_base64=base64.b64encode(public_key).decode(),
        approved_capabilities=("image.generate:chatgpt-web",),
    )

    restarted = PersistentDevicePairing(repositories.workers.devices)
    challenge_id, _nonce, _expires = await restarted.issue_challenge("durable-worker")
    signature = base64.b64encode(private_key.sign(restarted.challenge_bytes(challenge_id))).decode()
    await restarted.authenticate(
        worker_id="durable-worker",
        challenge_id=challenge_id,
        signature=signature,
    )
    assert await restarted.revoke("durable-worker")
    with pytest.raises(PermissionError, match="unavailable"):
        await restarted.issue_challenge("durable-worker")
