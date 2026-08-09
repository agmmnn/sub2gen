"""Load balancing module for sub2gen"""
import asyncio
import random
from typing import Optional, Dict, Set, Any
from ..core.models import Token
from ..core.config import config
from ..core.account_tiers import (
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from .concurrency_manager import ConcurrencyManager
from ..core.logger import debug_logger


class LoadBalancer:
    """Token load balancer with load-aware selection"""

    def __init__(self, token_manager, concurrency_manager: Optional[ConcurrencyManager] = None):
        self.token_manager = token_manager
        self.concurrency_manager = concurrency_manager
        self._image_pending: Dict[int, int] = {}
        self._video_pending: Dict[int, int] = {}
        self._pending_lock = asyncio.Lock()
        self._round_robin_state: Dict[str, Optional[int]] = {"image": None, "video": None, "default": None}
        self._rr_lock = asyncio.Lock()
        self._last_selection_diagnostics: Dict[str, Any] = {}

    def _summarize_reasons(self, reasons: Dict[int, str]) -> list[Dict[str, Any]]:
        summary_counts: Dict[str, int] = {}
        for reason in reasons.values():
            summary_counts[reason] = summary_counts.get(reason, 0) + 1
        return [
            {"reason": reason, "count": count}
            for reason, count in sorted(summary_counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    def _store_selection_diagnostics(self, diagnostics: Dict[str, Any], sink: Optional[Dict[str, Any]] = None) -> None:
        self._last_selection_diagnostics = dict(diagnostics)
        if sink is not None:
            sink.clear()
            sink.update(diagnostics)

    def get_last_selection_diagnostics(self) -> Dict[str, Any]:
        return dict(self._last_selection_diagnostics)

    def build_diagnostics_reason(self, diagnostics: Optional[Dict[str, Any]]) -> Optional[str]:
        if not diagnostics:
            return None
        allowlist_filter_reason_type = str(diagnostics.get("allowlist_filter_reason_type") or "").strip()
        project_pin_context = (
            allowlist_filter_reason_type == "project_pin"
            and int(diagnostics.get("allowed_token_ids_count") or 0) > 0
        )
        priority_summary = diagnostics.get("post_check_failure_summary") or diagnostics.get("filtered_reason_summary") or []
        if project_pin_context and isinstance(priority_summary, list) and priority_summary:
            non_pin = [item for item in priority_summary if "绑定项目账号" not in str(item.get("reason") or "")]
            if non_pin:
                priority_summary = non_pin
        if not isinstance(priority_summary, list) or not priority_summary:
            if project_pin_context:
                return "当前请求已绑定到指定项目所属账号，其他账号不会参与本次调度"
            return None
        top_reason = priority_summary[0]
        reason = str(top_reason.get("reason") or "").strip()
        count = int(top_reason.get("count") or 0)
        total_active = int(diagnostics.get("total_active_tokens") or 0)
        if not reason:
            if project_pin_context:
                return "当前请求已绑定到指定项目所属账号，其他账号不会参与本次调度"
            return None
        if project_pin_context and "绑定项目账号" in reason:
            return "当前请求已绑定到指定项目所属账号，其他账号不会参与本次调度"
        if project_pin_context:
            reason = f"{reason}（当前请求已绑定项目账号）"
        if count > 0 and total_active > 0:
            return f"没有可用Token：{reason}（{count}/{total_active}）"
        return f"没有可用Token：{reason}"

    async def _get_pending_count(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> int:
        async with self._pending_lock:
            if for_image_generation:
                return max(0, int(self._image_pending.get(token_id, 0)))
            if for_video_generation:
                return max(0, int(self._video_pending.get(token_id, 0)))
            return 0

    async def _add_pending(self, token_id: int, for_image_generation: bool, for_video_generation: bool):
        async with self._pending_lock:
            if for_image_generation:
                self._image_pending[token_id] = max(0, int(self._image_pending.get(token_id, 0))) + 1
            elif for_video_generation:
                self._video_pending[token_id] = max(0, int(self._video_pending.get(token_id, 0))) + 1

    async def release_pending(self, token_id: int, for_image_generation: bool = False, for_video_generation: bool = False):
        async with self._pending_lock:
            if for_image_generation:
                current = max(0, int(self._image_pending.get(token_id, 0)))
                if current <= 1:
                    self._image_pending.pop(token_id, None)
                else:
                    self._image_pending[token_id] = current - 1
            elif for_video_generation:
                current = max(0, int(self._video_pending.get(token_id, 0)))
                if current <= 1:
                    self._video_pending.pop(token_id, None)
                else:
                    self._video_pending[token_id] = current - 1

    async def _get_token_load(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> tuple[int, Optional[int]]:
        """获取 token 当前负载。

        Returns:
            (inflight, remaining)
            remaining 为 None 表示无限制
        """
        if not self.concurrency_manager:
            return 0, None

        if for_image_generation:
            inflight = await self.concurrency_manager.get_image_inflight(token_id)
            remaining = await self.concurrency_manager.get_image_remaining(token_id)
            pending = await self._get_pending_count(token_id, True, False)
            effective_inflight = inflight + pending
            if remaining is not None:
                remaining = max(0, remaining - pending)
            return effective_inflight, remaining

        if for_video_generation:
            inflight = await self.concurrency_manager.get_video_inflight(token_id)
            remaining = await self.concurrency_manager.get_video_remaining(token_id)
            pending = await self._get_pending_count(token_id, False, True)
            effective_inflight = inflight + pending
            if remaining is not None:
                remaining = max(0, remaining - pending)
            return effective_inflight, remaining

        return 0, None

    async def _reserve_slot(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> bool:
        """尝试为当前 token 预占一个生成槽位。"""
        if not self.concurrency_manager:
            return True

        if for_image_generation:
            return await self.concurrency_manager.acquire_image(token_id)

        if for_video_generation:
            return await self.concurrency_manager.acquire_video(token_id)

        return True

    async def _select_round_robin(self, tokens: list[dict], scenario: str) -> Optional[dict]:
        """Select candidate in round-robin order for the given scenario."""
        if not tokens:
            return None

        tokens_sorted = sorted(tokens, key=lambda item: item["token"].id or 0)
        async with self._rr_lock:
            last_id = self._round_robin_state.get(scenario)
            start_idx = 0
            if last_id is not None:
                for idx, item in enumerate(tokens_sorted):
                    if item["token"].id == last_id:
                        start_idx = (idx + 1) % len(tokens_sorted)
                        break
            selected = tokens_sorted[start_idx]
            self._round_robin_state[scenario] = selected["token"].id
        return selected

    async def _check_extension_route(self, token: Token, managed_api_key_id: Optional[int] = None) -> tuple[bool, str]:
        """Ensure extension captcha requests are routed to the selected managed API key."""
        if config.captcha_method != "extension":
            return True, ""

        try:
            from .browser_captcha_extension import ExtensionCaptchaService

            service = await ExtensionCaptchaService.get_instance(getattr(self.token_manager, "db", None))
            has_connection = False
            route_key = ""
            if managed_api_key_id is not None and hasattr(service, "has_connection_for_managed_key"):
                has_connection = await service.has_connection_for_managed_key(managed_api_key_id)
                if has_connection:
                    return True, ""
                # Central/dedicated worker mode fallback: token-bound worker is acceptable
                # when managed-key binding is missing for the currently selected token.
                if hasattr(service, "has_connection_for_dedicated_token"):
                    has_dedicated = await service.has_connection_for_dedicated_token(token.id)
                    if has_dedicated:
                        debug_logger.log_info(
                            "[LOAD_BALANCER] managed-key worker missing, using dedicated token worker fallback: "
                            f"token_id={token.id}, managed_api_key_id={managed_api_key_id}"
                        )
                        return True, ""

            has_connection, route_key = await service.has_connection_for_token(
                token.id,
                managed_api_key_id=managed_api_key_id,
            )
            if has_connection:
                return True, ""

            available = service.describe_routes() or "none"
            if managed_api_key_id is not None:
                return (
                    False,
                    f"Managed API Key {managed_api_key_id} 没有可用扩展工作器，且 Token {token.id} 未连接专属工作器"
                    f"（可用路由: {available}）",
                )
            if route_key:
                return False, f"扩展路由 {route_key} 未连接（可用路由: {available}）"
            return False, f"扩展路由未配置或匿名插件未连接（可用路由: {available}）"
        except Exception as exc:
            return False, f"扩展路由检查失败: {exc}"

    async def select_token(
        self,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
        reserve: bool = False,
        enforce_concurrency_filter: bool = True,
        track_pending: bool = False,
        allowed_token_ids: Optional[Set[int]] = None,
        managed_api_key_id: Optional[int] = None,
        allowlist_filter_reason_type: Optional[str] = None,
        diagnostics_sink: Optional[Dict[str, Any]] = None,
    ) -> Optional[Token]:
        """
        Select a token using load-aware balancing

        Args:
            for_image_generation: If True, only select tokens with image_enabled=True
            for_video_generation: If True, only select tokens with video_enabled=True
            model: Model name (used to filter tokens for specific models)
            reserve: Whether to atomically reserve one concurrency slot for the selected token
            enforce_concurrency_filter:
                Whether to pre-filter tokens by current inflight/remaining capacity.
                For reserve=False generation paths, this should usually be False so
                requests can enter the downstream wait queue instead of failing fast.
            track_pending:
                Whether to count the selected token as a queued request immediately.
                This smooths burst distribution before the hard concurrency slot is acquired.

        Returns:
            Selected token or None if no available tokens
        """
        debug_logger.log_info(
            f"[LOAD_BALANCER] 开始选择Token (图片生成={for_image_generation}, "
            f"视频生成={for_video_generation}, 模型={model}, 预占槽位={reserve})"
        )

        active_tokens = await self.token_manager.get_active_tokens()
        debug_logger.log_info(f"[LOAD_BALANCER] 获取到 {len(active_tokens)} 个活跃Token")
        diagnostics: Dict[str, Any] = {
            "model": model,
            "for_image_generation": bool(for_image_generation),
            "for_video_generation": bool(for_video_generation),
            "reserve": bool(reserve),
            "enforce_concurrency_filter": bool(enforce_concurrency_filter),
            "track_pending": bool(track_pending),
            "total_active_tokens": len(active_tokens),
            "allowed_token_ids_count": len(allowed_token_ids) if allowed_token_ids is not None else 0,
            "effective_allowed_token_ids": sorted(int(token_id) for token_id in (allowed_token_ids or set())),
            "allowlist_filter_reason_type": (allowlist_filter_reason_type or "").strip() or (
                "project_pin" if allowed_token_ids is not None and len(allowed_token_ids) == 1 else "api_key_assignment"
            ),
            "managed_api_key_id": managed_api_key_id,
            "filtered_reasons": {},
            "filtered_reason_summary": [],
            "candidate_token_ids": [],
            "post_check_failures": {},
            "post_check_failure_summary": [],
            "selected_token_id": None,
        }
        if allowed_token_ids is not None:
            debug_logger.log_info(
                "[LOAD_BALANCER] allowlist context: "
                f"type={diagnostics['allowlist_filter_reason_type']}, "
                f"effective_allowed={diagnostics['effective_allowed_token_ids']}"
            )

        if not active_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ 没有活跃的Token")
            self._store_selection_diagnostics(diagnostics, diagnostics_sink)
            return None

        available_tokens = []
        filtered_reasons = {}
        required_tier = get_required_paygate_tier_for_model(model)

        for token in active_tokens:
            if allowed_token_ids is not None and token.id not in allowed_token_ids:
                if diagnostics["allowlist_filter_reason_type"] == "project_pin":
                    filtered_reasons[token.id] = "当前请求已绑定项目账号，本账号不参与本次调度"
                else:
                    filtered_reasons[token.id] = "不在 API Key 允许账号列表"
                continue
            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            if model and not supports_model_for_tier(model, normalized_tier):
                filtered_reasons[token.id] = '账号等级不足，需要 ' + get_paygate_tier_label(required_tier)
                continue
            if for_image_generation:
                if not token.image_enabled:
                    filtered_reasons[token.id] = "图片生成已禁用"
                    continue

                route_ok, route_reason = await self._check_extension_route(token, managed_api_key_id=managed_api_key_id)
                if not route_ok:
                    filtered_reasons[token.id] = route_reason
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_image(token.id)
                ):
                    filtered_reasons[token.id] = "图片并发已满"
                    continue

            if for_video_generation:
                if not token.video_enabled:
                    filtered_reasons[token.id] = "视频生成已禁用"
                    continue

                route_ok, route_reason = await self._check_extension_route(token, managed_api_key_id=managed_api_key_id)
                if not route_ok:
                    filtered_reasons[token.id] = route_reason
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_video(token.id)
                ):
                    filtered_reasons[token.id] = "视频并发已满"
                    continue

            inflight, remaining = await self._get_token_load(
                token.id,
                for_image_generation=for_image_generation,
                for_video_generation=for_video_generation
            )
            available_tokens.append({
                "token": token,
                "inflight": inflight,
                "remaining": remaining,
                "needs_refresh": self.token_manager.needs_at_refresh(token),
                "random": random.random()
            })

        if filtered_reasons:
            debug_logger.log_info(f"[LOAD_BALANCER] 已过滤Token:")
            for token_id, reason in filtered_reasons.items():
                debug_logger.log_info(f"[LOAD_BALANCER]   - Token {token_id}: {reason}")
        diagnostics["filtered_reasons"] = {str(token_id): reason for token_id, reason in filtered_reasons.items()}
        diagnostics["filtered_reason_summary"] = self._summarize_reasons(filtered_reasons)
        diagnostics["candidate_token_ids"] = [int(item["token"].id or 0) for item in available_tokens]

        if not available_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ 没有可用的Token (图片生成={for_image_generation}, 视频生成={for_video_generation})")
            debug_logger.log_info(
                "[LOAD_BALANCER] no-candidate diagnostics: "
                f"filtered_summary={diagnostics['filtered_reason_summary']}"
            )
            self._store_selection_diagnostics(diagnostics, diagnostics_sink)
            return None

        # 最低 in-flight 优先；有并发上限时，剩余槽位更多的 token 优先；最后随机打散
        call_mode = config.call_logic_mode
        if call_mode == "polling":
            scenario = "default"
            if for_image_generation:
                scenario = "image"
            elif for_video_generation:
                scenario = "video"

            ordered_candidates = []
            first_candidate = await self._select_round_robin(available_tokens, scenario)
            if first_candidate is not None:
                ordered_candidates.append(first_candidate)
                ordered_candidates.extend(
                    item for item in sorted(available_tokens, key=lambda item: item["token"].id or 0)
                    if item["token"].id != first_candidate["token"].id
                )
            available_tokens = ordered_candidates
        else:
            available_tokens.sort(
                key=lambda item: (
                    1 if item["needs_refresh"] else 0,
                    item["inflight"],
                    0 if item["remaining"] is None else 1,
                    -(item["remaining"] or 0),
                    item["random"]
                )
            )

        ready_candidates = [item for item in available_tokens if not item["needs_refresh"]]
        refresh_candidates = [item for item in available_tokens if item["needs_refresh"]]
        if ready_candidates and refresh_candidates:
            available_tokens = ready_candidates + refresh_candidates

        debug_logger.log_info("[LOAD_BALANCER] 候选Token负载:")
        for item in available_tokens:
            token = item["token"]
            remaining = "unlimited" if item["remaining"] is None else item["remaining"]
            debug_logger.log_info(
                f"[LOAD_BALANCER]   - Token {token.id} ({token.email}) "
                f"inflight={item['inflight']}, remaining={remaining}, "
                f"needs_refresh={item['needs_refresh']}, credits={token.credits}"
            )

        # 只为候选列表中真正尝试到的 token 做 AT 校验，避免每次请求把所有 token 全扫一遍
        post_check_failures: Dict[int, str] = {}
        for item in available_tokens:
            token = item["token"]
            token_id = token.id

            token = await self.token_manager.ensure_valid_token(token)
            if not token:
                debug_logger.log_info(f"[LOAD_BALANCER] 跳过 Token {token_id}: AT无效或已过期")
                post_check_failures[token_id] = "AT无效或已过期"
                continue

            if reserve and not await self._reserve_slot(token.id, for_image_generation, for_video_generation):
                debug_logger.log_info(f"[LOAD_BALANCER] 跳过 Token {token.id}: 预占槽位失败")
                post_check_failures[token.id] = "预占槽位失败"
                continue

            if track_pending:
                await self._add_pending(token.id, for_image_generation, for_video_generation)

            debug_logger.log_info(
                f"[LOAD_BALANCER] ✅ 已选择Token {token.id} ({token.email}) - "
                f"余额: {token.credits}, inflight={item['inflight']}"
            )
            diagnostics["post_check_failures"] = {str(tid): reason for tid, reason in post_check_failures.items()}
            diagnostics["post_check_failure_summary"] = self._summarize_reasons(post_check_failures)
            diagnostics["selected_token_id"] = int(token.id or 0)
            self._store_selection_diagnostics(diagnostics, diagnostics_sink)
            return token

        debug_logger.log_info(f"[LOAD_BALANCER] ❌ 候选Token均不可用 (图片生成={for_image_generation}, 视频生成={for_video_generation})")
        diagnostics["post_check_failures"] = {str(tid): reason for tid, reason in post_check_failures.items()}
        diagnostics["post_check_failure_summary"] = self._summarize_reasons(post_check_failures)
        debug_logger.log_info(
            "[LOAD_BALANCER] final failure diagnostics: "
            f"filtered_summary={diagnostics['filtered_reason_summary']}, "
            f"post_check_summary={diagnostics['post_check_failure_summary']}"
        )
        self._store_selection_diagnostics(diagnostics, diagnostics_sink)
        return None

    async def get_unavailable_reason(
        self,
        *,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
    ) -> Optional[str]:
        """给出更明确的“无可用账号”原因，优先用于分辨率/tier 档位提示。"""
        active_tokens = await self.token_manager.get_active_tokens()
        if not active_tokens:
            return None

        required_tier = get_required_paygate_tier_for_model(model)
        supported_tokens = []
        for token in active_tokens:
            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            if model and not supports_model_for_tier(model, normalized_tier):
                continue
            supported_tokens.append(token)

        if model and not supported_tokens:
            tier_label = get_paygate_tier_label(required_tier)
            return f"当前模型需要 {tier_label} 账号，但没有可用的 {tier_label} 账号: {model}"

        capability_tokens = []
        for token in supported_tokens:
            if for_image_generation and not token.image_enabled:
                continue
            if for_video_generation and not token.video_enabled:
                continue
            capability_tokens.append(token)

        if supported_tokens and not capability_tokens:
            if for_image_generation:
                return "当前有符合档位的账号，但图片生成功能已全部禁用。"
            if for_video_generation:
                return "当前有符合档位的账号，但视频生成功能已全部禁用。"

        return None
