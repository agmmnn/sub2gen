"""Application startup and shutdown composition."""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI

from ..core.config import config
from ..core.logger import debug_logger
from ..core.monitoring import set_event_loop_lag
from ..services.browser_metrics_cleanup import cleanup_browser_metrics
from ..services.browser_profile_service import BrowserProfileService
from ..services.st_refresh_reasons import describe_st_refresh_reason
from .container import AppContainer

REQUEST_LOG_RETENTION_DAYS = 7
REQUEST_LOG_CLEANUP_INTERVAL_SECONDS = 12 * 3600


def build_lifespan(
    *,
    api_only_hostnames: Callable[[], set[str]],
    init_database_with_storage_recovery: Callable[..., Awaitable[None]],
    abort_refresh_batch_on_resource_exhaustion: Callable[..., Awaitable[bool]],
) -> Callable[[FastAPI], Any]:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Application lifespan manager"""
        container: AppContainer = app.state.container
        db = container.db
        flow_client = container.flow_client
        token_manager = container.token_manager
        concurrency_manager = container.concurrency_manager
        generation_handler = container.generation_handler
        geminigen_service = container.geminigen_service
        google_drive_backup_service = container.google_drive_backup_service
        redis_runtime = container.redis_runtime
        failed_payload_manager = container.failed_payload_manager
        tasks = container.tasks

        # Startup
        print("=" * 60)
        print("sub2gen Starting...")
        api_only = api_only_hostnames()
        if api_only:
            print(f"API-only host(s) (no web UI on these hosts): {', '.join(sorted(api_only))}")
        print("=" * 60)

        # BrowserMetrics is disposable Chromium telemetry. Reclaim it before SQLite
        # touches a persistent volume that may already be full.
        startup_metrics = await asyncio.to_thread(cleanup_browser_metrics)
        if startup_metrics.removed_directories or startup_metrics.failures:
            print(
                "BrowserMetrics startup cleanup: "
                f"removed={startup_metrics.removed_directories}, "
                f"reclaimed={startup_metrics.reclaimed_bytes} bytes, "
                f"skipped_active={startup_metrics.skipped_active_profiles}, "
                f"failures={startup_metrics.failures}"
            )

        # Get config from setting.toml
        config_dict = config.get_raw_config()

        # Check if database exists (determine if first startup)
        is_first_startup = not db.db_exists()
        db.enable_persistent_connections()

        # Initialize database tables/configuration, reclaiming generated cache once on storage pressure.
        await init_database_with_storage_recovery(
            db,
            generation_handler.file_cache,
            config_dict=config_dict,
            is_first_startup=is_first_startup,
        )
        await db.cache_schema_capabilities()
        reconciled_jobs = await container.generation_audit.reconcile_non_resumable_jobs()
        if reconciled_jobs:
            print(f"WARN Marked {reconciled_jobs} interrupted browser-backed generation job(s) as failed")
        db.set_event_runtime(redis_runtime)
        redis_warm = await redis_runtime.start(db)

        # 启动时统一把数据库配置同步到内存，避免 personal/brower 相关运行时配置遗漏。
        await db.reload_config_to_memory()
        generation_handler.file_cache.set_timeout(config.cache_timeout)
        await generation_handler.file_cache.configure_backend(
            config.cache_provider,
            config.cache_delivery_mode,
            validate=config.cache_provider == "digitalocean",
        )
        db.set_log_payload_manager(failed_payload_manager)
        await failed_payload_manager.start(
            db,
            enabled=config.cache_provider == "digitalocean",
        )
        cache_cleanup_enabled = await generation_handler.file_cache.refresh_cleanup_task()
        await google_drive_backup_service.start()
        captcha_config = await db.get_captcha_config()

        # 尽量在浏览器服务启动前就拿到 token 快照，后续并发管理和预热共用。
        tokens = await token_manager.get_all_tokens()

        # Initialize browser captcha service if needed
        browser_service = None
        if captcha_config.captcha_method == "personal":
            from ..services.browser_captcha_personal import (
                BrowserCaptchaService,
                PERSONAL_POOL_MAX_TOTAL_RESIDENT_TABS,
                resolve_effective_browser_count,
                resolve_effective_personal_max_resident_tabs,
            )

            browser_service = await BrowserCaptchaService.get_instance(db)
            print("OK Browser captcha service initialized (nodriver mode)")

            warmup_limit = max(
                1,
                min(
                    PERSONAL_POOL_MAX_TOTAL_RESIDENT_TABS,
                    resolve_effective_browser_count(config.browser_count)
                    * resolve_effective_personal_max_resident_tabs(config.personal_max_resident_tabs),
                ),
            )
            warmup_project_ids = await token_manager.get_personal_warmup_project_ids(
                tokens=tokens,
                limit=warmup_limit,
            )

            warmed_slots = []
            warmup_error = None
            try:
                warmed_slots = await browser_service.warmup_resident_tabs(
                    warmup_project_ids,
                    limit=warmup_limit,
                )
            except Exception as e:
                warmup_error = e
                print(f"WARN Browser captcha resident warmup failed: {type(e).__name__}: {e}")
            if warmed_slots:
                print(
                    f"OK Browser captcha shared resident tabs warmed ({len(warmed_slots)} slot(s), limit={warmup_limit})"
                )
            elif warmup_error is not None:
                print("WARN Browser captcha resident warmup skipped for this startup")
            elif tokens:
                print("WARN Browser captcha resident warmup skipped: no tab warmed successfully")
            else:
                # 没有任何可用 token 时，打开登录窗口供用户手动操作
                await browser_service.open_login_window()
                print("WARN No active token found, opened login window for manual setup")
        elif captcha_config.captcha_method == "browser":
            from ..services.browser_captcha import BrowserCaptchaService

            browser_service = await BrowserCaptchaService.get_instance(db)
            await browser_service.warmup_browser_slots()
            print("Browser captcha service initialized (headed / Playwright pool)")

        # Initialize concurrency manager
        await concurrency_manager.initialize(tokens)
        if redis_runtime.ready:
            print(
                "OK Redis runtime warmed "
                f"(auth_records={redis_warm['auth_records']}, active_tasks={redis_warm['active_tasks']})"
            )

        if config.captcha_method == "remote_browser":
            try:
                warmed_projects = await flow_client.prefill_remote_browser_for_tokens(tokens, action="IMAGE_GENERATION")
                print(f"OK Remote browser pool prefill started for {warmed_projects} project(s)")
            except Exception as e:
                print(f"WARN Remote browser pool prefill failed: {e}")

        # Start 429 auto-unban task

        async def event_loop_lag_task():
            interval = 0.5
            expected = asyncio.get_running_loop().time() + interval
            while True:
                try:
                    await asyncio.sleep(interval)
                    now = asyncio.get_running_loop().time()
                    set_event_loop_lag(max(0.0, now - expected))
                    expected = now + interval
                except asyncio.CancelledError:
                    raise

        tasks.start("event-loop-lag", event_loop_lag_task())

        async def request_log_cleanup_task():
            """Run bounded seven-day cleanup after the maintenance rollout enables it."""
            while True:
                try:
                    retention_enabled = str(
                        os.environ.get("SUB2GEN_RETENTION_ENABLED", "0") or "0"
                    ).strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                    if not retention_enabled:
                        await asyncio.sleep(3600)
                        continue
                    if redis_runtime.maintenance_active:
                        await asyncio.sleep(60)
                        continue
                    totals: dict[str, int] = {}
                    for _ in range(100):
                        batch = await db.cleanup_retention_batch(REQUEST_LOG_RETENTION_DAYS, 500)
                        for key, value in batch.items():
                            totals[key] = totals.get(key, 0) + int(value)
                        if not any(int(value) for value in batch.values()):
                            break
                        await asyncio.sleep(0)
                    totals["spaces_payloads"] = await failed_payload_manager.cleanup_expired(REQUEST_LOG_RETENTION_DAYS)
                    await db.optimize_after_retention()
                    if any(totals.values()):
                        debug_logger.log_info(f"[RETENTION] seven-day cleanup totals={totals}")
                    await asyncio.sleep(REQUEST_LOG_CLEANUP_INTERVAL_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    debug_logger.log_warning(f"[REQUEST_LOG_CLEANUP] task error: {e}")
                    await asyncio.sleep(3600)

        tasks.start("request-log-cleanup", request_log_cleanup_task())

        async def auto_unban_task():
            """定时任务：每小时检查并解禁429被禁用的token"""
            while True:
                try:
                    await asyncio.sleep(3600)  # 每小时执行一次
                    if redis_runtime.maintenance_active:
                        continue
                    await token_manager.auto_unban_429_tokens()
                except Exception as e:
                    print(f"ERR Auto-unban task error: {e}")

        tasks.start("auto-unban", auto_unban_task())

        async def browser_metrics_cleanup_task():
            while True:
                try:
                    await asyncio.sleep(6 * 3600)
                    stats = await asyncio.to_thread(cleanup_browser_metrics)
                    if stats.removed_directories or stats.failures:
                        debug_logger.log_info(
                            "[BrowserMetrics] periodic cleanup "
                            f"removed={stats.removed_directories}, "
                            f"reclaimed={stats.reclaimed_bytes}, "
                            f"skipped_active={stats.skipped_active_profiles}, "
                            f"failures={stats.failures}"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    debug_logger.log_warning(f"[BrowserMetrics] periodic cleanup failed: {type(exc).__name__}")

        tasks.start("browser-metrics-cleanup", browser_metrics_cleanup_task())

        async def scheduled_token_refresh_task():
            """Configurable scheduled token refresh that reuses existing refresh path."""
            while True:
                try:
                    interval_minutes = max(1, int(config.session_refresh_scheduler_interval_minutes))
                    await asyncio.sleep(interval_minutes * 60)
                    if not config.session_refresh_scheduler_enabled:
                        continue
                    if redis_runtime.maintenance_active:
                        continue

                    all_tokens = await token_manager.get_active_tokens()
                    if not all_tokens:
                        continue

                    expiring_within_minutes = max(
                        1,
                        int(config.session_refresh_scheduler_only_expiring_within_minutes),
                    )
                    expiring_window = expiring_within_minutes * 60
                    now = datetime.now(timezone.utc)
                    candidates = []
                    for token in all_tokens:
                        if not token:
                            continue
                        if token.at_expires is None:
                            candidates.append(token)
                            continue
                        exp = token.at_expires
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=timezone.utc)
                        remaining = (exp - now).total_seconds()
                        if remaining <= expiring_window:
                            candidates.append(token)

                    batch_size = max(1, int(config.session_refresh_scheduler_batch_size))
                    for token in candidates[:batch_size]:
                        try:
                            await token_manager._refresh_at(token.id)
                            refresh_reason = token_manager.consume_st_refresh_reason(token.id)
                            if await abort_refresh_batch_on_resource_exhaustion(
                                source="AT_REFRESH",
                                reason=refresh_reason,
                            ):
                                break
                        except Exception as refresh_err:
                            if await abort_refresh_batch_on_resource_exhaustion(
                                source="AT_REFRESH",
                                error=refresh_err,
                            ):
                                break
                            print(f"WARN Scheduled refresh failed for token {token.id}: {refresh_err}")
                except Exception as e:
                    print(f"ERR Scheduled token refresh task error: {e}")

        tasks.start("scheduled-token-refresh", scheduled_token_refresh_task())

        async def scheduled_st_only_refresh_task():
            """ST-only refresh scheduler.

            For each active token whose at_expires is within X minutes (or already
            expired / unknown), pull a fresh __Secure-next-auth.session-token from
            the bound extension worker (or local headed browser fallback) without
            minting a new AT. Per-token in-memory debounce of X minutes prevents
            re-attacking the same token within a single window. Failures are logged
            with the friendly hint from describe_st_refresh_reason; tokens are NOT
            disabled by this scheduler.
            """
            last_attempt: dict[int, float] = {}
            while True:
                try:
                    interval_minutes = max(1, int(config.st_only_refresh_scheduler_interval_minutes))
                    await asyncio.sleep(interval_minutes * 60)
                    if not config.st_only_refresh_scheduler_enabled:
                        continue
                    if redis_runtime.maintenance_active:
                        continue

                    all_tokens = await token_manager.get_active_tokens()
                    if not all_tokens:
                        continue

                    window_minutes = max(1, int(config.st_only_refresh_scheduler_expiring_within_minutes))
                    window_seconds = window_minutes * 60
                    now = datetime.now(timezone.utc)
                    tokens_due = []
                    for tk in all_tokens:
                        if not tk:
                            continue
                        exp = tk.at_expires
                        if exp is None:
                            tokens_due.append(tk)
                            continue
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=timezone.utc)
                        if (exp - now).total_seconds() <= window_seconds:
                            tokens_due.append(tk)

                    debounce_seconds = window_seconds
                    now_ts = now.timestamp()
                    tokens_due = [
                        t for t in tokens_due if (now_ts - last_attempt.get(t.id or 0, 0.0)) >= debounce_seconds
                    ]

                    batch_size = max(1, int(config.st_only_refresh_scheduler_batch_size))
                    for tk in tokens_due[:batch_size]:
                        if tk.id is None:
                            continue
                        last_attempt[tk.id] = now_ts
                        try:
                            ok = await token_manager.refresh_st_only(tk.id)
                            reason = token_manager.consume_st_refresh_reason(tk.id)
                            if ok:
                                debug_logger.log_info(
                                    f"[ST_SCHEDULER] Token {tk.id}: ST refreshed (reason={reason or 'success'})"
                                )
                            else:
                                hint = describe_st_refresh_reason(reason)
                                debug_logger.log_warning(
                                    f"[ST_SCHEDULER] Token {tk.id}: ST refresh failed "
                                    f"(reason={reason or 'unknown'}; {hint or 'no hint'})"
                                )
                            if await abort_refresh_batch_on_resource_exhaustion(
                                source="ST_SCHEDULER",
                                reason=reason,
                            ):
                                break
                        except Exception as refresh_err:
                            if await abort_refresh_batch_on_resource_exhaustion(
                                source="ST_SCHEDULER",
                                error=refresh_err,
                            ):
                                break
                            debug_logger.log_warning(
                                f"[ST_SCHEDULER] Token {tk.id}: scheduled ST refresh raised: {refresh_err}"
                            )
                except Exception as e:
                    debug_logger.log_error(f"[ST_SCHEDULER] task error: {e}")

        tasks.start("scheduled-st-only-refresh", scheduled_st_only_refresh_task())
        resumed_geminigen_tasks = await geminigen_service.resume_active_tasks()

        # Restore/cutover maintenance is cleared only after the new PostgreSQL
        # process has warmed Redis and recovered active tasks.
        if getattr(db, "backend", "sqlite") == "postgres" and redis_runtime.ready:
            raw_restore_status = await redis_runtime.client.get("sub2gen:restore:status")
            if isinstance(raw_restore_status, bytes):
                raw_restore_status = raw_restore_status.decode("utf-8", errors="replace")
            try:
                restore_status = json.loads(raw_restore_status) if raw_restore_status else {}
            except (TypeError, ValueError):
                restore_status = {}
            restore_state = str(restore_status.get("status") or "")
            restore_restart_pending = restore_state in {
                "restart_pending",
                "rollback_restart_pending",
            }
            cutover_restart_pending = redis_runtime.maintenance_reason == "postgres_cutover"
            if restore_restart_pending or cutover_restart_pending:
                database_health = await db.health_snapshot()
                if database_health.get("database_ready"):
                    await redis_runtime.set_maintenance(False, reason="readiness_verified")
                    if restore_status:
                        restore_status.update(
                            status=(
                                "rollback_completed" if restore_state == "rollback_restart_pending" else "completed"
                            ),
                            readiness_verified_at=datetime.now(timezone.utc).isoformat(),
                        )
                        await redis_runtime.client.set(
                            "sub2gen:restore:status",
                            json.dumps(restore_status, separators=(",", ":")),
                            ex=7 * 24 * 3600,
                        )

        if not redis_runtime.maintenance_active:
            token_manager.start_protocol_refresher()

        print("OK Database initialized")
        print(f"OK Total tokens: {len(tokens)}")
        ct = config.cache_timeout
        d = f", ~{ct / 86400.0:.3g}d" if ct and ct > 0 else " (no auto-expiry)"
        print(f"OK Cache: {'Enabled' if config.cache_enabled else 'Disabled'} (timeout: {ct}s{d})")
        if cache_cleanup_enabled:
            print("OK File cache cleanup task started")
        else:
            print("WARN File cache cleanup task failed to start")
        print("OK 429 auto-unban task started (runs every hour)")
        print("OK BrowserMetrics cleanup task started (runs every 6 hours)")
        print(f"OK Request log cleanup task started (retention: {REQUEST_LOG_RETENTION_DAYS} days)")
        print("OK Scheduled token refresh task started")
        print("OK Scheduled ST-only refresh task started")
        print("OK Protocol token refresh task started")
        if resumed_geminigen_tasks:
            print(f"OK GeminiGen active task resume started ({resumed_geminigen_tasks} task(s))")
        print(f"OK Server running on http://{config.server_host}:{config.server_port}")
        print("=" * 60)

        yield

        # Shutdown
        print("sub2gen Shutting down...")
        # Stop file cache cleanup task
        await generation_handler.file_cache.stop_cleanup_task()
        await google_drive_backup_service.stop()
        await tasks.cancel_all()
        await token_manager.stop_protocol_refresher()
        await failed_payload_manager.stop()
        await redis_runtime.stop()
        profile_service = BrowserProfileService.get_existing_instance()
        if profile_service is not None:
            closed_profiles = await profile_service.close_all()
            print(f"OK Browser profile service closed ({closed_profiles} runtime(s))")
        # Close browser if initialized
        if browser_service:
            await browser_service.close()
            print("OK Browser captcha service closed")
        await db.close_runtime_connections()
        print("OK File cache cleanup task stopped")
        print("OK Request log cleanup task stopped")
        print("OK 429 auto-unban task stopped")
        print("OK BrowserMetrics cleanup task stopped")
        print("OK Scheduled token refresh task stopped")
        print("OK Scheduled ST-only refresh task stopped")
        print("OK Protocol token refresh task stopped")

    return lifespan
