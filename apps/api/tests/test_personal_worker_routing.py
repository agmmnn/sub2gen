from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sub2gen.workers.personal import PersonalWorkerRouting


class Worker:
    def __init__(
        self,
        *,
        projects: set[str] | None = None,
        tokens: set[str] | None = None,
        initialized: bool = False,
        live: bool = False,
        restart_pending: bool = False,
        cooldown: float = 0.0,
    ) -> None:
        self._project_resident_affinity = {key: "slot" for key in projects or set()}
        self._token_resident_affinity = {key: "slot" for key in tokens or set()}
        self._resident_tabs = {}
        self._browser_lock = asyncio.Lock()
        self._legacy_lock = asyncio.Lock()
        self._tab_build_lock = asyncio.Lock()
        self._initialized = initialized
        self.browser = SimpleNamespace(stopped=not live, _sub2gen_runtime_disconnected=False) if initialized else None
        self._fresh_profile_restart_pending = restart_pending
        self._fresh_profile_restart_task = None
        self._cooldown = cooldown

    def _get_browser_launch_cooldown_remaining_seconds(self) -> float:
        return self._cooldown

    def get_resident_count(self) -> int:
        return len(self._resident_tabs)


def test_routing_owns_affinity_and_discards_stale_worker_indexes() -> None:
    routing = PersonalWorkerRouting(affinity_cache_limit=2)
    workers = [Worker(), Worker()]

    routing.remember(workers, project_key="project-1", token_key="7", worker_index=1)
    routing.remember(workers, project_key="project-2", worker_index=0)
    routing.remember(workers, project_key="project-3", worker_index=0)

    assert routing.project_affinity == {"project-2": 0, "project-3": 0}
    assert routing.token_affinity == {"7": 1}
    routing.cleanup(worker_count=1)
    assert routing.token_affinity == {}


def test_candidate_order_preserves_exact_slot_then_affinity() -> None:
    routing = PersonalWorkerRouting()
    workers = [Worker(projects={"project-1"}), Worker(tokens={"7"}), Worker()]

    candidates = routing.candidate_indexes(
        workers,
        project_key="project-1",
        token_key="7",
        slot_id="b3-slot-4",
    )

    assert candidates == [2, 1, 0]


def test_dispatch_score_penalizes_restart_and_launch_cooldown() -> None:
    routing = PersonalWorkerRouting()
    restarting = Worker(restart_pending=True)
    cooling = Worker(cooldown=30.0)
    available = Worker()
    workers = [restarting, cooling, available]

    candidates = routing.candidate_indexes(workers, allow_affinity=False)

    assert candidates[0] == 2
    assert candidates[-1] == 0
