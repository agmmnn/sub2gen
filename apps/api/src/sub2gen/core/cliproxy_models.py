"""Typed, secret-free models used by the CLIProxy control plane."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CLIProxyModel(BaseModel):
    id: str
    raw_id: str
    platform: str
    display_name: str = ""
    owned_by: str = ""
    capabilities: List[str] = Field(default_factory=list)
    excluded: bool = False


class CLIProxyAccount(BaseModel):
    id: str
    auth_index: str = ""
    name: str
    platform: str
    label: str = ""
    email: str = ""
    account_type: str = ""
    status: str = "unknown"
    status_message: str = ""
    disabled: bool = False
    unavailable: bool = False
    runtime_only: bool = False
    source: str = ""
    last_refresh: Optional[str] = None
    success_count: int = 0
    failure_count: int = 0
    models: List[CLIProxyModel] = Field(default_factory=list)


class CLIProxyPlatform(BaseModel):
    id: str
    label: str
    namespace: str
    oauth: bool = False
    import_types: List[str] = Field(default_factory=list)
    account_count: int = 0
    healthy_count: int = 0
    models: List[CLIProxyModel] = Field(default_factory=list)
    error: str = ""


class CLIProxyOverview(BaseModel):
    configured: bool
    reachable: bool
    version: str
    public_url: str = ""
    routing_strategy: str = "unknown"
    platform_count: int = 0
    account_count: int = 0
    healthy_count: int = 0
    unavailable_count: int = 0
    disabled_count: int = 0
    message: str = ""


class CLIProxyOAuthSession(BaseModel):
    provider: str
    status: str
    state: str = ""
    url: str = ""
    flow: str = "browser"
    user_code: str = ""
    expires_in: Optional[int] = None
    error: str = ""


class CLIProxyRouting(BaseModel):
    strategy: str
    supported_strategies: List[str] = Field(
        default_factory=lambda: ["round-robin", "fill-first"]
    )
    session_affinity: bool = False
    retry_count: int = 2
    max_retry_credentials: int = 3
    max_retry_interval_seconds: int = 15


class CLIProxyLogs(BaseModel):
    lines: List[str] = Field(default_factory=list)
    cursor: str = ""
    cursor_reset: bool = False
    line_count: int = 0


class CLIProxyAlias(BaseModel):
    name: str
    alias: str
    fork: bool = False
    display_name: str = ""
    force_mapping: bool = True


class CLIProxyApiKeyImport(BaseModel):
    provider: str
    api_key: str = Field(repr=False, min_length=1)
    name: str = ""
    base_url: str = ""
    models: List[str] = Field(default_factory=list)
    prefix: str = ""


class CLIProxyConnectivityTest(BaseModel):
    model: str


class CLIProxyStatusUpdate(BaseModel):
    enabled: bool


class CLIProxyRoutingUpdate(BaseModel):
    strategy: Optional[str] = None
    retry_count: Optional[int] = Field(default=None, ge=0, le=10)
    max_retry_interval_seconds: Optional[int] = Field(default=None, ge=0, le=300)


class CLIProxyQuotaReset(BaseModel):
    auth_index: str


class CLIProxyAliasUpdate(BaseModel):
    aliases: List[CLIProxyAlias] = Field(default_factory=list)


class CLIProxyExclusionUpdate(BaseModel):
    models: List[str] = Field(default_factory=list)


class CLIProxyImportResult(BaseModel):
    status: str = "ok"
    platform: str
    name: str = ""
    detail: Dict[str, Any] = Field(default_factory=dict)


class CLIProxyCredentialImportItem(BaseModel):
    name: str
    email: str = ""
    status: str
    error: str = ""


class CLIProxyCredentialImportResponse(BaseModel):
    success: bool
    platform: str
    source_name: str
    total: int
    imported: int
    failed: int
    items: List[CLIProxyCredentialImportItem] = Field(default_factory=list)
