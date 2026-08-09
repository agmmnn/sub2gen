"""State models shared by personal browser workers and their pool."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


class ResidentTabInfo:
    """Mutable state for one resident browser tab."""

    def __init__(
        self,
        tab: Any,
        slot_id: str,
        project_id: str | None = None,
        *,
        token_id: int | None = None,
        browser_context_id: Any = None,
    ) -> None:
        self.tab = tab
        self.slot_id = slot_id
        self.project_id = project_id or slot_id
        self.token_id = token_id
        self.browser_context_id = browser_context_id
        self.recaptcha_ready = False
        self.created_at = time.time()
        self.last_used_at = time.time()
        self.use_count = 0
        self.fingerprint: dict[str, Any] | None = None
        self.cookie_signature: str | None = None
        self.session_cookies: dict[str, str] | None = None
        self.session_cookies_fetched_at = 0.0
        self.solve_lock = asyncio.Lock()
        self.pending_assignment_count = 0


@dataclass
class TokenPoolLease:
    bucket_key: str
    token: str
    project_id: str
    action: str
    token_id: int | None
    slot_id: str | None
    worker_index: int | None
    solve_bundle: dict[str, Any] | None
    created_at: float
    expires_at: float


class TokenPoolTimeoutError(TimeoutError):
    """Raised when strict pool mode exhausts its wait deadline."""
