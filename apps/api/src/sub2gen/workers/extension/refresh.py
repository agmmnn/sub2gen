"""Extension session-token refresh request executor."""

from __future__ import annotations

import asyncio
import json
import uuid

from .jobs import ExtensionJobBroker
from .models import ExtensionConnection


class ExtensionRefreshJobs:
    def __init__(self, broker: ExtensionJobBroker) -> None:
        self.broker = broker

    async def execute(
        self,
        connection: ExtensionConnection,
        *,
        token_id: int,
        timeout: int,
    ) -> str | None:
        request_id = f"req_{uuid.uuid4().hex}"
        future = self.broker.register("captcha", request_id, connection.websocket)
        try:
            await connection.websocket.send_text(
                json.dumps(
                    {
                        "type": "refresh_st",
                        "req_id": request_id,
                        "token_id": int(token_id),
                    }
                )
            )
            result = await asyncio.wait_for(future, timeout=timeout)
            if isinstance(result, dict) and result.get("status") == "success":
                return str(result.get("session_token") or "").strip() or None
            return None
        finally:
            self.broker.remove("captcha", request_id)
