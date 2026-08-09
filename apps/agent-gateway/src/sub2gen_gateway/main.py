"""Uvicorn entry: ``uvicorn sub2gen_gateway.main:app``."""
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import load_settings
from .routes_sub2gen import router as sub2gen_router
from .ws_agents import router as ws_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agent-gateway] %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = load_settings()
    min_bearer_len = 16
    if not s.sub2gen_bearer:
        logging.getLogger(__name__).error(
            "GATEWAY_SUB2GEN_BEARER is empty — set to match sub2gen remote_browser_api_key"
        )
        raise RuntimeError("GATEWAY_SUB2GEN_BEARER must be set")
    if len(s.sub2gen_bearer) < min_bearer_len:
        logging.getLogger(__name__).error(
            "GATEWAY_SUB2GEN_BEARER is too short (<%s chars). Use a high-entropy secret.",
            min_bearer_len,
        )
        raise RuntimeError("GATEWAY_SUB2GEN_BEARER is too short")
    if s.sub2gen_bearer_previous and len(s.sub2gen_bearer_previous) < min_bearer_len:
        logging.getLogger(__name__).error(
            "GATEWAY_SUB2GEN_BEARER_PREVIOUS is too short (<%s chars).",
            min_bearer_len,
        )
        raise RuntimeError("GATEWAY_SUB2GEN_BEARER_PREVIOUS is too short")
    if s.sub2gen_bearer_previous:
        logging.getLogger(__name__).info(
            "sub2gen bearer rotation window enabled (current+previous accepted)"
        )
    if s.agent_auth_mode in {"legacy", "dual"} and not s.agent_device_token:
        logging.getLogger(__name__).warning(
            "GATEWAY_AGENT_DEVICE_TOKEN is empty — WebSocket agents cannot authenticate"
        )
    if s.agent_auth_mode in {"keygen", "dual"}:
        if s.keygen_verify_mode == "jwt" and not s.keygen_public_key:
            logging.getLogger(__name__).warning(
                "KEYGEN_PUBLIC_KEY is empty in keygen/jwt mode"
            )
        if s.keygen_verify_mode == "introspection" and not s.keygen_api_token:
            logging.getLogger(__name__).warning(
                "KEYGEN_API_TOKEN is empty in keygen/introspection mode"
            )
    yield


app = FastAPI(
    title="sub2gen Agent Gateway",
    description="Bridges sub2gen remote_browser HTTP to WebSocket agents.",
    lifespan=lifespan,
    # Operator UI: main sub2gen admin in apps/admin-web (Agent gateway tab), not Swagger here.
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(sub2gen_router, tags=["sub2gen"])
app.include_router(ws_router, tags=["agents"])


@app.get("/health")
def health() -> dict:
    s = load_settings()
    verify_mode = s.keygen_verify_mode if s.agent_auth_mode in {"keygen", "dual"} else ""
    return {
        "ok": True,
        "service": "sub2gen-agent-gateway",
        "auth_mode": s.agent_auth_mode,
        "verify_mode": verify_mode,
    }


def run() -> None:
    s = load_settings()
    uvicorn.run(
        "sub2gen_gateway.main:app",
        host=s.host,
        port=s.port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    run()
