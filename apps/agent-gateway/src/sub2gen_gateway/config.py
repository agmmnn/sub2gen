import os
from dataclasses import dataclass
from typing import Literal


@dataclass
class Settings:
    # sub2gen remote_browser_api_key must match (Bearer)
    sub2gen_bearer: str
    # Previous bearer accepted during rotation window.
    sub2gen_bearer_previous: str
    # WebSocket agents send this in register
    agent_device_token: str
    agent_auth_mode: Literal["legacy", "keygen", "dual"]
    keygen_verify_mode: Literal["jwt", "introspection"]
    keygen_api_url: str
    keygen_account: str
    keygen_api_token: str
    keygen_public_key: str
    keygen_issuer: str
    keygen_audience: str
    keygen_leeway_seconds: int
    host: str
    port: int
    solve_timeout_seconds: int


def load_settings() -> Settings:
    raw_timeout = os.environ.get("SOLVE_TIMEOUT_SECONDS")
    t = 120
    if raw_timeout:
        t = max(5, int(raw_timeout))
    auth_mode = (os.environ.get("GATEWAY_AGENT_AUTH_MODE") or "legacy").strip().lower()
    if auth_mode not in {"legacy", "keygen", "dual"}:
        auth_mode = "legacy"
    verify_mode = (os.environ.get("KEYGEN_VERIFY_MODE") or "jwt").strip().lower()
    if verify_mode not in {"jwt", "introspection"}:
        verify_mode = "jwt"
    raw_leeway = os.environ.get("KEYGEN_LEEWAY_SECONDS")
    leeway_seconds = 10
    if raw_leeway:
        leeway_seconds = max(0, int(raw_leeway))
    return Settings(
        sub2gen_bearer=(os.environ.get("GATEWAY_SUB2GEN_BEARER") or "").strip(),
        sub2gen_bearer_previous=(os.environ.get("GATEWAY_SUB2GEN_BEARER_PREVIOUS") or "").strip(),
        agent_device_token=(os.environ.get("GATEWAY_AGENT_DEVICE_TOKEN") or "").strip(),
        agent_auth_mode=auth_mode,  # type: ignore[arg-type]
        keygen_verify_mode=verify_mode,  # type: ignore[arg-type]
        keygen_api_url=(os.environ.get("KEYGEN_API_URL") or "https://api.keygen.sh").strip(),
        keygen_account=(os.environ.get("KEYGEN_ACCOUNT") or "").strip(),
        keygen_api_token=(os.environ.get("KEYGEN_API_TOKEN") or "").strip(),
        keygen_public_key=(os.environ.get("KEYGEN_PUBLIC_KEY") or "").strip(),
        keygen_issuer=(os.environ.get("KEYGEN_ISSUER") or "https://api.keygen.sh").strip(),
        keygen_audience=(os.environ.get("KEYGEN_AUDIENCE") or "sub2gen-agent-gateway").strip(),
        keygen_leeway_seconds=leeway_seconds,
        host=(os.environ.get("GATEWAY_HOST") or "0.0.0.0").strip() or "0.0.0.0",
        port=int(os.environ.get("GATEWAY_PORT") or "9080"),
        solve_timeout_seconds=t,
    )
