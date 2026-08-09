"""Base resource for Google Flow capabilities."""

from __future__ import annotations

from typing import Any


class FlowResource:
    def __init__(self, client: Any) -> None:
        self.client = client
