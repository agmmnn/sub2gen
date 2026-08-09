from __future__ import annotations

from types import SimpleNamespace

import pytest

from sub2gen.core.database import Database
from sub2gen.persistence import CredentialBindingRecord, CredentialStorageKind, ProviderAccountRecord, Repositories, WorkerDeviceRecord
from sub2gen.services.canonical_browser_worker import CAPTCHA_CAPABILITY, CanonicalBrowserWorkerService


class FakeRuntime:
    def __init__(self) -> None:
        self.calls = []

    def is_connected(self, worker_id: str) -> bool:
        return worker_id == "worker-bound"

    async def dispatch(self, **values):
        self.calls.append(values)
        return SimpleNamespace(error=None, output={"token": "captcha-token", "user_agent": "Chrome/Test"})


@pytest.mark.asyncio
async def test_canonical_browser_worker_routes_only_to_bound_capable_device(tmp_path) -> None:
    database = Database(str(tmp_path / "canonical-worker.db"))
    await database.init_db()
    repositories = Repositories.from_database(database)
    account = await repositories.provider_accounts.create(
        ProviderAccountRecord(
            id="account-flow",
            provider_key="google-flow",
            label="Flow",
            legacy_source="tokens",
            legacy_id="42",
        )
    )
    await repositories.workers.register_device(
        WorkerDeviceRecord(
            id="worker-bound",
            kind="chrome-extension",
            label="Chrome",
            approved_capabilities=(CAPTCHA_CAPABILITY,),
        )
    )
    await repositories.credential_bindings.create(
        CredentialBindingRecord(
            provider_account_id=account.id,
            worker_id="worker-bound",
            binding_key="chrome",
            credential_type="browser_session",
            storage_kind=CredentialStorageKind.BROWSER_SESSION,
            secret_ref="worker://worker-bound",
        )
    )
    runtime = FakeRuntime()
    service = CanonicalBrowserWorkerService(runtime, repositories)

    token, request_id = await service.get_token(
        project_id="project",
        action="IMAGE_GENERATION",
        timeout=10,
        token_id=42,
        managed_api_key_id=None,
    )

    assert token == "captcha-token"
    assert request_id is not None
    assert service.consume_user_agent(request_id) == "Chrome/Test"
    assert runtime.calls[0]["worker_id"] == "worker-bound"
    assert runtime.calls[0]["capability"] == CAPTCHA_CAPABILITY
    with pytest.raises(RuntimeError, match="No protocol-v1 worker"):
        await service.get_token(
            project_id="project",
            action="IMAGE_GENERATION",
            timeout=10,
            token_id=999,
            managed_api_key_id=None,
        )
