from __future__ import annotations

import uuid

import pytest

from sub2gen.core.database import Database
from sub2gen.core.models import Project, RequestLog, Token
from sub2gen.persistence.repositories import Repositories


@pytest.mark.asyncio
async def test_sqlite_repositories_cover_capability_boundaries(tmp_path) -> None:
    database = Database(str(tmp_path / "repositories.db"))
    await database.init_db()
    repositories = Repositories.from_database(database)

    token_id = await repositories.accounts.add_token(Token(st=f"st-{uuid.uuid4().hex}", email="repository@example.com"))
    token = await repositories.accounts.get_token(token_id)
    assert token is not None and token.email == "repository@example.com"

    project_id = f"project-{uuid.uuid4().hex}"
    await repositories.projects.add_project(
        Project(project_id=project_id, token_id=token_id, project_name="Repository")
    )
    project = await repositories.projects.get_project_by_id(project_id)
    assert project is not None and project.token_id == token_id

    key_id = await repositories.api_keys.create_client_api_key(
        client_name="repository-client",
        label="repository-key",
        key_prefix="f2_repo",
        key_plaintext=None,
        key_hash=uuid.uuid4().hex,
        scopes="*",
        account_ids=[token_id],
        endpoint_limits={},
        expires_at=None,
    )
    assert await repositories.api_keys.get_api_key_account_ids(key_id) == [token_id]

    log_id = await repositories.request_logs.add_request_log(
        RequestLog(
            token_id=token_id,
            api_key_id=key_id,
            operation="repository-contract",
            status_code=200,
            duration=0.01,
        )
    )
    detail = await repositories.request_logs.get_log_detail(log_id)
    assert detail is not None and detail["operation"] == "repository-contract"

    await repositories.cache.upsert_cache_file(
        filename="repository.bin",
        api_key_id=key_id,
        token_id=token_id,
        media_type="image",
        source_url="https://example.invalid/source",
        flow_project_id=project_id,
        size_bytes=42,
    )
    cached = await repositories.cache.get_cache_file_for_api_key("repository.bin", key_id)
    assert cached is not None and cached["size_bytes"] == 42

    await repositories.workers.upsert_extension_worker_binding("repository-route", key_id)
    binding = await repositories.workers.get_extension_worker_binding_for_route_key("repository-route")
    assert binding is not None and binding["api_key_id"] == key_id


def test_repository_set_uses_one_backend_instance() -> None:
    database = object()
    repositories = Repositories.from_database(database)

    assert {
        id(repositories.accounts.database),
        id(repositories.projects.database),
        id(repositories.api_keys.database),
        id(repositories.cache.database),
        id(repositories.request_logs.database),
        id(repositories.workers.database),
    } == {id(database)}
