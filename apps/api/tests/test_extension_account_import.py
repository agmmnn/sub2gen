import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from sub2gen.api import routes
from sub2gen.core.api_key_manager import AuthContext
from sub2gen.main import _path_allowed_on_api_only_host


def make_auth(*, key_id=42, scopes=("tokens:import",), legacy=False):
    return AuthContext(
        key_id=None if legacy else key_id,
        key_label="extension-test",
        is_legacy=legacy,
        allowed_accounts=set(),
        scopes={"*"} if legacy else set(scopes),
    )


def import_body():
    return routes.ExtensionAccountImportRequest(
        session_token="session-token",
        google_cookies=json.dumps(
            [
                {
                    "name": "SID",
                    "value": "sid-value",
                    "domain": ".google.com",
                    "path": "/",
                },
                {
                    "name": "SAPISID",
                    "value": "sapisid-value",
                    "domain": ".google.com",
                    "path": "/",
                },
            ]
        ),
        refresh_interval_minutes=60,
    )


def test_account_import_requires_explicit_managed_scope():
    with pytest.raises(HTTPException) as exc_info:
        routes._require_token_import_scope(make_auth(scopes=("generate:chat",)))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Missing scope: tokens:import"


def test_account_import_updates_and_assigns_existing_account():
    existing = SimpleNamespace(id=7, email="user@example.com")
    database = SimpleNamespace(
        get_token_by_email=AsyncMock(return_value=existing),
        update_token=AsyncMock(),
        get_api_key_account_ids=AsyncMock(return_value=[]),
        update_api_key=AsyncMock(),
    )
    token_manager = SimpleNamespace(
        db=database,
        flow_client=SimpleNamespace(
            st_to_at=AsyncMock(
                return_value={
                    "access_token": "access-token",
                    "expires": "2099-01-01T00:00:00Z",
                    "user": {"email": "user@example.com"},
                }
            )
        ),
        update_token=AsyncMock(),
        add_token=AsyncMock(),
    )
    key_manager = SimpleNamespace(invalidate=AsyncMock())

    container = SimpleNamespace(token_manager=token_manager, api_key_manager=key_manager)
    result = asyncio.run(
        routes.extension_import_current_account(import_body(), make_auth(), container)
    )

    assert result["success"] is True
    assert result["added"] == 0
    assert result["updated"] == 1
    assert result["token_id"] == 7
    token_manager.update_token.assert_awaited_once()
    update_kwargs = token_manager.update_token.await_args.kwargs
    assert update_kwargs["protocol_mode"] == "protocol"
    assert update_kwargs["auto_refresh_enabled"] is True
    assert update_kwargs["refresh_interval_minutes"] == 60
    assert json.loads(update_kwargs["google_cookies"])[0]["name"] == "SID"
    database.update_api_key.assert_awaited_once_with(42, account_ids=[7])
    key_manager.invalidate.assert_awaited_once_with(42)
    token_manager.add_token.assert_not_awaited()


def test_account_import_adds_new_account_for_legacy_key():
    database = SimpleNamespace(
        get_token_by_email=AsyncMock(return_value=None),
        update_token=AsyncMock(),
        get_api_key_account_ids=AsyncMock(),
        update_api_key=AsyncMock(),
    )
    token_manager = SimpleNamespace(
        db=database,
        flow_client=SimpleNamespace(
            st_to_at=AsyncMock(
                return_value={
                    "access_token": "access-token",
                    "expires": "2099-01-01T00:00:00Z",
                    "user": {"email": "new@example.com"},
                }
            )
        ),
        update_token=AsyncMock(),
        add_token=AsyncMock(return_value=SimpleNamespace(id=9, email="new@example.com")),
    )

    container = SimpleNamespace(
        token_manager=token_manager,
        api_key_manager=SimpleNamespace(invalidate=AsyncMock()),
    )
    result = asyncio.run(
        routes.extension_import_current_account(
            import_body(),
            make_auth(legacy=True),
            container,
        )
    )

    assert result["added"] == 1
    assert result["updated"] == 0
    assert result["token_id"] == 9
    token_manager.add_token.assert_awaited_once()
    database.update_api_key.assert_not_awaited()


def test_extension_import_is_available_on_api_only_host():
    assert _path_allowed_on_api_only_host("/api/extension/import-current-account")
