"""Affinity-aware routing policy for personal browser workers."""

from __future__ import annotations

import re
from typing import Any


class PersonalWorkerRouting:
    """Own pool dispatch state and rank browser workers without running them."""

    def __init__(self, *, affinity_cache_limit: int = 256) -> None:
        self.round_robin_index = 0
        self.reservations: dict[int, int] = {}
        self.project_affinity: dict[str, int] = {}
        self.token_affinity: dict[str, int] = {}
        self.affinity_cache_limit = affinity_cache_limit

    @staticmethod
    def parse_worker_index(slot_id: str | None) -> int | None:
        normalized = str(slot_id or "").strip()
        match = re.match(r"^b(\d+)-", normalized)
        if not match:
            return None
        index = int(match.group(1)) - 1
        return index if index >= 0 else None

    def remember(
        self,
        workers: list[Any],
        *,
        project_key: str = "",
        token_key: str = "",
        slot_id: str | None = None,
        worker_index: int | None = None,
    ) -> None:
        resolved_index = worker_index if worker_index is not None else self.parse_worker_index(slot_id)
        if resolved_index is None or not (0 <= resolved_index < len(workers)):
            return
        if project_key:
            self.project_affinity[project_key] = resolved_index
            self.trim(self.project_affinity)
        if token_key:
            self.token_affinity[token_key] = resolved_index
            self.trim(self.token_affinity)

    def trim(self, cache: dict[str, int]) -> None:
        while len(cache) > self.affinity_cache_limit:
            try:
                cache.pop(next(iter(cache)), None)
            except StopIteration:
                return

    def cleanup(self, worker_count: int) -> None:
        valid_indexes = set(range(worker_count))
        self.project_affinity = {key: value for key, value in self.project_affinity.items() if value in valid_indexes}
        self.token_affinity = {key: value for key, value in self.token_affinity.items() if value in valid_indexes}

    @staticmethod
    def worker_has_project_mapping(worker: Any, project_key: str) -> bool:
        if not project_key:
            return False
        if project_key in (getattr(worker, "_project_resident_affinity", {}) or {}):
            return True
        return any(
            str(getattr(info, "project_id", "") or "").strip() == project_key
            for info in (getattr(worker, "_resident_tabs", {}) or {}).values()
        )

    @staticmethod
    def worker_has_token_mapping(worker: Any, token_key: str) -> bool:
        if not token_key:
            return False
        if token_key in (getattr(worker, "_token_resident_affinity", {}) or {}):
            return True
        for info in (getattr(worker, "_resident_tabs", {}) or {}).values():
            try:
                if int(getattr(info, "token_id", 0) or 0) == int(token_key):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def worker_busy_score(worker: Any) -> int:
        score = sum(
            1
            for lock_name in ("_browser_lock", "_legacy_lock", "_tab_build_lock")
            if (lock := getattr(worker, lock_name, None)) is not None and lock.locked()
        )
        for info in (getattr(worker, "_resident_tabs", {}) or {}).values():
            try:
                score += int(info.solve_lock.locked())
                score += int(int(getattr(info, "pending_assignment_count", 0) or 0) > 0)
            except Exception:
                continue
        return score

    @staticmethod
    def worker_has_live_runtime(worker: Any) -> bool:
        browser = getattr(worker, "browser", None)
        return bool(
            getattr(worker, "_initialized", False)
            and browser
            and not getattr(browser, "stopped", False)
            and not getattr(browser, "_sub2gen_runtime_disconnected", False)
        )

    @staticmethod
    def worker_has_pending_restart(worker: Any) -> bool:
        restart_task = getattr(worker, "_fresh_profile_restart_task", None)
        return bool(
            getattr(worker, "_fresh_profile_restart_pending", False)
            or (restart_task is not None and not restart_task.done())
        )

    @staticmethod
    def worker_launch_cooldown(worker: Any) -> float:
        try:
            return max(0.0, float(worker._get_browser_launch_cooldown_remaining_seconds() or 0.0))
        except Exception:
            return 0.0

    def worker_runtime_unavailable_score(self, worker: Any) -> int:
        if self.worker_has_live_runtime(worker):
            return 0
        if self.worker_launch_cooldown(worker) > 0.0:
            return 3
        return 2 if getattr(worker, "_initialized", False) else 1

    def dispatch_score(
        self,
        worker_index: int,
        worker: Any,
        *,
        worker_count: int,
        affinity_preferred: bool = False,
    ) -> tuple[int, int, int, int, int, int, int]:
        reservations = int(self.reservations.get(worker_index, 0) or 0)
        cooldown = self.worker_launch_cooldown(worker)
        return (
            int(self.worker_has_pending_restart(worker)),
            reservations + self.worker_busy_score(worker),
            self.worker_runtime_unavailable_score(worker),
            int(cooldown > 0.0),
            int(worker.get_resident_count() <= 0),
            int(not affinity_preferred),
            (worker_index - self.round_robin_index) % max(worker_count, 1),
        )

    def find_project_worker(self, workers: list[Any], project_key: str) -> int | None:
        if not project_key:
            return None
        mapped = self.project_affinity.get(project_key)
        if mapped is not None and 0 <= mapped < len(workers):
            return mapped
        for index, worker in enumerate(workers):
            if self.worker_has_project_mapping(worker, project_key):
                self.project_affinity[project_key] = index
                self.trim(self.project_affinity)
                return index
        return None

    def find_token_worker(self, workers: list[Any], token_key: str) -> int | None:
        if not token_key:
            return None
        mapped = self.token_affinity.get(token_key)
        if mapped is not None and 0 <= mapped < len(workers):
            if self.worker_has_token_mapping(workers[mapped], token_key):
                return mapped
            self.token_affinity.pop(token_key, None)
        for index, worker in enumerate(workers):
            if self.worker_has_token_mapping(worker, token_key):
                self.token_affinity[token_key] = index
                self.trim(self.token_affinity)
                return index
        return None

    def candidate_indexes(
        self,
        workers: list[Any],
        *,
        project_key: str = "",
        token_key: str = "",
        slot_id: str | None = None,
        allow_affinity: bool = True,
    ) -> list[int]:
        worker_count = len(workers)
        if worker_count == 0:
            return []
        preferred: list[int] = []
        exact_index = self.parse_worker_index(slot_id)
        if exact_index is not None and 0 <= exact_index < worker_count:
            preferred.append(exact_index)
        soft_affinity: list[int] = []
        if allow_affinity:
            for candidate in (
                self.find_token_worker(workers, token_key),
                self.find_project_worker(workers, project_key),
            ):
                if candidate is not None and candidate not in preferred and candidate not in soft_affinity:
                    soft_affinity.append(candidate)
        preferred.extend(soft_affinity)
        remaining = [index for index in range(worker_count) if index not in preferred]
        if remaining:
            offset = self.round_robin_index % len(remaining)
            rotated = remaining[offset:] + remaining[:offset]
            ranked = sorted(
                enumerate(rotated),
                key=lambda item: (
                    self.dispatch_score(
                        item[1],
                        workers[item[1]],
                        worker_count=worker_count,
                        affinity_preferred=item[1] in soft_affinity,
                    ),
                    item[0],
                ),
            )
            preferred.extend(index for _position, index in ranked)
        return preferred
