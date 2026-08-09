import asyncio
import json
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest
from fastapi import HTTPException

from sub2gen.api import routes
from sub2gen.core.api_key_manager import AuthContext


def make_auth(*, accounts=(7,), scopes=("generate:chat", "projects:read")) -> AuthContext:
    return AuthContext(
        key_id=42,
        key_label="test-key",
        is_legacy=False,
        allowed_accounts=set(accounts),
        scopes=set(scopes),
    )


def test_project_pin_resolves_to_owning_assigned_account():
    db = SimpleNamespace(
        get_project_by_id=AsyncMock(
            return_value=SimpleNamespace(project_id="project-one", token_id=7, is_active=True)
        )
    )
    handler = SimpleNamespace(db=db)

    token_ids, project_id = asyncio.run(
        routes._resolve_project_pin(" project-one ", make_auth(), handler)
    )

    assert token_ids == {7}
    assert project_id == "project-one"
    db.get_project_by_id.assert_awaited_once_with("project-one", 42)


def test_project_pin_rejects_project_from_unassigned_account():
    db = SimpleNamespace(
        get_project_by_id=AsyncMock(
            return_value=SimpleNamespace(project_id="project-two", token_id=8, is_active=True)
        )
    )
    handler = SimpleNamespace(db=db)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes._resolve_project_pin("project-two", make_auth(), handler))

    assert exc_info.value.status_code == 400
    assert "not assigned" in str(exc_info.value.detail)


def test_generation_target_uses_pin_without_automatic_selection():
    with (
        patch.object(
            routes,
            "_resolve_project_pin",
            AsyncMock(return_value=({7}, "project-one")),
        ) as resolve_pin,
        patch.object(
            routes,
            "_select_random_active_project_for_api_key",
            AsyncMock(),
        ) as select_automatic,
    ):
        result = asyncio.run(
            routes._select_generation_target(
                make_auth(), "gemini-3.0-pro-image-landscape", "project-one", object()
            )
        )

    assert result == ({7}, "project-one")
    resolve_pin.assert_awaited_once()
    select_automatic.assert_not_awaited()


def test_generation_target_keeps_automatic_fallback():
    with (
        patch.object(
            routes,
            "_resolve_project_pin",
            AsyncMock(return_value=(None, None)),
        ),
        patch.object(
            routes,
            "_select_random_active_project_for_api_key",
            AsyncMock(return_value=({9}, None)),
        ) as select_automatic,
    ):
        result = asyncio.run(
            routes._select_generation_target(
                make_auth(), "gemini-3.0-pro-image-landscape", None, object()
            )
        )

    assert result == ({9}, None)
    select_automatic.assert_awaited_once_with(
        make_auth(), "gemini-3.0-pro-image-landscape", ANY
    )


def test_project_id_is_added_to_json_and_stream_responses():
    payload = routes._with_projectid({"choices": []}, "project-one")
    assert payload["project_id"] == "project-one"

    chunk = routes._inject_projectid_into_openai_sse_chunk(
        'data: {"choices": []}\n\n', "project-one"
    )
    decoded = json.loads(chunk.removeprefix("data: ").strip())
    assert decoded["project_id"] == "project-one"


def test_project_listing_requires_read_scope():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            routes.list_flow_projects(
                auth_ctx=make_auth(scopes=("generate:chat",)),
            )
        )

    assert exc_info.value.status_code == 403
    assert "projects:read" in str(exc_info.value.detail)
