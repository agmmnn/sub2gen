"""Resident-tab registry and allocation state for personal browser workers."""

from __future__ import annotations

import asyncio
from typing import Any

from .models import ResidentTabInfo


class ResidentTabRegistry:
    """Own resident slot identity, affinity, reservation, and recovery state."""

    def __init__(self, *, slot_prefix: str = "") -> None:
        self.slot_prefix = slot_prefix
        self.tabs: dict[str, ResidentTabInfo] = {}
        self.token_affinity: dict[str, str] = {}
        self.project_affinity: dict[str, str] = {}
        self.slot_sequence = 0
        self.pick_index = 0
        self.lock = asyncio.Lock()
        self.error_streaks: dict[str, int] = {}
        self.unavailable_slots: set[str] = set()
        self.rebuild_tasks: dict[str, asyncio.Task[Any]] = {}
        self.recovery_tasks: dict[str, asyncio.Task[Any]] = {}

    def next_slot_id(self) -> str:
        self.slot_sequence += 1
        return f"{self.slot_prefix}slot-{self.slot_sequence}"

    @staticmethod
    def normalize_token_key(token_id: int | None) -> str:
        try:
            normalized = int(token_id or 0)
        except (TypeError, ValueError):
            normalized = 0
        return str(normalized) if normalized > 0 else ""

    def forget_token_affinity(self, slot_id: str | None, *, preserve_token_key: str | None = None) -> None:
        if not slot_id:
            return
        stale = [
            key
            for key, mapped_slot in self.token_affinity.items()
            if mapped_slot == slot_id and key != preserve_token_key
        ]
        for key in stale:
            self.token_affinity.pop(key, None)

    def forget_project_affinity(self, slot_id: str | None, *, preserve_project_id: str | None = None) -> None:
        if not slot_id:
            return
        stale = [
            key
            for key, mapped_slot in self.project_affinity.items()
            if mapped_slot == slot_id and key != preserve_project_id
        ]
        for key in stale:
            self.project_affinity.pop(key, None)

    def has_pending_assignment(self, slot_id: str | None, info: ResidentTabInfo | None = None) -> bool:
        current = self._resolve(slot_id, info)
        return current is not None and int(current.pending_assignment_count or 0) > 0

    def is_busy(self, slot_id: str | None, info: ResidentTabInfo | None = None) -> bool:
        current = self._resolve(slot_id, info)
        return current is not None and (current.solve_lock.locked() or self.has_pending_assignment(slot_id, current))

    def reserve(self, slot_id: str | None, info: ResidentTabInfo | None = None) -> bool:
        current = self._resolve(slot_id, info)
        if current is None or not current.tab or self.is_busy(slot_id, current):
            return False
        current.pending_assignment_count = int(current.pending_assignment_count or 0) + 1
        return True

    def release(self, slot_id: str | None, info: ResidentTabInfo | None = None) -> None:
        current = self._resolve(slot_id, info)
        if current is None:
            return
        current.pending_assignment_count = max(0, int(current.pending_assignment_count or 0) - 1)

    def resolve_token_affinity(self, token_id: int | None, *, available_only: bool = False) -> str | None:
        token_key = self.normalize_token_key(token_id)
        if not token_key:
            return None
        slot_id = self.token_affinity.get(token_key)
        if not slot_id:
            return None
        info = self.tabs.get(slot_id)
        if info and info.tab and slot_id not in self.unavailable_slots and info.token_id == int(token_key):
            return None if available_only and self.is_busy(slot_id, info) else slot_id
        if slot_id not in self.tabs or (info is not None and info.token_id != int(token_key)):
            self.token_affinity.pop(token_key, None)
        return None

    def resolve_project_affinity(self, project_id: str | None, *, available_only: bool = False) -> str | None:
        project_key = str(project_id or "").strip()
        if not project_key:
            return None
        slot_id = self.project_affinity.get(project_key)
        if not slot_id:
            return None
        info = self.tabs.get(slot_id)
        if info and info.tab and slot_id not in self.unavailable_slots and info.project_id == project_key:
            return None if available_only and self.is_busy(slot_id, info) else slot_id
        if slot_id not in self.tabs or (info is not None and info.project_id != project_key):
            self.project_affinity.pop(project_key, None)
        return None

    def remember_project(self, project_id: str | None, slot_id: str | None, info: ResidentTabInfo | None) -> None:
        project_key = str(project_id or "").strip()
        if not project_key or not slot_id or info is None:
            return
        self.forget_project_affinity(slot_id, preserve_project_id=project_key)
        self.project_affinity[project_key] = slot_id
        info.project_id = project_key

    def remember_token(self, token_id: int | None, slot_id: str | None, info: ResidentTabInfo | None) -> None:
        token_key = self.normalize_token_key(token_id)
        if not token_key or not slot_id or info is None:
            return
        self.forget_token_affinity(slot_id, preserve_token_key=token_key)
        self.token_affinity[token_key] = slot_id
        info.token_id = int(token_key)

    def mark_unavailable(self, slot_id: str | None, info: ResidentTabInfo | None = None) -> None:
        normalized = str(slot_id or "").strip()
        if not normalized:
            return
        self.unavailable_slots.add(normalized)
        current = self.tabs.get(normalized) or info
        if current is not None:
            current.recaptcha_ready = False

    def clear_unavailable(self, slot_id: str | None) -> None:
        normalized = str(slot_id or "").strip()
        if normalized:
            self.unavailable_slots.discard(normalized)

    def _resolve(self, slot_id: str | None, info: ResidentTabInfo | None) -> ResidentTabInfo | None:
        normalized = str(slot_id or "").strip()
        if not normalized:
            return None
        return info or self.tabs.get(normalized)
