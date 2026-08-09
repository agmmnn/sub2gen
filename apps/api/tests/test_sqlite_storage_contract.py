from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from sub2gen.core.database import Database
from sub2gen.core.models import Project, Token


CONTRACT = json.loads(
    (Path(__file__).parent / "contracts" / "storage-state.json").read_text(encoding="utf-8")
)


@pytest.mark.asyncio
async def test_sqlite_repository_state_contract(tmp_path: Path) -> None:
    database = Database(str(tmp_path / "sub2gen.db"))
    await database.init_db()
    try:
        token_id = await database.add_token(
            Token(st=f"st-{uuid.uuid4().hex}", email=CONTRACT["token"]["email"])
        )
        await database.update_token(
            token_id,
            is_active=CONTRACT["token"]["is_active"],
            credits=CONTRACT["token"]["credits"],
        )

        project_id = f"project-{uuid.uuid4().hex}"
        await database.add_project(
            Project(
                project_id=project_id,
                token_id=token_id,
                project_name=CONTRACT["project"]["name"],
            )
        )

        key_id = await database.create_client_api_key(
            client_name="storage-contract-client",
            label="contract-key",
            key_prefix="f2_contract",
            key_plaintext=None,
            key_hash=uuid.uuid4().hex,
            scopes="*",
            account_ids=[token_id],
            endpoint_limits={},
            expires_at=None,
        )
        await database.upsert_cache_file(
            filename="storage-contract.bin",
            api_key_id=key_id,
            token_id=token_id,
            media_type=CONTRACT["cache"]["media_type"],
            source_url="https://example.com/source",
            flow_project_id=project_id,
            size_bytes=CONTRACT["cache"]["size_bytes"],
        )
        await database.upsert_extension_worker_binding("contract-route", key_id)

        token = await database.get_token(token_id)
        project = await database.get_project_by_id(project_id)
        key = await database.get_api_key_detail(key_id)
        cache = await database.get_cache_file_for_api_key("storage-contract.bin", key_id)
        binding = await database.get_extension_worker_binding_for_route_key("contract-route")

        state = {
            "token": {
                "email": token.email if token else None,
                "is_active": token.is_active if token else None,
                "credits": token.credits if token else None,
            },
            "project": {
                "name": project.project_name if project else None,
                "token_matches": bool(project and project.token_id == token_id),
            },
            "managed_key": {
                "is_active": key["is_active"] if key else None,
                "account_count": len(key["account_ids"]) if key else None,
            },
            "cache": {
                "media_type": cache["media_type"] if cache else None,
                "size_bytes": cache["size_bytes"] if cache else None,
            },
            "binding": {
                "api_key_matches": bool(binding and binding["api_key_id"] == key_id),
            },
        }
        assert state == CONTRACT
    finally:
        await database.close_runtime_connections()
