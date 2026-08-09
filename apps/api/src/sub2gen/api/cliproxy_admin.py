"""Admin-only sub2gen facade for an external CLIProxyAPI service."""

from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter, File, Form, Query, UploadFile

from ..core.cliproxy_models import (
    CLIProxyAccount,
    CLIProxyAlias,
    CLIProxyAliasUpdate,
    CLIProxyApiKeyImport,
    CLIProxyConnectivityTest,
    CLIProxyCredentialImportResponse,
    CLIProxyExclusionUpdate,
    CLIProxyLogs,
    CLIProxyModel,
    CLIProxyOAuthSession,
    CLIProxyOverview,
    CLIProxyPlatform,
    CLIProxyQuotaReset,
    CLIProxyRouting,
    CLIProxyRoutingUpdate,
    CLIProxyStatusUpdate,
)
from ..services.cliproxy_client import (
    CLIProxyInferenceClient,
    CLIProxyManagementClient,
    CLIProxyUpstreamError,
    management_error_to_http,
)
from ..core.config import config


router = APIRouter(tags=["CLIProxy AI Gateway"])


async def _management_call(awaitable):
    try:
        return await awaitable
    except CLIProxyUpstreamError as exc:
        raise management_error_to_http(exc) from exc


@router.get("/status", response_model=CLIProxyOverview)
async def cliproxy_status() -> CLIProxyOverview:
    return await CLIProxyManagementClient().overview()


@router.get("/accounts", response_model=List[CLIProxyAccount])
async def cliproxy_accounts(include_models: bool = Query(False)) -> List[CLIProxyAccount]:
    return await _management_call(
        CLIProxyManagementClient().list_accounts(include_models=include_models)
    )


@router.patch("/accounts/{name}/status")
async def cliproxy_account_status(name: str, request: CLIProxyStatusUpdate):
    result = await _management_call(
        CLIProxyManagementClient().set_account_enabled(name, request.enabled)
    )
    return {"success": True, "enabled": request.enabled, "result": result}


@router.delete("/accounts/{name}")
async def cliproxy_delete_account(name: str):
    result = await _management_call(CLIProxyManagementClient().delete_account(name))
    return {"success": True, "result": result}


@router.post("/accounts/import", response_model=CLIProxyCredentialImportResponse)
async def cliproxy_import_credential(
    platform: str = Form(...),
    file: UploadFile = File(...),
    location: str = Form("us-central1"),
):
    content = await file.read(2 * 1024 * 1024 + 1)
    return await _management_call(
        CLIProxyManagementClient().import_credential_file(
            platform=platform,
            filename=file.filename or "credential.json",
            content=content,
            location=location,
        )
    )


@router.post("/accounts/api-key")
async def cliproxy_import_api_key(request: CLIProxyApiKeyImport):
    result = await _management_call(CLIProxyManagementClient().import_api_key(request))
    return {"success": True, "platform": request.provider, "result": result}


@router.post("/oauth/{provider}/start", response_model=CLIProxyOAuthSession)
async def cliproxy_oauth_start(provider: str) -> CLIProxyOAuthSession:
    # Start browser flows through the public HTTPS origin so CLIProxy builds
    # callback/forwarder URLs with the externally reachable Railway host.
    oauth_base_url = config.cliproxy_public_url or config.cliproxy_base_url
    return await _management_call(
        CLIProxyManagementClient(base_url=oauth_base_url).start_oauth(provider)
    )


@router.get("/oauth/status", response_model=CLIProxyOAuthSession)
async def cliproxy_oauth_status(
    state: str = Query(...), provider: str = Query("")
) -> CLIProxyOAuthSession:
    return await _management_call(CLIProxyManagementClient().oauth_status(state, provider))


@router.delete("/oauth/session")
async def cliproxy_oauth_cancel(state: str = Query(...)):
    result = await _management_call(CLIProxyManagementClient().cancel_oauth(state))
    return {"success": True, "result": result}


@router.get("/platforms", response_model=List[CLIProxyPlatform])
async def cliproxy_platforms() -> List[CLIProxyPlatform]:
    return await _management_call(CLIProxyManagementClient().platforms())


@router.get("/models", response_model=List[CLIProxyModel])
async def cliproxy_models(platform: str = Query("")) -> List[CLIProxyModel]:
    client = CLIProxyManagementClient()
    if platform:
        return await _management_call(client.model_definitions(platform))
    platforms = await _management_call(client.platforms())
    seen: set[str] = set()
    models: List[CLIProxyModel] = []
    for item in platforms:
        for model in item.models:
            if model.id not in seen:
                seen.add(model.id)
                models.append(model)
    return models


@router.post("/models/test")
async def cliproxy_test_model(request: CLIProxyConnectivityTest):
    return await CLIProxyInferenceClient().connectivity_test(request.model)


@router.get("/aliases", response_model=Dict[str, List[CLIProxyAlias]])
async def cliproxy_aliases() -> Dict[str, List[CLIProxyAlias]]:
    return await _management_call(CLIProxyManagementClient().list_aliases())


@router.patch("/aliases/{platform}")
async def cliproxy_replace_aliases(platform: str, request: CLIProxyAliasUpdate):
    result = await _management_call(
        CLIProxyManagementClient().replace_aliases(platform, request.aliases)
    )
    return {"success": True, "result": result}


@router.delete("/aliases/{platform}")
async def cliproxy_delete_aliases(platform: str):
    result = await _management_call(CLIProxyManagementClient().delete_aliases(platform))
    return {"success": True, "result": result}


@router.get("/exclusions", response_model=Dict[str, List[str]])
async def cliproxy_exclusions() -> Dict[str, List[str]]:
    return await _management_call(CLIProxyManagementClient().exclusions())


@router.patch("/exclusions/{platform}")
async def cliproxy_replace_exclusions(platform: str, request: CLIProxyExclusionUpdate):
    result = await _management_call(
        CLIProxyManagementClient().replace_exclusions(platform, request.models)
    )
    return {"success": True, "result": result}


@router.delete("/exclusions/{platform}")
async def cliproxy_delete_exclusions(platform: str):
    result = await _management_call(CLIProxyManagementClient().delete_exclusions(platform))
    return {"success": True, "result": result}


@router.get("/routing", response_model=CLIProxyRouting)
async def cliproxy_routing() -> CLIProxyRouting:
    return await _management_call(CLIProxyManagementClient().routing())


@router.patch("/routing", response_model=CLIProxyRouting)
async def cliproxy_update_routing(request: CLIProxyRoutingUpdate) -> CLIProxyRouting:
    return await _management_call(
        CLIProxyManagementClient().set_routing(
            request.strategy,
            request.retry_count,
            request.max_retry_interval_seconds,
        )
    )


@router.post("/routing/reset-quota")
async def cliproxy_reset_quota(request: CLIProxyQuotaReset):
    result = await _management_call(
        CLIProxyManagementClient().reset_quota(request.auth_index)
    )
    return {"success": True, "result": result}


@router.get("/logs", response_model=CLIProxyLogs)
async def cliproxy_logs(
    limit: int = Query(200, ge=1, le=1000), cursor: str = Query("")
) -> CLIProxyLogs:
    return await _management_call(CLIProxyManagementClient().logs(limit=limit, cursor=cursor))
