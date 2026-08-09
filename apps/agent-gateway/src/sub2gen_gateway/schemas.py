"""Message shapes for WebSocket agents (reference; validation can be tightened later)."""
from typing import Literal, Optional

from pydantic import AliasChoices, BaseModel, Field


class WsRegister(BaseModel):
    type: Literal["register"] = "register"
    # Legacy shared secret (legacy/dual mode).
    device_token: str = ""
    # Keygen-backed identity token (keygen/dual mode).
    agent_token: str = Field(
        default="",
        validation_alias=AliasChoices("agent_token", "agentToken", "license_token", "licenseToken"),
    )
    # Optional Keygen token resource id (UUID). Preferred for introspection lookup.
    agent_token_id: str = Field(
        default="",
        validation_alias=AliasChoices("agent_token_id", "agentTokenId", "license_token_id", "licenseTokenId"),
    )
    # Compatibility aliases (kept for client payload clarity in logs/docs).
    license_token: str = Field(default="", validation_alias=AliasChoices("license_token", "licenseToken"))
    license_token_id: str = Field(
        default="",
        validation_alias=AliasChoices("license_token_id", "licenseTokenId"),
    )
    # Optional machine or license identifier (for introspection fallback / debugging).
    agent_id: str = ""


class AgentIdentity(BaseModel):
    auth_method: Literal["legacy", "keygen"]
    subject: str
    machine_id: str = ""
    license_id: str = ""
    account_id: str = ""


class WsSolveResult(BaseModel):
    type: Literal["solve_result"] = "solve_result"
    job_id: str
    token: str
    session_id: str
    fingerprint: Optional[dict] = None


class WsSolveError(BaseModel):
    type: Literal["solve_error"] = "solve_error"
    job_id: str
    error: str = "agent_error"


