from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from sub2gen.bootstrap.container import build_container
from sub2gen.core.database import Database
from sub2gen.persistence import (
    CredentialBindingRecord,
    CredentialStorageKind,
    GenerationJobRecord,
    ProviderAccountRecord,
    WorkerDeviceRecord,
)
from sub2gen.transport.admin.control_plane import build_control_plane_router


@pytest.mark.asyncio
async def test_control_plane_is_secret_free_and_mutations_are_audited(tmp_path) -> None:
    database = Database(str(tmp_path / "control-plane.db"))
    await database.init_db()
    container = build_container(database=database)
    account = await container.repositories.provider_accounts.create(
        ProviderAccountRecord(
            id="pa_chatgpt",
            provider_key="chatgpt-web",
            label="Personal ChatGPT",
            metadata={"billing_pool": "chatgpt:web-subscription", "secret_note": "do-not-return"},
        )
    )
    worker = await container.repositories.workers.register_device(
        WorkerDeviceRecord(
            id="worker_mac",
            kind="image-worker",
            label="Mac",
            approved_capabilities=("image.generate:chatgpt-web",),
            auth_key_hash="raw-auth-hash",
            metadata={"login_state": "ready", "profile_path": "/private/profile"},
        )
    )
    await container.repositories.credential_bindings.create(
        CredentialBindingRecord(
            id="binding_browser",
            provider_account_id=account.id,
            worker_id=worker.id,
            binding_key="default",
            credential_type="chatgpt-browser-profile",
            storage_kind=CredentialStorageKind.BROWSER_SESSION,
            secret_ref="browser-session://private-profile",
        )
    )
    await container.repositories.generation_jobs.create(
        GenerationJobRecord(
            id="job_one",
            request_id="request-one",
            job_kind="image.generate",
            requested_model="chatgpt/gpt-image-web",
        )
    )

    async def admin() -> str:
        return "test-admin"

    app = FastAPI()
    app.state.container = container
    app.include_router(build_control_plane_router(admin))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        overview = await client.get("/api/admin/control-plane/overview")
        assert overview.status_code == 200
        body = overview.json()
        serialized = overview.text
        assert body["accounts"][0]["credential_locations"] == ["browser_session"]
        assert body["accounts"][0]["bindings"][0]["worker_id"] == worker.id
        assert body["jobs"][0]["requested_model"] == "chatgpt/gpt-image-web"
        assert "browser-session://private-profile" not in serialized
        assert "raw-auth-hash" not in serialized
        assert "/private/profile" not in serialized
        assert "do-not-return" not in serialized

        paused = await client.patch(f"/api/admin/control-plane/accounts/{account.id}", json={"enabled": False})
        assert paused.status_code == 200
        capabilities = await client.patch(
            f"/api/admin/control-plane/workers/{worker.id}",
            json={"capabilities": ["image.generate:chatgpt-web", "captcha.solve"]},
        )
        assert capabilities.status_code == 200
        wrong_confirmation = await client.request(
            "DELETE", f"/api/admin/control-plane/workers/{worker.id}", json={"confirm": "wrong"}
        )
        assert wrong_confirmation.status_code == 400
        revoked = await client.request(
            "DELETE", f"/api/admin/control-plane/workers/{worker.id}", json={"confirm": worker.id}
        )
        assert revoked.status_code == 200

    events = await container.repositories.operator_audit.list_recent()
    assert [event["action"] for event in events] == [
        "worker.revoked",
        "worker.updated",
        "account.paused",
    ]
