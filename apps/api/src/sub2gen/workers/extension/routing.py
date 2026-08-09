"""Health-aware extension worker selection policy."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from .models import DedicatedWorkerStats, ExtensionConnection


EMA_ALPHA = 0.25
TIE_DELTA = 5.0
FAILURE_WINDOW_SECONDS = 30.0
COOLDOWN_SECONDS = 20.0
FAILURES_FOR_COOLDOWN = 2
SCORE_WEIGHT_SUCCESS = 100.0
SCORE_WEIGHT_INFLIGHT = 15.0
SCORE_WEIGHT_EMA_DIVISOR = 50.0
SCORE_WEIGHT_TIMEOUT = 20.0
TIMEOUT_WINDOW_SECONDS = 60.0


class ExtensionWorkerRouting:
    """Own worker health signals and deterministic hybrid selection cursors."""

    def __init__(self) -> None:
        self.worker_stats: dict[str, DedicatedWorkerStats] = {}
        self.round_robin: dict[str, int] = {}
        self.lock = asyncio.Lock()

    def stats(self, worker_session_id: str) -> DedicatedWorkerStats:
        session_id = (worker_session_id or "").strip() or "_"
        if session_id not in self.worker_stats:
            self.worker_stats[session_id] = DedicatedWorkerStats()
        return self.worker_stats[session_id]

    @staticmethod
    def prune_timestamps(stamps: list[float], now: float, window: float) -> None:
        cutoff = now - window
        stamps[:] = [stamp for stamp in stamps if stamp >= cutoff]

    def score(self, stats: DedicatedWorkerStats, now: float) -> float:
        self.prune_timestamps(stats.fail_timestamps, now, FAILURE_WINDOW_SECONDS)
        self.prune_timestamps(stats.timeout_timestamps, now, TIMEOUT_WINDOW_SECONDS)
        total = stats.success_count + stats.fail_count
        success_rate = (stats.success_count / total) if total > 0 else 1.0
        latency = stats.ema_latency_ms if stats.has_latency_sample else 0.0
        return float(
            success_rate * SCORE_WEIGHT_SUCCESS
            - stats.inflight_count * SCORE_WEIGHT_INFLIGHT
            - (latency / SCORE_WEIGHT_EMA_DIVISOR)
            - len(stats.timeout_timestamps) * SCORE_WEIGHT_TIMEOUT
        )

    def _pick_scored(
        self,
        candidates: list[ExtensionConnection],
        *,
        round_robin_key: str,
    ) -> tuple[ExtensionConnection, float, int, int]:
        now = time.time()
        healthy = [
            connection for connection in candidates if self.stats(connection.worker_session_id).cooldown_until <= now
        ]
        pool = healthy if healthy else list(candidates)
        scored = [(self.score(self.stats(connection.worker_session_id), now), connection) for connection in pool]
        best_score = max(score for score, _connection in scored)
        tied = sorted(
            (connection for score, connection in scored if abs(score - best_score) <= TIE_DELTA),
            key=lambda connection: connection.worker_session_id,
        )
        index = self.round_robin.get(round_robin_key, 0) % len(tied)
        chosen = tied[index]
        self.round_robin[round_robin_key] = (index + 1) % len(tied)
        return chosen, best_score, index, len(healthy)

    def pick_dedicated(
        self,
        pool: list[ExtensionConnection],
        preferred_token_id: int,
        *,
        exclude_worker_session_ids: set[str] | None = None,
        selection_meta_out: dict[str, Any] | None = None,
        eligible: Callable[[ExtensionConnection], bool],
        hybrid_rr_suffix: str = "captcha",
        log_info: Callable[[str], None] | None = None,
    ) -> ExtensionConnection | None:
        token_id = int(preferred_token_id)
        excluded = exclude_worker_session_ids or set()
        candidates = [
            connection
            for connection in pool
            if connection.refresh_token_id is not None
            and int(connection.refresh_token_id) == token_id
            and connection.worker_session_id not in excluded
            and eligible(connection)
        ]
        if not candidates:
            return None
        key = f"dedicated:{token_id}:{hybrid_rr_suffix}"
        chosen, score, index, healthy_count = self._pick_scored(candidates, round_robin_key=key)
        if selection_meta_out is not None:
            selection_meta_out.clear()
            selection_meta_out.update(
                dedicated_hybrid=True,
                dedicated_hybrid_suffix=hybrid_rr_suffix,
                dedicated_token_id=token_id,
                dedicated_score=round(score, 2),
                dedicated_rr_idx=index,
                dedicated_pool_size=len(candidates),
                dedicated_pick_from=healthy_count or len(candidates),
            )
        if log_info is not None:
            log_info(
                "[Extension Captcha] Dedicated hybrid pick: "
                f"token_id={token_id}, pool={hybrid_rr_suffix}, "
                f"worker_session_id={chosen.worker_session_id}, score={score:.2f}, "
                f"rr_idx={index}, candidates={len(candidates)}, healthy={healthy_count}"
            )
        return chosen

    def pick_global_captcha_worker(
        self,
        pool: list[ExtensionConnection],
        *,
        exclude_worker_session_ids: set[str] | None = None,
        selection_meta_out: dict[str, Any] | None = None,
        eligible: Callable[[ExtensionConnection], bool],
        log_info: Callable[[str], None] | None = None,
    ) -> ExtensionConnection | None:
        excluded = exclude_worker_session_ids or set()
        candidates = [
            connection
            for connection in pool
            if connection.captcha_worker_id is not None
            and connection.worker_session_id not in excluded
            and eligible(connection)
        ]
        if not candidates:
            return None
        chosen, score, index, healthy_count = self._pick_scored(candidates, round_robin_key="captcha_worker:global")
        if selection_meta_out is not None:
            selection_meta_out.clear()
            selection_meta_out.update(
                captcha_worker_pool=True,
                captcha_worker_score=round(score, 2),
                captcha_worker_rr_idx=index,
                captcha_worker_pool_size=len(candidates),
                captcha_worker_pick_from=healthy_count or len(candidates),
            )
        if log_info is not None:
            log_info(
                "[Extension Captcha] Global captcha worker pick: "
                f"worker_session_id={chosen.worker_session_id}, "
                f"captcha_worker_id={chosen.captcha_worker_id}, score={score:.2f}, "
                f"rr_idx={index}, candidates={len(candidates)}, healthy={healthy_count}"
            )
        return chosen

    def record_failure(self, stats: DedicatedWorkerStats, now: float, *, is_timeout: bool) -> None:
        if is_timeout:
            stats.timeout_timestamps.append(now)
            self.prune_timestamps(stats.timeout_timestamps, now, TIMEOUT_WINDOW_SECONDS)
            stats.cooldown_until = max(stats.cooldown_until, now + COOLDOWN_SECONDS)
            return
        stats.fail_count += 1
        stats.fail_timestamps.append(now)
        self.prune_timestamps(stats.fail_timestamps, now, FAILURE_WINDOW_SECONDS)
        if len(stats.fail_timestamps) >= FAILURES_FOR_COOLDOWN:
            stats.cooldown_until = max(stats.cooldown_until, now + COOLDOWN_SECONDS)

    @staticmethod
    def record_success(stats: DedicatedWorkerStats, latency_ms: float) -> None:
        stats.success_count += 1
        if stats.has_latency_sample:
            stats.ema_latency_ms = EMA_ALPHA * latency_ms + (1.0 - EMA_ALPHA) * stats.ema_latency_ms
        else:
            stats.ema_latency_ms = latency_ms
            stats.has_latency_sample = True
