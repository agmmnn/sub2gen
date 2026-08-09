"""Database storage layer for sub2gen"""

import asyncio
import aiosqlite
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional, List, Dict, Any, Tuple, Set
from pathlib import Path
from .config import (
    DEFAULT_CAPMONSTER_MIN_SCORE,
    DEFAULT_YESCAPTCHA_TASK_TYPE,
    get_runtime_data_dir,
    normalize_capmonster_min_score,
    normalize_yescaptcha_task_type,
)
from .runway_manifest import RUNWAY_MANIFEST_VERSION, RUNWAY_MODEL_MANIFEST
from .storage_errors import (
    is_sqlite_recoverable_storage_error,
    is_sqlite_storage_full_error,
)
from .models import (
    Token,
    TokenStats,
    Task,
    RequestLog,
    AdminConfig,
    ProxyConfig,
    GenerationConfig,
    CacheConfig,
    Project,
    CaptchaConfig,
    PluginConfig,
    CallLogicConfig,
    DebugConfig,
    RunwayConfig,
    RunwayAccount,
    RunwayModel,
    RunwayTask,
    GeminiGenConfig,
    GeminiGenAccount,
    GeminiGenTask,
    TokenRefreshConfig,
)
from ..persistence.migrations.sqlite import (
    discover_sqlite_migrations,
    prepare_sqlite_migrations,
    stamp_compatible_sqlite_database,
)


class _DedicatedWorkerUpdateOmit:
    """Sentinel: PATCH / internal call did not supply this column — skip in UPDATE."""

    __slots__ = ()


_DEDICATED_WORKER_UPDATE_OMIT = _DedicatedWorkerUpdateOmit()


def derive_adobe_ints_from_scopes_csv(scopes_csv: str) -> Tuple[int, int, int]:
    """Sync api_keys.adobe_* columns from comma-separated scope tokens (single source: scopes)."""
    parts = {x.strip() for x in (scopes_csv or "").split(",") if x.strip()}
    return (
        1 if "adobe:cloning" in parts else 0,
        1 if "adobe:metadata" in parts else 0,
        1 if "adobe:tracker" in parts else 0,
    )


RUNWAY_DEFAULT_MODELS = RUNWAY_MODEL_MANIFEST

RUNWAY_AVAILABILITY_ALIASES_BY_TASK_TYPE = {
    "gemini_3_1_flash_image": ["workflow_gemini_3_1_flash_image"],
    "kling_3_0_pro": ["workflow_kling_3_0_pro_direct", "workflow_kling_3_0_pro_generic"],
    "seedance_2": ["workflow_seedance_2", "seedance_2_fast"],
    "magnific_precision_upscaler_v2": ["workflow_magnific_precision_upscaler_v2"],
    "media_upscale": ["magnific_video_upscaler_creative", "workflow_bytedance_upscaler_1"],
    "sora": ["workflow_sora2", "workflow_sora2_pro"],
    "wan": ["workflow_wan_2_6", "workflow_wan_2_2_animate", "workflow_wan_2_6_flash"],
    "grok_video": ["workflow_xai_image_generation", "workflow_xai_image_edit"],
}


def _runway_feature_sections(live_features: Dict[str, Any]) -> List[Dict[str, Any]]:
    sections: List[Dict[str, Any]] = []
    if isinstance(live_features, dict):
        sections.append(live_features)
        nested = live_features.get("features")
        if isinstance(nested, dict):
            sections.append(nested)
    return sections


def _runway_disabled_value(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "disabled"}:
        return True
    if isinstance(value, dict):
        enabled = value.get("enabled")
        if enabled is False:
            return True
        if isinstance(enabled, str) and enabled.strip().lower() in {"false", "disabled"}:
            return True
    return False


def _extract_runway_disabled_features(live_features: Dict[str, Any]) -> Tuple[Dict[str, str], int]:
    disabled: Dict[str, str] = {}
    disabled_task_types: Set[str] = set()
    for section in _runway_feature_sections(live_features):
        for key in ("disabledTaskTypes", "disabled_task_types", "disabledTasks", "disabled_tasks"):
            value = section.get(key)
            if not isinstance(value, list):
                continue
            for item in value:
                reason = "runway_disabled"
                task_type: Optional[str] = None
                if isinstance(item, dict):
                    task_type = str(item.get("taskType") or item.get("task_type") or item.get("id") or "").strip()
                    reason = str(item.get("reason") or reason)
                elif item is not None:
                    task_type = str(item).strip()
                if task_type and task_type not in disabled:
                    disabled[task_type] = reason
                if task_type:
                    disabled_task_types.add(task_type)

        permitted = section.get("permitted")
        if isinstance(permitted, dict):
            for key, value in permitted.items():
                feature_key = str(key).strip()
                if feature_key and _runway_disabled_value(value) and feature_key not in disabled:
                    disabled[feature_key] = "permission_disabled"
    return disabled, len(disabled_task_types)


def _runway_model_availability_keys(model: Dict[str, Any]) -> Set[str]:
    keys: Set[str] = set()
    for value in (model.get("task_type"), model.get("cost_feature")):
        if value:
            text = str(value)
            keys.add(text)
            keys.update(RUNWAY_AVAILABILITY_ALIASES_BY_TASK_TYPE.get(text, []))
    for field in ("feature_flags", "availability_task_types"):
        values = model.get(field)
        if isinstance(values, list):
            keys.update(str(item) for item in values if str(item or "").strip())
    return keys


def _runway_json_list(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item or "").strip()]
    try:
        parsed = json.loads(str(raw or "[]"))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item or "").strip()]


class Database:
    """SQLite database manager"""

    backend = "sqlite"

    def __init__(self, db_path: str = None):
        if db_path is None:
            data_dir = get_runtime_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(data_dir / "sub2gen.db")
        self.db_path = db_path
        self._write_lock = asyncio.Lock()
        self._connect_timeout = 30
        self._busy_timeout_ms = 30000
        self._writer_connection = None
        self._persistent_connections_enabled = False
        self._read_pool: Optional[asyncio.Queue] = None
        self._read_pool_size = max(1, min(int(os.environ.get("SUB2GEN_SQLITE_READ_POOL_SIZE", "3") or 3), 8))
        self._connection_init_lock = asyncio.Lock()
        self._writer_wait_samples = deque(maxlen=2048)
        self._writer_wait_total = 0.0
        self._writer_wait_count = 0
        self._schema_capabilities: Dict[str, Set[str]] = {}
        self.event_runtime = None
        self.log_payload_manager = None
        self._last_progress_persist: Dict[str, float] = {}
        self._last_log_progress_persist: Dict[int, float] = {}
        self.database_revision = ""

    def db_exists(self) -> bool:
        """Check if database file exists"""
        return Path(self.db_path).exists()

    async def _configure_connection(self, db):
        """Apply SQLite runtime settings for better concurrent behavior."""
        await db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        await db.execute("PRAGMA foreign_keys = ON")

    async def _configure_write_pragmas(self, db):
        """Prefer WAL, but fall back when the mounted volume cannot create WAL files."""
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")
            await db.execute("PRAGMA auto_vacuum = INCREMENTAL")
        except Exception as exc:
            if not is_sqlite_recoverable_storage_error(exc):
                raise
            print(f"WARN SQLite WAL mode unavailable during startup ({exc}); falling back to DELETE journal mode.")
            await db.execute("PRAGMA journal_mode = DELETE")
            await db.execute("PRAGMA synchronous = FULL")

    def _current_stats_date(self) -> str:
        """Return the logical date used by daily token statistics."""
        return date.today().isoformat()

    @asynccontextmanager
    async def _connect(self, *, write: bool = False):
        """Borrow a configured persistent SQLite connection."""
        if not self._persistent_connections_enabled:
            if write:
                wait_started = time.perf_counter()
                async with self._write_lock:
                    waited = time.perf_counter() - wait_started
                    self._writer_wait_samples.append(waited)
                    self._writer_wait_total += waited
                    self._writer_wait_count += 1
                    async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
                        await self._configure_connection(db)
                        yield db
                return
            async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
                await self._configure_connection(db)
                yield db
            return
        if write:
            wait_started = time.perf_counter()
            await self._write_lock.acquire()
            waited = time.perf_counter() - wait_started
            self._writer_wait_samples.append(waited)
            self._writer_wait_total += waited
            self._writer_wait_count += 1
            db = None
            try:
                db = await self._get_writer_connection()
                yield db
            except Exception:
                if db is not None:
                    try:
                        await db.rollback()
                    except Exception:
                        await self._discard_writer_connection()
                raise
            finally:
                if db is not None and getattr(db, "in_transaction", False):
                    try:
                        await db.rollback()
                    except Exception:
                        await self._discard_writer_connection()
                self._write_lock.release()
            return

        db = await self._borrow_read_connection()
        try:
            yield db
        finally:
            if getattr(db, "in_transaction", False):
                try:
                    await db.rollback()
                except Exception:
                    pass
            if self._read_pool is not None:
                await self._read_pool.put(db)

    async def _get_writer_connection(self):
        if self._writer_connection is not None:
            return self._writer_connection
        async with self._connection_init_lock:
            if self._writer_connection is None:
                self._writer_connection = await aiosqlite.connect(
                    self.db_path,
                    timeout=self._connect_timeout,
                )
                await self._configure_connection(self._writer_connection)
            return self._writer_connection

    async def _borrow_read_connection(self):
        if self._read_pool is None:
            async with self._connection_init_lock:
                if self._read_pool is None:
                    pool: asyncio.Queue = asyncio.Queue(maxsize=self._read_pool_size)
                    for _ in range(self._read_pool_size):
                        connection = await aiosqlite.connect(
                            self.db_path,
                            timeout=self._connect_timeout,
                        )
                        await self._configure_connection(connection)
                        await pool.put(connection)
                    self._read_pool = pool
        return await self._read_pool.get()

    async def _discard_writer_connection(self) -> None:
        connection, self._writer_connection = self._writer_connection, None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass

    async def close_runtime_connections(self) -> None:
        """Close persistent connections before shutdown or an atomic database swap."""
        self._schema_capabilities = {}
        async with self._write_lock:
            await self._discard_writer_connection()
            pool, self._read_pool = self._read_pool, None
            if pool is not None:
                while not pool.empty():
                    connection = await pool.get()
                    try:
                        await connection.close()
                    except Exception:
                        pass

    def enable_persistent_connections(self) -> None:
        self._persistent_connections_enabled = True

    def runtime_metrics(self) -> Dict[str, float]:
        samples = sorted(self._writer_wait_samples)
        p95_index = max(0, min(len(samples) - 1, int(len(samples) * 0.95))) if samples else 0
        return {
            "writer_wait_seconds_total": float(self._writer_wait_total),
            "writer_wait_count": float(self._writer_wait_count),
            "writer_wait_seconds_p95": float(samples[p95_index]) if samples else 0.0,
            "writer_wait_seconds_max": float(samples[-1]) if samples else 0.0,
        }

    async def health_snapshot(self) -> Dict[str, Any]:
        """Return backend-specific health and revision details."""
        started = time.perf_counter()
        revision = self.database_revision
        if not revision:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT revision FROM schema_migrations ORDER BY revision DESC LIMIT 1"
                )
                row = await cursor.fetchone()
                revision = str(row[0]) if row else ""
        return {
            "database_backend": "sqlite",
            "database_ready": True,
            "database_revision": revision,
            "cutover_marker_present": False,
            "restore_marker": None,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "database_size_bytes": Path(self.db_path).stat().st_size if Path(self.db_path).exists() else 0,
            "pool": self.runtime_metrics(),
        }

    def set_event_runtime(self, runtime: Any) -> None:
        self.event_runtime = runtime

    def set_log_payload_manager(self, manager: Any) -> None:
        self.log_payload_manager = manager

    async def cache_schema_capabilities(self) -> None:
        capabilities: Dict[str, Set[str]] = {}
        async with self._connect() as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            for row in await cursor.fetchall():
                table_name = str(row[0])
                info_cursor = await db.execute(f"PRAGMA table_info({table_name})")
                capabilities[table_name] = {str(col[1]) for col in await info_cursor.fetchall()}
        self._schema_capabilities = capabilities

    async def _table_exists(self, db, table_name: str) -> bool:
        """Check if a table exists in the database"""
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        result = await cursor.fetchone()
        return result is not None

    async def _column_exists(self, db, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table"""
        cached = self._schema_capabilities.get(table_name)
        if cached is not None:
            return column_name in cached
        try:
            cursor = await db.execute(f"PRAGMA table_info({table_name})")
            columns = await cursor.fetchall()
            return any(col[1] == column_name for col in columns)
        except:
            return False

    async def _ensure_runway_tables(self, db) -> None:
        """Create Runway integration tables and starter model registry rows."""
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runway_config (
                id INTEGER PRIMARY KEY DEFAULT 1,
                enabled BOOLEAN DEFAULT 0,
                base_url TEXT DEFAULT 'https://api.runwayml.com/v1',
                poll_interval_sec REAL DEFAULT 3.0,
                timeout_sec REAL DEFAULT 600.0,
                cache_outputs BOOLEAN DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runway_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT DEFAULT '',
                raw_credential TEXT DEFAULT '',
                is_active BOOLEAN DEFAULT 1,
                workspace_id TEXT,
                team_id TEXT,
                concurrency_limit INTEGER DEFAULT 1,
                in_flight INTEGER DEFAULT 0,
                last_status TEXT,
                last_error TEXT,
                last_used_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runway_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_model_id TEXT UNIQUE NOT NULL,
                display_name TEXT DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'image',
                task_type TEXT NOT NULL,
                builder_key TEXT DEFAULT '',
                default_options TEXT DEFAULT '{}',
                request_mapping TEXT DEFAULT '{}',
                capability_schema TEXT DEFAULT '{}',
                media_roles TEXT DEFAULT '[]',
                supported_modes TEXT DEFAULT '[]',
                limits TEXT DEFAULT '{}',
                feature_flags TEXT DEFAULT '[]',
                cost_feature TEXT DEFAULT '',
                source_version TEXT DEFAULT '',
                live_available BOOLEAN DEFAULT 1,
                disabled_reason TEXT DEFAULT '',
                last_synced_at TIMESTAMP,
                is_enabled BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runway_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                upstream_task_id TEXT,
                account_id INTEGER,
                api_key_id INTEGER,
                public_model_id TEXT NOT NULL,
                prompt TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'processing',
                progress INTEGER DEFAULT 0,
                raw_artifact_urls TEXT,
                cached_artifact_urls TEXT,
                request_payload TEXT,
                response_payload TEXT,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES runway_accounts(id) ON DELETE SET NULL,
                FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
            )
        """)
        runway_model_columns = [
            ("builder_key", "TEXT DEFAULT ''"),
            ("capability_schema", "TEXT DEFAULT '{}'"),
            ("media_roles", "TEXT DEFAULT '[]'"),
            ("supported_modes", "TEXT DEFAULT '[]'"),
            ("limits", "TEXT DEFAULT '{}'"),
            ("feature_flags", "TEXT DEFAULT '[]'"),
            ("cost_feature", "TEXT DEFAULT ''"),
            ("source_version", "TEXT DEFAULT ''"),
            ("live_available", "BOOLEAN DEFAULT 1"),
            ("disabled_reason", "TEXT DEFAULT ''"),
            ("last_synced_at", "TIMESTAMP"),
        ]
        for column_name, column_type in runway_model_columns:
            if not await self._column_exists(db, "runway_models", column_name):
                await db.execute(f"ALTER TABLE runway_models ADD COLUMN {column_name} {column_type}")
        await db.execute("INSERT OR IGNORE INTO runway_config (id) VALUES (1)")
        for model in RUNWAY_DEFAULT_MODELS:
            live_available = model.get("builder_key") != "unsupported"
            disabled_reason = model.get("disabled_reason") or (
                "" if live_available else "Exact task builder is not available"
            )
            await db.execute(
                """
                INSERT INTO runway_models (
                    public_model_id, display_name, kind, task_type, builder_key,
                    default_options, request_mapping, capability_schema, media_roles,
                    supported_modes, limits, feature_flags, cost_feature, source_version,
                    live_available, disabled_reason, last_synced_at, is_enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(public_model_id) DO UPDATE SET
                    display_name = CASE WHEN runway_models.builder_key = '' THEN excluded.display_name ELSE runway_models.display_name END,
                    kind = CASE WHEN runway_models.builder_key = '' THEN excluded.kind ELSE runway_models.kind END,
                    task_type = CASE WHEN runway_models.builder_key = '' THEN excluded.task_type ELSE runway_models.task_type END,
                    builder_key = CASE WHEN runway_models.builder_key = '' THEN excluded.builder_key ELSE runway_models.builder_key END,
                    default_options = CASE WHEN runway_models.builder_key = '' THEN excluded.default_options ELSE runway_models.default_options END,
                    request_mapping = CASE WHEN runway_models.builder_key = '' THEN excluded.request_mapping ELSE runway_models.request_mapping END,
                    capability_schema = CASE WHEN runway_models.capability_schema = '{}' THEN excluded.capability_schema ELSE runway_models.capability_schema END,
                    media_roles = CASE WHEN runway_models.media_roles = '[]' THEN excluded.media_roles ELSE runway_models.media_roles END,
                    supported_modes = CASE WHEN runway_models.supported_modes = '[]' THEN excluded.supported_modes ELSE runway_models.supported_modes END,
                    limits = CASE WHEN runway_models.limits = '{}' THEN excluded.limits ELSE runway_models.limits END,
                    feature_flags = CASE WHEN runway_models.feature_flags = '[]' THEN excluded.feature_flags ELSE runway_models.feature_flags END,
                    cost_feature = CASE WHEN runway_models.cost_feature = '' THEN excluded.cost_feature ELSE runway_models.cost_feature END,
                    source_version = CASE WHEN runway_models.source_version = '' THEN excluded.source_version ELSE runway_models.source_version END,
                    live_available = CASE WHEN runway_models.source_version = '' THEN excluded.live_available ELSE runway_models.live_available END,
                    disabled_reason = CASE WHEN runway_models.disabled_reason = '' THEN excluded.disabled_reason ELSE runway_models.disabled_reason END,
                    last_synced_at = COALESCE(runway_models.last_synced_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    model["public_model_id"],
                    model["display_name"],
                    model["kind"],
                    model["task_type"],
                    model.get("builder_key", ""),
                    json.dumps(model["default_options"], ensure_ascii=False),
                    json.dumps(model.get("request_mapping", {}), ensure_ascii=False),
                    json.dumps(model.get("option_schema", {}), ensure_ascii=False),
                    json.dumps(model.get("media_roles", []), ensure_ascii=False),
                    json.dumps(model.get("supported_modes", []), ensure_ascii=False),
                    json.dumps(model.get("limits", {}), ensure_ascii=False),
                    json.dumps(model.get("feature_flags", []), ensure_ascii=False),
                    model.get("cost_feature", ""),
                    RUNWAY_MANIFEST_VERSION,
                    int(bool(live_available)),
                    disabled_reason,
                    int(bool(model.get("default_enabled", True) and live_available)),
                ),
            )

    async def _ensure_geminigen_tables(self, db) -> None:
        """Create GeminiGen integration tables."""
        await db.execute("""
            CREATE TABLE IF NOT EXISTS geminigen_config (
                id INTEGER PRIMARY KEY DEFAULT 1,
                enabled BOOLEAN DEFAULT 0,
                video_enabled BOOLEAN DEFAULT 1,
                base_url TEXT DEFAULT 'https://api.geminigen.ai',
                poll_interval_image_sec REAL DEFAULT 3.0,
                poll_interval_video_sec REAL DEFAULT 12.0,
                timeout_image_sec REAL DEFAULT 600.0,
                timeout_video_sec REAL DEFAULT 1800.0,
                global_image_concurrency INTEGER DEFAULT 5,
                global_video_concurrency INTEGER DEFAULT 5,
                cache_outputs BOOLEAN DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS geminigen_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT DEFAULT '',
                raw_cookie TEXT DEFAULT '',
                bearer_token TEXT DEFAULT '',
                refresh_token TEXT DEFAULT '',
                guard_id TEXT DEFAULT '',
                turnstile_token TEXT DEFAULT '',
                is_active BOOLEAN DEFAULT 1,
                image_concurrency INTEGER DEFAULT 5,
                video_concurrency INTEGER DEFAULT 5,
                image_in_flight INTEGER DEFAULT 0,
                video_in_flight INTEGER DEFAULT 0,
                image_gen_daily_limit_reset_at TIMESTAMP,
                grok_image_daily_limit_reset_at TIMESTAMP,
                video_daily_limit_reset_at TIMESTAMP,
                last_status TEXT,
                last_error TEXT,
                last_used_at TIMESTAMP,
                profile_user_id INTEGER,
                profile_uuid TEXT,
                profile_email TEXT,
                profile_full_name TEXT,
                profile_is_active BOOLEAN,
                available_credit INTEGER,
                plan_credit INTEGER,
                purchased_credit INTEGER,
                locked_credit INTEGER,
                subscription_credit INTEGER,
                plan_name TEXT,
                plan_expire_at TIMESTAMP,
                active_benefits_json TEXT,
                is_image_gen_max BOOLEAN,
                is_image_premium BOOLEAN,
                image_gen_concurrent_streams INTEGER,
                remaining_image_gen_max_daily_images INTEGER,
                remaining_image_gen_free_daily_images INTEGER,
                image_gen_quota_synced_at TIMESTAMP,
                is_grok_image_max BOOLEAN,
                grok_image_concurrent_streams INTEGER,
                remaining_grok_image_max_daily_images INTEGER,
                remaining_grok_image_free_daily_images INTEGER,
                grok_image_quota_synced_at TIMESTAMP,
                is_grok_max BOOLEAN,
                grok_max_concurrent_streams INTEGER,
                grok_max_quota_synced_at TIMESTAMP,
                remaining_bulk_videos INTEGER,
                remaining_daily_videos INTEGER,
                remaining_grok_max_daily_videos INTEGER,
                remaining_grok_max_daily_720p_videos INTEGER,
                remaining_grok_max_daily_10s_videos INTEGER,
                remaining_grok_max_daily_15s_videos INTEGER,
                quota_synced_at TIMESTAMP,
                quota_sync_status TEXT,
                quota_sync_error TEXT,
                profile_synced_at TIMESTAMP,
                profile_sync_status TEXT,
                profile_sync_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS geminigen_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                upstream_uuid TEXT,
                account_id INTEGER,
                api_key_id INTEGER,
                request_log_id INTEGER,
                public_model_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'image',
                endpoint_type TEXT NOT NULL DEFAULT 'imagen',
                prompt TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER DEFAULT 0,
                raw_artifact_urls TEXT,
                cached_artifact_urls TEXT,
                request_payload TEXT,
                response_payload TEXT,
                error_message TEXT,
                error_code TEXT,
                retry_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES geminigen_accounts(id),
                FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
            )
        """)
        account_columns = [
            ("bearer_token", "TEXT DEFAULT ''"),
            ("refresh_token", "TEXT DEFAULT ''"),
            ("guard_id", "TEXT DEFAULT ''"),
            ("turnstile_token", "TEXT DEFAULT ''"),
            ("image_concurrency", "INTEGER DEFAULT 5"),
            ("video_concurrency", "INTEGER DEFAULT 5"),
            ("image_in_flight", "INTEGER DEFAULT 0"),
            ("video_in_flight", "INTEGER DEFAULT 0"),
            ("image_gen_daily_limit_reset_at", "TIMESTAMP"),
            ("grok_image_daily_limit_reset_at", "TIMESTAMP"),
            ("video_daily_limit_reset_at", "TIMESTAMP"),
            ("profile_user_id", "INTEGER"),
            ("profile_uuid", "TEXT"),
            ("profile_email", "TEXT"),
            ("profile_full_name", "TEXT"),
            ("profile_is_active", "BOOLEAN"),
            ("available_credit", "INTEGER"),
            ("plan_credit", "INTEGER"),
            ("purchased_credit", "INTEGER"),
            ("locked_credit", "INTEGER"),
            ("subscription_credit", "INTEGER"),
            ("plan_name", "TEXT"),
            ("plan_expire_at", "TIMESTAMP"),
            ("active_benefits_json", "TEXT"),
            ("is_image_gen_max", "BOOLEAN"),
            ("is_image_premium", "BOOLEAN"),
            ("image_gen_concurrent_streams", "INTEGER"),
            ("remaining_image_gen_max_daily_images", "INTEGER"),
            ("remaining_image_gen_free_daily_images", "INTEGER"),
            ("image_gen_quota_synced_at", "TIMESTAMP"),
            ("is_grok_image_max", "BOOLEAN"),
            ("grok_image_concurrent_streams", "INTEGER"),
            ("remaining_grok_image_max_daily_images", "INTEGER"),
            ("remaining_grok_image_free_daily_images", "INTEGER"),
            ("grok_image_quota_synced_at", "TIMESTAMP"),
            ("is_grok_max", "BOOLEAN"),
            ("grok_max_concurrent_streams", "INTEGER"),
            ("grok_max_quota_synced_at", "TIMESTAMP"),
            ("remaining_bulk_videos", "INTEGER"),
            ("remaining_daily_videos", "INTEGER"),
            ("remaining_grok_max_daily_videos", "INTEGER"),
            ("remaining_grok_max_daily_720p_videos", "INTEGER"),
            ("remaining_grok_max_daily_10s_videos", "INTEGER"),
            ("remaining_grok_max_daily_15s_videos", "INTEGER"),
            ("quota_synced_at", "TIMESTAMP"),
            ("quota_sync_status", "TEXT"),
            ("quota_sync_error", "TEXT"),
            ("profile_synced_at", "TIMESTAMP"),
            ("profile_sync_status", "TEXT"),
            ("profile_sync_error", "TEXT"),
        ]
        for column_name, column_type in account_columns:
            if not await self._column_exists(db, "geminigen_accounts", column_name):
                await db.execute(f"ALTER TABLE geminigen_accounts ADD COLUMN {column_name} {column_type}")
        if await self._column_exists(db, "geminigen_accounts", "image_daily_limit_reset_at"):
            await db.execute(
                """
                UPDATE geminigen_accounts
                SET image_gen_daily_limit_reset_at = COALESCE(
                    image_gen_daily_limit_reset_at,
                    image_daily_limit_reset_at
                )
                WHERE image_daily_limit_reset_at IS NOT NULL
                """
            )
        config_columns = [
            ("video_enabled", "BOOLEAN DEFAULT 1"),
            ("global_image_concurrency", "INTEGER DEFAULT 5"),
            ("global_video_concurrency", "INTEGER DEFAULT 5"),
        ]
        for column_name, column_type in config_columns:
            if not await self._column_exists(db, "geminigen_config", column_name):
                await db.execute(f"ALTER TABLE geminigen_config ADD COLUMN {column_name} {column_type}")
        task_columns = [
            ("request_log_id", "INTEGER"),
            ("error_code", "TEXT"),
            ("retry_at", "TIMESTAMP"),
        ]
        for column_name, column_type in task_columns:
            if not await self._column_exists(db, "geminigen_tasks", column_name):
                await db.execute(f"ALTER TABLE geminigen_tasks ADD COLUMN {column_name} {column_type}")
        await db.execute("INSERT OR IGNORE INTO geminigen_config (id) VALUES (1)")

    async def _ensure_config_rows(self, db, config_dict: dict = None):
        """Ensure all config tables have their default rows

        Args:
            db: Database connection
            config_dict: Configuration dictionary from setting.toml (optional)
                        If None, use default values instead of reading from TOML.
        """
        # Ensure admin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM admin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            admin_username = "admin"
            admin_password = "admin"
            api_key = "han1234"
            error_ban_threshold = 3
            error_ban_enabled = 1

            if config_dict:
                global_config = config_dict.get("global", {})
                admin_username = global_config.get("admin_username", "admin")
                admin_password = global_config.get("admin_password", "admin")
                api_key = global_config.get("api_key", "han1234")

                admin_config = config_dict.get("admin", {})
                error_ban_threshold = admin_config.get("error_ban_threshold", 3)
                if "error_ban_enabled" in admin_config:
                    error_ban_enabled = 1 if admin_config.get("error_ban_enabled") else 0

            await db.execute(
                """
                INSERT INTO admin_config (id, username, password, api_key, error_ban_threshold, error_ban_enabled)
                VALUES (1, ?, ?, ?, ?, ?)
            """,
                (admin_username, admin_password, api_key, error_ban_threshold, error_ban_enabled),
            )

        # Ensure proxy_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM proxy_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            proxy_enabled = False
            proxy_url = None
            media_proxy_enabled = False
            media_proxy_url = None

            if config_dict:
                proxy_config = config_dict.get("proxy", {})
                proxy_enabled = proxy_config.get("proxy_enabled", False)
                proxy_url = proxy_config.get("proxy_url", "")
                proxy_url = proxy_url if proxy_url else None
                media_proxy_enabled = proxy_config.get(
                    "media_proxy_enabled", proxy_config.get("image_io_proxy_enabled", False)
                )
                media_proxy_url = proxy_config.get("media_proxy_url", proxy_config.get("image_io_proxy_url", ""))
                media_proxy_url = media_proxy_url if media_proxy_url else None

            await db.execute(
                """
                INSERT INTO proxy_config (id, enabled, proxy_url, media_proxy_enabled, media_proxy_url)
                VALUES (1, ?, ?, ?, ?)
            """,
                (proxy_enabled, proxy_url, media_proxy_enabled, media_proxy_url),
            )

        # Ensure generation_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM generation_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            image_timeout = 300
            video_timeout = 1500
            max_retries = 3
            extension_generation_enabled = False
            extension_generation_fallback_mode = "local_http_on_recaptcha"

            if config_dict:
                generation_config = config_dict.get("generation", {})
                flow_config = config_dict.get("flow", {})
                generation_routing_config = config_dict.get("generation_routing", {})
                image_timeout = generation_config.get("image_timeout", 300)
                video_timeout = generation_config.get("video_timeout", 1500)
                max_retries = flow_config.get("max_retries", 3)
                extension_generation_enabled = bool(
                    generation_routing_config.get("extension_generation_enabled", False)
                )
                mode = (
                    str(
                        generation_routing_config.get(
                            "extension_generation_fallback_mode",
                            "local_http_on_recaptcha",
                        )
                        or ""
                    )
                    .strip()
                    .lower()
                )
                extension_generation_fallback_mode = (
                    mode if mode in {"none", "local_http_on_recaptcha"} else "local_http_on_recaptcha"
                )

            try:
                max_retries = max(1, int(max_retries))
            except Exception:
                max_retries = 3

            await db.execute(
                """
                INSERT INTO generation_config (
                    id,
                    image_timeout,
                    video_timeout,
                    max_retries,
                    extension_generation_enabled,
                    extension_generation_fallback_mode
                )
                VALUES (1, ?, ?, ?, ?, ?)
            """,
                (
                    image_timeout,
                    video_timeout,
                    max_retries,
                    extension_generation_enabled,
                    extension_generation_fallback_mode,
                ),
            )

        # Ensure call_logic_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM call_logic_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            call_mode = "default"
            polling_mode_enabled = False

            if config_dict:
                call_logic_config = config_dict.get("call_logic", {})
                call_mode = call_logic_config.get("call_mode", "default")
                if call_mode not in ("default", "polling"):
                    polling_mode_enabled = call_logic_config.get("polling_mode_enabled", False)
                    call_mode = "polling" if polling_mode_enabled else "default"
                else:
                    polling_mode_enabled = call_mode == "polling"

            await db.execute(
                """
                INSERT INTO call_logic_config (id, call_mode, polling_mode_enabled)
                VALUES (1, ?, ?)
            """,
                (call_mode, polling_mode_enabled),
            )

        # Ensure cache_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM cache_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            cache_enabled = False
            cache_timeout = 7200
            cache_base_url = None
            cache_provider = "local"
            cache_delivery_mode = "proxy"

            if config_dict:
                cache_config = config_dict.get("cache", {})
                cache_enabled = cache_config.get("enabled", False)
                cache_timeout = cache_config.get("timeout", 7200)
                cache_base_url = cache_config.get("base_url", "")
                cache_provider = cache_config.get("provider", "local")
                cache_delivery_mode = cache_config.get("delivery_mode", "proxy")
                # Convert empty string to None
                cache_base_url = cache_base_url if cache_base_url else None

            await db.execute(
                """
                INSERT INTO cache_config (
                    id, cache_enabled, cache_timeout, cache_base_url,
                    cache_provider, cache_delivery_mode
                ) VALUES (1, ?, ?, ?, ?, ?)
            """,
                (
                    cache_enabled,
                    cache_timeout,
                    cache_base_url,
                    cache_provider,
                    cache_delivery_mode,
                ),
            )

        # Ensure debug_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM debug_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            debug_enabled = False
            log_requests = True
            log_responses = True
            mask_token = True

            if config_dict:
                debug_config = config_dict.get("debug", {})
                debug_enabled = debug_config.get("enabled", False)
                log_requests = debug_config.get("log_requests", True)
                log_responses = debug_config.get("log_responses", True)
                mask_token = debug_config.get("mask_token", True)

            await db.execute(
                """
                INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                VALUES (1, ?, ?, ?, ?)
            """,
                (debug_enabled, log_requests, log_responses, mask_token),
            )

        # Ensure captcha_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM captcha_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            captcha_method = "browser"
            yescaptcha_api_key = ""
            yescaptcha_base_url = "https://api.yescaptcha.com"
            yescaptcha_task_type = DEFAULT_YESCAPTCHA_TASK_TYPE
            remote_browser_base_url = ""
            remote_browser_api_key = ""
            remote_browser_timeout = 60
            browser_fallback_to_remote_browser = True
            browser_count = 1
            browser_personal_fresh_restart_every_n_solves = 10
            personal_project_pool_size = 4
            personal_max_resident_tabs = 5
            personal_idle_tab_ttl_seconds = 600
            session_refresh_enabled = True
            session_refresh_browser_first = True
            session_refresh_inject_st_cookie = True
            session_refresh_warmup_urls = "https://labs.google/fx/tools/flow,https://labs.google/fx"
            session_refresh_wait_seconds_per_url = 60
            session_refresh_overall_timeout_seconds = 180
            session_refresh_update_st_from_cookie = True
            session_refresh_fail_if_st_refresh_fails = True
            session_refresh_local_only = True
            session_refresh_scheduler_enabled = False
            session_refresh_scheduler_interval_minutes = 30
            session_refresh_scheduler_batch_size = 10
            session_refresh_scheduler_only_expiring_within_minutes = 60
            extension_queue_wait_timeout_seconds = 20
            dedicated_extension_enabled = False
            dedicated_extension_captcha_timeout_seconds = 25
            dedicated_extension_st_refresh_timeout_seconds = 45

            if config_dict:
                captcha_config = config_dict.get("captcha", {})
                captcha_method = captcha_config.get("captcha_method", "browser")
                yescaptcha_api_key = captcha_config.get("yescaptcha_api_key", "")
                yescaptcha_base_url = captcha_config.get("yescaptcha_base_url", "https://api.yescaptcha.com")
                yescaptcha_task_type = normalize_yescaptcha_task_type(captcha_config.get("yescaptcha_task_type"))
                remote_browser_base_url = captcha_config.get("remote_browser_base_url", "")
                remote_browser_api_key = captcha_config.get("remote_browser_api_key", "")
                remote_browser_timeout = captcha_config.get("remote_browser_timeout", 60)
                browser_fallback_to_remote_browser = captcha_config.get(
                    "browser_fallback_to_remote_browser",
                    True,
                )
                browser_count = captcha_config.get("browser_count", 1)
                browser_personal_fresh_restart_every_n_solves = captcha_config.get(
                    "browser_personal_fresh_restart_every_n_solves",
                    10,
                )
                personal_project_pool_size = captcha_config.get("personal_project_pool_size", 4)
                personal_max_resident_tabs = captcha_config.get("personal_max_resident_tabs", 5)
                personal_idle_tab_ttl_seconds = captcha_config.get("personal_idle_tab_ttl_seconds", 600)
                session_refresh_enabled = captcha_config.get("session_refresh_enabled", True)
                session_refresh_browser_first = captcha_config.get("session_refresh_browser_first", True)
                session_refresh_inject_st_cookie = captcha_config.get("session_refresh_inject_st_cookie", True)
                session_refresh_warmup_urls = captcha_config.get(
                    "session_refresh_warmup_urls",
                    "https://labs.google/fx/tools/flow,https://labs.google/fx",
                )
                session_refresh_wait_seconds_per_url = captcha_config.get("session_refresh_wait_seconds_per_url", 60)
                session_refresh_overall_timeout_seconds = captcha_config.get(
                    "session_refresh_overall_timeout_seconds", 180
                )
                session_refresh_update_st_from_cookie = captcha_config.get(
                    "session_refresh_update_st_from_cookie", True
                )
                session_refresh_fail_if_st_refresh_fails = captcha_config.get(
                    "session_refresh_fail_if_st_refresh_fails", True
                )
                session_refresh_local_only = captcha_config.get("session_refresh_local_only", True)
                session_refresh_scheduler_enabled = captcha_config.get("session_refresh_scheduler_enabled", False)
                session_refresh_scheduler_interval_minutes = captcha_config.get(
                    "session_refresh_scheduler_interval_minutes", 30
                )
                session_refresh_scheduler_batch_size = captcha_config.get("session_refresh_scheduler_batch_size", 10)
                session_refresh_scheduler_only_expiring_within_minutes = captcha_config.get(
                    "session_refresh_scheduler_only_expiring_within_minutes",
                    60,
                )
                extension_queue_wait_timeout_seconds = captcha_config.get(
                    "extension_queue_wait_timeout_seconds",
                    20,
                )
                dedicated_extension_enabled = captcha_config.get("dedicated_extension_enabled", False)
                dedicated_extension_captcha_timeout_seconds = captcha_config.get(
                    "dedicated_extension_captcha_timeout_seconds",
                    25,
                )
                dedicated_extension_st_refresh_timeout_seconds = captcha_config.get(
                    "dedicated_extension_st_refresh_timeout_seconds",
                    45,
                )
            try:
                remote_browser_timeout = max(5, int(remote_browser_timeout))
            except Exception:
                remote_browser_timeout = 60
            try:
                browser_count = max(1, int(browser_count))
            except Exception:
                browser_count = 1
            try:
                browser_personal_fresh_restart_every_n_solves = max(
                    0,
                    int(browser_personal_fresh_restart_every_n_solves),
                )
            except Exception:
                browser_personal_fresh_restart_every_n_solves = 10
            try:
                personal_project_pool_size = max(1, min(50, int(personal_project_pool_size)))
            except Exception:
                personal_project_pool_size = 4
            try:
                personal_max_resident_tabs = max(1, min(50, int(personal_max_resident_tabs)))
            except Exception:
                personal_max_resident_tabs = 5
            try:
                personal_idle_tab_ttl_seconds = max(60, int(personal_idle_tab_ttl_seconds))
            except Exception:
                personal_idle_tab_ttl_seconds = 600

            await db.execute(
                """
                INSERT INTO captcha_config (
                    id, captcha_method, yescaptcha_api_key, yescaptcha_base_url,
                    yescaptcha_task_type,
                    remote_browser_base_url, remote_browser_api_key, remote_browser_timeout,
                    browser_fallback_to_remote_browser,
                    browser_count, browser_personal_fresh_restart_every_n_solves, personal_project_pool_size,
                    personal_max_resident_tabs, personal_idle_tab_ttl_seconds,
                    session_refresh_enabled, session_refresh_browser_first, session_refresh_inject_st_cookie,
                    session_refresh_warmup_urls, session_refresh_wait_seconds_per_url,
                    session_refresh_overall_timeout_seconds, session_refresh_update_st_from_cookie,
                    session_refresh_fail_if_st_refresh_fails, session_refresh_local_only,
                    session_refresh_scheduler_enabled, session_refresh_scheduler_interval_minutes,
                    session_refresh_scheduler_batch_size, session_refresh_scheduler_only_expiring_within_minutes,
                    extension_queue_wait_timeout_seconds,
                    dedicated_extension_enabled, dedicated_extension_captcha_timeout_seconds,
                    dedicated_extension_st_refresh_timeout_seconds
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    captcha_method,
                    yescaptcha_api_key,
                    yescaptcha_base_url,
                    yescaptcha_task_type,
                    remote_browser_base_url,
                    remote_browser_api_key,
                    remote_browser_timeout,
                    bool(browser_fallback_to_remote_browser),
                    browser_count,
                    browser_personal_fresh_restart_every_n_solves,
                    personal_project_pool_size,
                    personal_max_resident_tabs,
                    personal_idle_tab_ttl_seconds,
                    bool(session_refresh_enabled),
                    bool(session_refresh_browser_first),
                    bool(session_refresh_inject_st_cookie),
                    str(session_refresh_warmup_urls or "").strip()
                    or "https://labs.google/fx/tools/flow,https://labs.google/fx",
                    max(0, int(session_refresh_wait_seconds_per_url or 60)),
                    max(10, int(session_refresh_overall_timeout_seconds or 180)),
                    bool(session_refresh_update_st_from_cookie),
                    bool(session_refresh_fail_if_st_refresh_fails),
                    bool(session_refresh_local_only),
                    bool(session_refresh_scheduler_enabled),
                    max(1, int(session_refresh_scheduler_interval_minutes or 30)),
                    max(1, int(session_refresh_scheduler_batch_size or 10)),
                    max(1, int(session_refresh_scheduler_only_expiring_within_minutes or 60)),
                    max(1, min(120, int(extension_queue_wait_timeout_seconds or 20))),
                    bool(dedicated_extension_enabled),
                    max(5, min(180, int(dedicated_extension_captcha_timeout_seconds or 25))),
                    max(10, min(300, int(dedicated_extension_st_refresh_timeout_seconds or 45))),
                ),
            )

        # Ensure plugin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM plugin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            await db.execute("""
                INSERT INTO plugin_config (id, connection_token, auto_enable_on_update)
                VALUES (1, '', 1)
            """)

    async def check_and_migrate_db(self, config_dict: dict = None):
        """Check database integrity and perform migrations if needed

        This method is called during upgrade mode to:
        1. Create missing tables (if they don't exist)
        2. Add missing columns to existing tables
        3. Ensure all config tables have default rows

        Args:
            config_dict: Configuration dictionary from setting.toml (optional)
                        Used only to initialize missing config rows with default values.
                        Existing config rows will NOT be overwritten.
        """
        async with self._connect(write=True) as db:
            migration_state = await prepare_sqlite_migrations(db)
            print("Checking database integrity and performing migrations...")
            await self._configure_write_pragmas(db)
            await self._ensure_runway_tables(db)
            await self._ensure_geminigen_tables(db)

            # ========== Step 1: Create missing tables ==========
            # Check and create cache_config table if missing
            if not await self._table_exists(db, "cache_config"):
                print("  OK Creating missing table: cache_config")
                await db.execute("""
                    CREATE TABLE cache_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        cache_enabled BOOLEAN DEFAULT 0,
                        cache_timeout INTEGER DEFAULT 7200,
                        cache_base_url TEXT,
                        cache_provider TEXT DEFAULT 'local',
                        cache_delivery_mode TEXT DEFAULT 'proxy',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create proxy_config table if missing
            if not await self._table_exists(db, "proxy_config"):
                print("  OK Creating missing table: proxy_config")
                await db.execute("""
                    CREATE TABLE proxy_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        enabled BOOLEAN DEFAULT 0,
                        proxy_url TEXT,
                        media_proxy_enabled BOOLEAN DEFAULT 0,
                        media_proxy_url TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create call_logic_config table if missing
            if not await self._table_exists(db, "call_logic_config"):
                print("  Creating missing table: call_logic_config")
                await db.execute("""
                    CREATE TABLE call_logic_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        call_mode TEXT DEFAULT 'default',
                        polling_mode_enabled BOOLEAN DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create captcha_config table if missing
            if not await self._table_exists(db, "captcha_config"):
                print("  OK Creating missing table: captcha_config")
                await db.execute("""
                    CREATE TABLE captcha_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        captcha_method TEXT DEFAULT 'browser',
                        yescaptcha_api_key TEXT DEFAULT '',
                        yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                        yescaptcha_task_type TEXT DEFAULT 'RecaptchaV3TaskProxylessM1S9',
                        capmonster_api_key TEXT DEFAULT '',
                        capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                        capmonster_min_score REAL DEFAULT 0.9,
                        ezcaptcha_api_key TEXT DEFAULT '',
                        ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                        capsolver_api_key TEXT DEFAULT '',
                        capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                        remote_browser_base_url TEXT DEFAULT '',
                        remote_browser_api_key TEXT DEFAULT '',
                        remote_browser_timeout INTEGER DEFAULT 60,
                        extension_queue_wait_timeout_seconds INTEGER DEFAULT 20,
                        dedicated_extension_enabled BOOLEAN DEFAULT 0,
                        dedicated_extension_captcha_timeout_seconds INTEGER DEFAULT 25,
                        dedicated_extension_st_refresh_timeout_seconds INTEGER DEFAULT 45,
                        website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                        page_action TEXT DEFAULT 'IMAGE_GENERATION',
                        browser_proxy_enabled BOOLEAN DEFAULT 0,
                        browser_proxy_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create plugin_config table if missing
            if not await self._table_exists(db, "plugin_config"):
                print("  OK Creating missing table: plugin_config")
                await db.execute("""
                    CREATE TABLE plugin_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        connection_token TEXT DEFAULT '',
                        auto_enable_on_update BOOLEAN DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            if not await self._table_exists(db, "extension_worker_bindings"):
                print("  OK Creating missing table: extension_worker_bindings")
                await db.execute("""
                    CREATE TABLE extension_worker_bindings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        route_key TEXT NOT NULL UNIQUE,
                        api_key_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                    )
                """)

            if not await self._table_exists(db, "captcha_worker_keys"):
                print("  OK Creating missing table: captcha_worker_keys")
                await db.execute("""
                    CREATE TABLE captcha_worker_keys (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        key_prefix TEXT NOT NULL UNIQUE,
                        key_hash TEXT NOT NULL UNIQUE,
                        label TEXT DEFAULT '',
                        key_plaintext TEXT,
                        is_active BOOLEAN DEFAULT 1,
                        last_instance_id TEXT,
                        last_seen_at TIMESTAMP,
                        last_error TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            if not await self._table_exists(db, "api_clients"):
                print("  OK Creating missing table: api_clients")
                await db.execute("""
                    CREATE TABLE api_clients (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        is_active BOOLEAN DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            if not await self._table_exists(db, "api_keys"):
                print("  OK Creating missing table: api_keys")
                await db.execute("""
                    CREATE TABLE api_keys (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        client_id INTEGER NOT NULL,
                        label TEXT NOT NULL,
                        key_prefix TEXT NOT NULL,
                        key_plaintext TEXT,
                        key_hash TEXT NOT NULL UNIQUE,
                        scopes TEXT DEFAULT '*',
                        is_active BOOLEAN DEFAULT 1,
                        expires_at TIMESTAMP,
                        last_used_at TIMESTAMP,
                        last_presence_at TIMESTAMP,
                        adobe_cloning_enabled INTEGER DEFAULT 1,
                        adobe_metadata_enabled INTEGER DEFAULT 1,
                        adobe_tracker_enabled INTEGER DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (client_id) REFERENCES api_clients(id)
                    )
                """)

            if not await self._table_exists(db, "api_key_accounts"):
                print("  OK Creating missing table: api_key_accounts")
                await db.execute("""
                    CREATE TABLE api_key_accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        api_key_id INTEGER NOT NULL,
                        account_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(api_key_id, account_id),
                        FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                    )
                """)

            if not await self._table_exists(db, "api_key_rate_limits"):
                print("  OK Creating missing table: api_key_rate_limits")
                await db.execute("""
                    CREATE TABLE api_key_rate_limits (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        api_key_id INTEGER NOT NULL,
                        endpoint TEXT NOT NULL,
                        rpm INTEGER DEFAULT 0,
                        rph INTEGER DEFAULT 0,
                        burst INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(api_key_id, endpoint),
                        FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                    )
                """)

            if not await self._table_exists(db, "api_key_audit_logs"):
                print("  OK Creating missing table: api_key_audit_logs")
                await db.execute("""
                    CREATE TABLE api_key_audit_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        api_key_id INTEGER,
                        endpoint TEXT NOT NULL,
                        account_id INTEGER,
                        status_code INTEGER NOT NULL,
                        detail TEXT,
                        ip TEXT,
                        user_agent TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                    )
                """)

            if not await self._table_exists(db, "cache_files"):
                print("  OK Creating missing table: cache_files")
                await db.execute("""
                    CREATE TABLE cache_files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        filename TEXT NOT NULL UNIQUE,
                        api_key_id INTEGER NOT NULL,
                        token_id INTEGER,
                        flow_project_id TEXT,
                        media_type TEXT,
                        source_url TEXT,
                        storage_provider TEXT DEFAULT 'local',
                        object_key TEXT,
                        delivery_mode TEXT DEFAULT 'proxy',
                        size_bytes INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (api_key_id) REFERENCES api_keys(id),
                        FOREIGN KEY (token_id) REFERENCES tokens(id)
                    )
                """)

            # ========== Step 2: Add missing columns to existing tables ==========
            # Check and add missing columns to tokens table
            if await self._table_exists(db, "tokens"):
                columns_to_add = [
                    ("at", "TEXT"),  # Access Token
                    ("at_expires", "TIMESTAMP"),  # AT expiration time
                    ("credits", "INTEGER DEFAULT 0"),  # Balance
                    ("user_paygate_tier", "TEXT"),  # User tier
                    ("current_project_id", "TEXT"),  # Current project UUID
                    ("current_project_name", "TEXT"),  # Project name
                    ("image_enabled", "BOOLEAN DEFAULT 1"),
                    ("video_enabled", "BOOLEAN DEFAULT 1"),
                    ("image_concurrency", "INTEGER DEFAULT -1"),
                    ("video_concurrency", "INTEGER DEFAULT -1"),
                    ("captcha_proxy_url", "TEXT"),  # token级打码代理
                    ("extension_route_key", "TEXT"),  # extension 模式路由键
                    ("use_extension_for_generation", "INTEGER NOT NULL DEFAULT 1"),
                    ("protocol_mode", "TEXT NOT NULL DEFAULT 'session'"),
                    ("google_cookies", "TEXT NOT NULL DEFAULT ''"),
                    ("login_account", "TEXT NOT NULL DEFAULT ''"),
                    ("login_password", "TEXT NOT NULL DEFAULT ''"),
                    ("proxy_url", "TEXT NOT NULL DEFAULT ''"),
                    ("auto_refresh_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
                    ("refresh_interval_minutes", "INTEGER NOT NULL DEFAULT 120"),
                    ("last_st_refresh_at", "TIMESTAMP"),
                    ("last_st_refresh_result", "TEXT NOT NULL DEFAULT ''"),
                    ("ban_reason", "TEXT"),  # 禁用原因
                    ("banned_at", "TIMESTAMP"),  # 禁用时间
                ]

                for col_name, col_type in columns_to_add:
                    if not await self._column_exists(db, "tokens", col_name):
                        try:
                            await db.execute(f"ALTER TABLE tokens ADD COLUMN {col_name} {col_type}")
                            print(f"  OK Added column '{col_name}' to tokens table")
                        except Exception as e:
                            print(f"  ERR Failed to add column '{col_name}': {e}")

            # Check and add missing columns to admin_config table
            if await self._table_exists(db, "admin_config"):
                if not await self._column_exists(db, "admin_config", "error_ban_threshold"):
                    try:
                        await db.execute("ALTER TABLE admin_config ADD COLUMN error_ban_threshold INTEGER DEFAULT 3")
                        print("  OK Added column 'error_ban_threshold' to admin_config table")
                    except Exception as e:
                        print(f"  ERR Failed to add column 'error_ban_threshold': {e}")
                if not await self._column_exists(db, "admin_config", "error_ban_enabled"):
                    try:
                        await db.execute("ALTER TABLE admin_config ADD COLUMN error_ban_enabled INTEGER DEFAULT 1")
                        print("  OK Added column 'error_ban_enabled' to admin_config table")
                    except Exception as e:
                        print(f"  ERR Failed to add column 'error_ban_enabled': {e}")

            # Check and add missing columns to proxy_config table
            if await self._table_exists(db, "proxy_config"):
                proxy_columns_to_add = [
                    ("media_proxy_enabled", "BOOLEAN DEFAULT 0"),
                    ("media_proxy_url", "TEXT"),
                ]

                for col_name, col_type in proxy_columns_to_add:
                    if not await self._column_exists(db, "proxy_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE proxy_config ADD COLUMN {col_name} {col_type}")
                            print(f"  OK Added column '{col_name}' to proxy_config table")
                        except Exception as e:
                            print(f"  ERR Failed to add column '{col_name}': {e}")

            # Check and add missing columns to generation_config table
            if await self._table_exists(db, "generation_config"):
                generation_columns_to_add = [
                    ("max_retries", "INTEGER DEFAULT 3"),
                    ("extension_generation_enabled", "BOOLEAN DEFAULT 0"),
                    ("extension_generation_fallback_mode", "TEXT DEFAULT 'local_http_on_recaptcha'"),
                    ("sub2gen_gemini_api_keys", "TEXT DEFAULT ''"),
                    ("sub2gen_openai_api_keys", "TEXT DEFAULT ''"),
                    ("sub2gen_openrouter_api_keys", "TEXT DEFAULT ''"),
                    ("sub2gen_third_party_gemini_api_keys", "TEXT DEFAULT ''"),
                    ("sub2gen_third_party_gemini_base_url", "TEXT DEFAULT ''"),
                    ("cloudflare_account_id", "TEXT DEFAULT ''"),
                    ("cloudflare_api_token", "TEXT DEFAULT ''"),
                    ("sub2gen_csvgen_cookie", "TEXT DEFAULT ''"),
                    ("sub2gen_csvgen_api_keys", "TEXT DEFAULT ''"),
                    ("sub2gen_cloning_model", "TEXT DEFAULT 'gemini-2.5-flash'"),
                    ("sub2gen_metadata_backend", "TEXT DEFAULT 'gemini_native'"),
                    ("sub2gen_metadata_provider_order", "TEXT DEFAULT ''"),
                    ("sub2gen_metadata_enabled_providers", "TEXT DEFAULT ''"),
                    ("sub2gen_metadata_provider_retry_count", "INTEGER DEFAULT 1"),
                    ("sub2gen_metadata_model", "TEXT DEFAULT 'gemini-2.5-flash'"),
                    ("sub2gen_metadata_enabled_models", "TEXT DEFAULT ''"),
                    ("sub2gen_metadata_primary_model", "TEXT DEFAULT ''"),
                    ("sub2gen_metadata_fallback_models", "TEXT DEFAULT ''"),
                    ("metadata_system_prompt", "TEXT DEFAULT ''"),
                    ("sub2gen_cloning_backend", "TEXT DEFAULT 'gemini_native'"),
                    ("sub2gen_cloning_provider_order", "TEXT DEFAULT ''"),
                    ("sub2gen_cloning_enabled_providers", "TEXT DEFAULT ''"),
                    ("sub2gen_cloning_provider_retry_count", "INTEGER DEFAULT 1"),
                    ("sub2gen_cloning_gemini_api_keys", "TEXT DEFAULT ''"),
                    ("sub2gen_cloning_openai_api_keys", "TEXT DEFAULT ''"),
                    ("sub2gen_cloning_openrouter_api_keys", "TEXT DEFAULT ''"),
                    ("sub2gen_cloning_third_party_gemini_api_keys", "TEXT DEFAULT ''"),
                    ("sub2gen_cloning_third_party_gemini_base_url", "TEXT DEFAULT ''"),
                    ("sub2gen_cloning_cloudflare_account_id", "TEXT DEFAULT ''"),
                    ("sub2gen_cloning_cloudflare_api_token", "TEXT DEFAULT ''"),
                    ("cloning_image_system_prompt", "TEXT DEFAULT ''"),
                    ("cloning_video_system_prompt", "TEXT DEFAULT ''"),
                    ("task_tracker_device_id", "TEXT DEFAULT ''"),
                    ("task_tracker_device_name", "TEXT DEFAULT ''"),
                    ("task_tracker_cookies", "TEXT DEFAULT ''"),
                    ("task_tracker_device_token", "TEXT DEFAULT ''"),
                    ("task_tracker_turnstile_token", "TEXT DEFAULT ''"),
                    ("task_tracker_tls_profile", "TEXT DEFAULT ''"),
                ]

                for col_name, col_type in generation_columns_to_add:
                    if not await self._column_exists(db, "generation_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE generation_config ADD COLUMN {col_name} {col_type}")
                            print(f"  OK Added column '{col_name}' to generation_config table")
                        except Exception as e:
                            print(f"  ERR Failed to add column '{col_name}': {e}")

            if await self._table_exists(db, "projects"):
                if not await self._column_exists(db, "projects", "api_key_id"):
                    try:
                        await db.execute("ALTER TABLE projects ADD COLUMN api_key_id INTEGER")
                        print("  OK Added column 'api_key_id' to projects table")
                    except Exception as e:
                        print(f"  ERR Failed to add column 'api_key_id' to projects: {e}")

            if await self._table_exists(db, "tasks"):
                if not await self._column_exists(db, "tasks", "api_key_id"):
                    try:
                        await db.execute("ALTER TABLE tasks ADD COLUMN api_key_id INTEGER")
                        print("  OK Added column 'api_key_id' to tasks table")
                    except Exception as e:
                        print(f"  ERR Failed to add column 'api_key_id' to tasks: {e}")

            if await self._table_exists(db, "request_logs"):
                if not await self._column_exists(db, "request_logs", "api_key_id"):
                    try:
                        await db.execute("ALTER TABLE request_logs ADD COLUMN api_key_id INTEGER")
                        print("  OK Added column 'api_key_id' to request_logs table")
                    except Exception as e:
                        print(f"  ERR Failed to add column 'api_key_id' to request_logs: {e}")

            # Check and add missing columns to captcha_config table
            if await self._table_exists(db, "captcha_config"):
                captcha_columns_to_add = [
                    ("browser_proxy_enabled", "BOOLEAN DEFAULT 0"),
                    ("browser_proxy_url", "TEXT"),
                    ("yescaptcha_task_type", "TEXT DEFAULT 'RecaptchaV3TaskProxylessM1S9'"),
                    ("capmonster_api_key", "TEXT DEFAULT ''"),
                    ("capmonster_base_url", "TEXT DEFAULT 'https://api.capmonster.cloud'"),
                    ("capmonster_min_score", "REAL DEFAULT 0.9"),
                    ("ezcaptcha_api_key", "TEXT DEFAULT ''"),
                    ("ezcaptcha_base_url", "TEXT DEFAULT 'https://api.ez-captcha.com'"),
                    ("capsolver_api_key", "TEXT DEFAULT ''"),
                    ("capsolver_base_url", "TEXT DEFAULT 'https://api.capsolver.com'"),
                    ("browser_count", "INTEGER DEFAULT 1"),
                    ("browser_personal_fresh_restart_every_n_solves", "INTEGER DEFAULT 10"),
                    ("remote_browser_base_url", "TEXT DEFAULT ''"),
                    ("remote_browser_api_key", "TEXT DEFAULT ''"),
                    ("remote_browser_timeout", "INTEGER DEFAULT 60"),
                    ("browser_fallback_to_remote_browser", "BOOLEAN DEFAULT 1"),
                    ("session_refresh_enabled", "BOOLEAN DEFAULT 1"),
                    ("session_refresh_browser_first", "BOOLEAN DEFAULT 1"),
                    ("session_refresh_inject_st_cookie", "BOOLEAN DEFAULT 1"),
                    (
                        "session_refresh_warmup_urls",
                        "TEXT DEFAULT 'https://labs.google/fx/tools/flow,https://labs.google/fx'",
                    ),
                    ("session_refresh_wait_seconds_per_url", "INTEGER DEFAULT 60"),
                    ("session_refresh_overall_timeout_seconds", "INTEGER DEFAULT 180"),
                    ("session_refresh_update_st_from_cookie", "BOOLEAN DEFAULT 1"),
                    ("session_refresh_fail_if_st_refresh_fails", "BOOLEAN DEFAULT 1"),
                    ("session_refresh_local_only", "BOOLEAN DEFAULT 1"),
                    ("session_refresh_scheduler_enabled", "BOOLEAN DEFAULT 0"),
                    ("session_refresh_scheduler_interval_minutes", "INTEGER DEFAULT 30"),
                    ("session_refresh_scheduler_batch_size", "INTEGER DEFAULT 10"),
                    ("session_refresh_scheduler_only_expiring_within_minutes", "INTEGER DEFAULT 60"),
                    ("st_only_refresh_scheduler_enabled", "BOOLEAN DEFAULT 0"),
                    ("st_only_refresh_scheduler_interval_minutes", "INTEGER DEFAULT 5"),
                    ("st_only_refresh_scheduler_batch_size", "INTEGER DEFAULT 20"),
                    ("st_only_refresh_scheduler_expiring_within_minutes", "INTEGER DEFAULT 5"),
                    ("extension_queue_wait_timeout_seconds", "INTEGER DEFAULT 20"),
                    ("dedicated_extension_enabled", "BOOLEAN DEFAULT 0"),
                    ("dedicated_extension_captcha_timeout_seconds", "INTEGER DEFAULT 25"),
                    ("dedicated_extension_st_refresh_timeout_seconds", "INTEGER DEFAULT 45"),
                    ("extension_fallback_to_managed_on_dedicated_failure", "BOOLEAN DEFAULT 0"),
                ]

                for col_name, col_type in captcha_columns_to_add:
                    if not await self._column_exists(db, "captcha_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {col_name} {col_type}")
                            print(f"  OK Added column '{col_name}' to captcha_config table")
                        except Exception as e:
                            print(f"  ERR Failed to add column '{col_name}': {e}")

            # Check and add missing columns to token_stats table
            if await self._table_exists(db, "token_stats"):
                stats_columns_to_add = [
                    ("today_image_count", "INTEGER DEFAULT 0"),
                    ("today_video_count", "INTEGER DEFAULT 0"),
                    ("today_error_count", "INTEGER DEFAULT 0"),
                    ("today_date", "DATE"),
                    ("consecutive_error_count", "INTEGER DEFAULT 0"),  # 🆕 连续错误计数
                ]

                for col_name, col_type in stats_columns_to_add:
                    if not await self._column_exists(db, "token_stats", col_name):
                        try:
                            await db.execute(f"ALTER TABLE token_stats ADD COLUMN {col_name} {col_type}")
                            print(f"  OK Added column '{col_name}' to token_stats table")
                        except Exception as e:
                            print(f"  ERR Failed to add column '{col_name}': {e}")

            # Check and add missing columns to plugin_config table
            if await self._table_exists(db, "plugin_config"):
                plugin_columns_to_add = [
                    ("auto_enable_on_update", "BOOLEAN DEFAULT 1"),  # 默认开启
                ]

                for col_name, col_type in plugin_columns_to_add:
                    if not await self._column_exists(db, "plugin_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE plugin_config ADD COLUMN {col_name} {col_type}")
                            print(f"  OK Added column '{col_name}' to plugin_config table")
                        except Exception as e:
                            print(f"  ERR Failed to add column '{col_name}': {e}")

            # Check and add missing columns to api_keys table
            if await self._table_exists(db, "api_keys"):
                api_keys_columns_to_add = [
                    ("key_plaintext", "TEXT"),
                    ("last_presence_at", "TIMESTAMP"),
                    ("adobe_cloning_enabled", "INTEGER DEFAULT 1"),
                    ("adobe_metadata_enabled", "INTEGER DEFAULT 1"),
                    ("adobe_tracker_enabled", "INTEGER DEFAULT 1"),
                ]

                for col_name, col_type in api_keys_columns_to_add:
                    if not await self._column_exists(db, "api_keys", col_name):
                        try:
                            await db.execute(f"ALTER TABLE api_keys ADD COLUMN {col_name} {col_type}")
                            print(f"  OK Added column '{col_name}' to api_keys table")
                        except Exception as e:
                            print(f"  ERR Failed to add column '{col_name}': {e}")

            # Check and add missing columns to captcha_config table
            if await self._table_exists(db, "captcha_config"):
                captcha_columns_to_add = [
                    ("personal_project_pool_size", "INTEGER DEFAULT 4"),
                    ("personal_max_resident_tabs", "INTEGER DEFAULT 5"),
                    ("personal_idle_tab_ttl_seconds", "INTEGER DEFAULT 600"),
                    ("browser_captcha_page_url", "TEXT DEFAULT 'https://labs.google/fx/api/auth/providers'"),
                ]

                for col_name, col_type in captcha_columns_to_add:
                    if not await self._column_exists(db, "captcha_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {col_name} {col_type}")
                            print(f"  OK Added column '{col_name}' to captcha_config table")
                        except Exception as e:
                            print(f"  ERR Failed to add column '{col_name}': {e}")

            if await self._table_exists(db, "cache_files"):
                await self._ensure_cache_backend_columns(db)

            await self._ensure_operation_stats_table(db)
            await self._ensure_tokens_use_extension_for_generation_column(db)
            await self._ensure_tokens_browser_profile_columns(db)

            # ========== Step 3: Ensure all config tables have default rows ==========
            # Note: This will NOT overwrite existing config rows
            # It only ensures missing rows are created with default values from setting.toml
            await self._ensure_config_rows(db, config_dict=config_dict)

            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_api_key_created_at ON projects(api_key_id, created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_logs_api_key_id_created_at ON request_logs(api_key_id, created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_logs_api_key_op_created ON request_logs(api_key_id, operation, created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_files_api_key_filename ON cache_files(api_key_id, filename)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_files_api_key_project ON cache_files(api_key_id, flow_project_id)"
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_operation_stats_operation ON operation_stats(operation)")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_extension_worker_bindings_api_key_id ON extension_worker_bindings(api_key_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_captcha_worker_keys_active ON captcha_worker_keys(is_active)"
            )

            await db.commit()
            if migration_state == "legacy":
                self.database_revision = await stamp_compatible_sqlite_database(db)
            else:
                self.database_revision = discover_sqlite_migrations()[-1].revision
            print("Database migration check completed.")

    async def init_db(self):
        """Initialize database tables"""
        async with self._connect(write=True) as db:
            await self._configure_write_pragmas(db)
            migration_state = await prepare_sqlite_migrations(db)
            if migration_state != "legacy":
                self.database_revision = discover_sqlite_migrations()[-1].revision
            # Tokens table (sub2gen版本)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    st TEXT UNIQUE NOT NULL,
                    at TEXT,
                    at_expires TIMESTAMP,
                    email TEXT NOT NULL,
                    name TEXT,
                    remark TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP,
                    use_count INTEGER DEFAULT 0,
                    credits INTEGER DEFAULT 0,
                    user_paygate_tier TEXT,
                    current_project_id TEXT,
                    current_project_name TEXT,
                    image_enabled BOOLEAN DEFAULT 1,
                    video_enabled BOOLEAN DEFAULT 1,
                    image_concurrency INTEGER DEFAULT -1,
                    video_concurrency INTEGER DEFAULT -1,
                    captcha_proxy_url TEXT,
                    extension_route_key TEXT,
                    use_extension_for_generation INTEGER NOT NULL DEFAULT 1,
                    auth_mode TEXT NOT NULL DEFAULT 'session_token',
                    browser_profile_path TEXT,
                    browser_profile_status TEXT DEFAULT 'not_created',
                    browser_profile_email TEXT,
                    browser_profile_name TEXT,
                    browser_profile_login_state TEXT DEFAULT 'unknown',
                    browser_profile_cookie_status TEXT DEFAULT 'unknown',
                    browser_profile_st_status TEXT DEFAULT 'unknown',
                    browser_profile_at_status TEXT DEFAULT 'unknown',
                    browser_profile_last_opened_at TIMESTAMP,
                    browser_profile_last_sync_at TIMESTAMP,
                    browser_profile_last_refresh_at TIMESTAMP,
                    browser_profile_last_error TEXT,
                    protocol_mode TEXT NOT NULL DEFAULT 'session',
                    google_cookies TEXT NOT NULL DEFAULT '',
                    login_account TEXT NOT NULL DEFAULT '',
                    login_password TEXT NOT NULL DEFAULT '',
                    proxy_url TEXT NOT NULL DEFAULT '',
                    auto_refresh_enabled BOOLEAN NOT NULL DEFAULT 1,
                    refresh_interval_minutes INTEGER NOT NULL DEFAULT 120,
                    last_st_refresh_at TIMESTAMP,
                    last_st_refresh_result TEXT NOT NULL DEFAULT '',
                    ban_reason TEXT,
                    banned_at TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_refresh_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    refresh_interval_minutes INTEGER NOT NULL DEFAULT 120,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute(
                "INSERT OR IGNORE INTO token_refresh_config (id, enabled, refresh_interval_minutes) VALUES (1, 1, 120)"
            )

            # Projects table (新增)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    api_key_id INTEGER,
                    project_name TEXT NOT NULL,
                    tool_name TEXT DEFAULT 'PINHOLE',
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id),
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                )
            """)

            # Token stats table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER NOT NULL,
                    image_count INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    last_success_at TIMESTAMP,
                    last_error_at TIMESTAMP,
                    today_image_count INTEGER DEFAULT 0,
                    today_video_count INTEGER DEFAULT 0,
                    today_error_count INTEGER DEFAULT 0,
                    today_date DATE,
                    consecutive_error_count INTEGER DEFAULT 0,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            await self._ensure_operation_stats_table(db)

            # Tasks table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    api_key_id INTEGER,
                    project_id TEXT,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    progress INTEGER DEFAULT 0,
                    result_urls TEXT,
                    base_result_urls TEXT,
                    delivery_urls TEXT,
                    requested_resolution TEXT,
                    output_resolution TEXT,
                    upscale_status TEXT,
                    upscale_error_message TEXT,
                    error_message TEXT,
                    scene_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id),
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                )
            """)

            # Request logs table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    api_key_id INTEGER,
                    operation TEXT NOT NULL,
                    request_body TEXT,
                    response_body TEXT,
                    status_code INTEGER NOT NULL,
                    duration FLOAT NOT NULL,
                    status_text TEXT DEFAULT '',
                    progress INTEGER DEFAULT 0,
                    request_excerpt TEXT DEFAULT '',
                    response_excerpt TEXT DEFAULT '',
                    payload_available BOOLEAN DEFAULT 0,
                    payload_object_key TEXT,
                    payload_storage_error TEXT,
                    request_size_bytes INTEGER DEFAULT 0,
                    response_size_bytes INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id),
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS redis_persisted_events (
                    cursor TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    persisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL UNIQUE,
                    api_key_id INTEGER NOT NULL,
                    token_id INTEGER,
                    flow_project_id TEXT,
                    media_type TEXT,
                    source_url TEXT,
                    storage_provider TEXT DEFAULT 'local',
                    object_key TEXT,
                    delivery_mode TEXT DEFAULT 'proxy',
                    size_bytes INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id),
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Admin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    username TEXT DEFAULT 'admin',
                    password TEXT DEFAULT 'admin',
                    api_key TEXT DEFAULT 'han1234',
                    error_ban_threshold INTEGER DEFAULT 3,
                    error_ban_enabled INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Admin UI session tokens (survive process restarts)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    last_used_at INTEGER
                )
            """)

            # Proxy config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proxy_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    proxy_url TEXT,
                    media_proxy_enabled BOOLEAN DEFAULT 0,
                    media_proxy_url TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Generation config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS generation_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    image_timeout INTEGER DEFAULT 300,
                    video_timeout INTEGER DEFAULT 1500,
                    max_retries INTEGER DEFAULT 3,
                    extension_generation_enabled BOOLEAN DEFAULT 0,
                    extension_generation_fallback_mode TEXT DEFAULT 'local_http_on_recaptcha',
                    sub2gen_gemini_api_keys TEXT DEFAULT '',
                    sub2gen_openai_api_keys TEXT DEFAULT '',
                    sub2gen_third_party_gemini_api_keys TEXT DEFAULT '',
                    sub2gen_third_party_gemini_base_url TEXT DEFAULT '',
                    cloudflare_account_id TEXT DEFAULT '',
                    cloudflare_api_token TEXT DEFAULT '',
                    sub2gen_csvgen_cookie TEXT DEFAULT '',
                    sub2gen_csvgen_api_keys TEXT DEFAULT '',
                    sub2gen_cloning_model TEXT DEFAULT 'gemini-2.5-flash',
                    sub2gen_metadata_backend TEXT DEFAULT 'gemini_native',
                    sub2gen_metadata_provider_order TEXT DEFAULT '',
                    sub2gen_metadata_enabled_providers TEXT DEFAULT '',
                    sub2gen_metadata_provider_retry_count INTEGER DEFAULT 1,
                    sub2gen_metadata_model TEXT DEFAULT 'gemini-2.5-flash',
                    sub2gen_metadata_enabled_models TEXT DEFAULT '',
                    sub2gen_metadata_primary_model TEXT DEFAULT '',
                    sub2gen_metadata_fallback_models TEXT DEFAULT '',
                    metadata_system_prompt TEXT DEFAULT '',
                    sub2gen_cloning_backend TEXT DEFAULT 'gemini_native',
                    sub2gen_cloning_provider_order TEXT DEFAULT '',
                    sub2gen_cloning_enabled_providers TEXT DEFAULT '',
                    sub2gen_cloning_provider_retry_count INTEGER DEFAULT 1,
                    cloning_image_system_prompt TEXT DEFAULT '',
                    cloning_video_system_prompt TEXT DEFAULT '',
                    task_tracker_device_id TEXT DEFAULT '',
                    task_tracker_device_name TEXT DEFAULT '',
                    task_tracker_cookies TEXT DEFAULT '',
                    task_tracker_device_token TEXT DEFAULT '',
                    task_tracker_turnstile_token TEXT DEFAULT '',
                    task_tracker_tls_profile TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await self._ensure_runway_tables(db)
            await self._ensure_geminigen_tables(db)

            # Call logic config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS call_logic_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    call_mode TEXT DEFAULT 'default',
                    polling_mode_enabled BOOLEAN DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Cache config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    cache_enabled BOOLEAN DEFAULT 0,
                    cache_timeout INTEGER DEFAULT 7200,
                    cache_base_url TEXT,
                    cache_provider TEXT DEFAULT 'local',
                    cache_delivery_mode TEXT DEFAULT 'proxy',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Debug config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS debug_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    log_requests BOOLEAN DEFAULT 1,
                    log_responses BOOLEAN DEFAULT 1,
                    mask_token BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Captcha config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS captcha_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    captcha_method TEXT DEFAULT 'browser',
                    yescaptcha_api_key TEXT DEFAULT '',
                    yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                    yescaptcha_task_type TEXT DEFAULT 'RecaptchaV3TaskProxylessM1S9',
                    capmonster_api_key TEXT DEFAULT '',
                    capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                    capmonster_min_score REAL DEFAULT 0.9,
                    ezcaptcha_api_key TEXT DEFAULT '',
                    ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                    capsolver_api_key TEXT DEFAULT '',
                    capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                    remote_browser_base_url TEXT DEFAULT '',
                    remote_browser_api_key TEXT DEFAULT '',
                    remote_browser_timeout INTEGER DEFAULT 60,
                    extension_queue_wait_timeout_seconds INTEGER DEFAULT 20,
                    browser_fallback_to_remote_browser BOOLEAN DEFAULT 1,
                    website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                    page_action TEXT DEFAULT 'IMAGE_GENERATION',

                    browser_proxy_enabled BOOLEAN DEFAULT 0,
                    browser_proxy_url TEXT,
                    browser_count INTEGER DEFAULT 1,
                    browser_personal_fresh_restart_every_n_solves INTEGER DEFAULT 10,
                    personal_project_pool_size INTEGER DEFAULT 4,
                    personal_max_resident_tabs INTEGER DEFAULT 5,
                    personal_idle_tab_ttl_seconds INTEGER DEFAULT 600,
                    browser_captcha_page_url TEXT DEFAULT 'https://labs.google/fx/api/auth/providers',
                    session_refresh_enabled BOOLEAN DEFAULT 1,
                    session_refresh_browser_first BOOLEAN DEFAULT 1,
                    session_refresh_inject_st_cookie BOOLEAN DEFAULT 1,
                    session_refresh_warmup_urls TEXT DEFAULT 'https://labs.google/fx/tools/flow,https://labs.google/fx',
                    session_refresh_wait_seconds_per_url INTEGER DEFAULT 60,
                    session_refresh_overall_timeout_seconds INTEGER DEFAULT 180,
                    session_refresh_update_st_from_cookie BOOLEAN DEFAULT 1,
                    session_refresh_fail_if_st_refresh_fails BOOLEAN DEFAULT 1,
                    session_refresh_local_only BOOLEAN DEFAULT 1,
                    session_refresh_scheduler_enabled BOOLEAN DEFAULT 0,
                    session_refresh_scheduler_interval_minutes INTEGER DEFAULT 30,
                    session_refresh_scheduler_batch_size INTEGER DEFAULT 10,
                    session_refresh_scheduler_only_expiring_within_minutes INTEGER DEFAULT 60,
                    st_only_refresh_scheduler_enabled BOOLEAN DEFAULT 0,
                    st_only_refresh_scheduler_interval_minutes INTEGER DEFAULT 5,
                    st_only_refresh_scheduler_batch_size INTEGER DEFAULT 20,
                    st_only_refresh_scheduler_expiring_within_minutes INTEGER DEFAULT 5,
                    dedicated_extension_enabled BOOLEAN DEFAULT 0,
                    dedicated_extension_captcha_timeout_seconds INTEGER DEFAULT 25,
                    dedicated_extension_st_refresh_timeout_seconds INTEGER DEFAULT 45,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Plugin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS plugin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    connection_token TEXT DEFAULT '',
                    auto_enable_on_update BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # API key manager tables
            await db.execute("""
                CREATE TABLE IF NOT EXISTS api_clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_plaintext TEXT,
                    key_hash TEXT NOT NULL UNIQUE,
                    scopes TEXT DEFAULT '*',
                    is_active BOOLEAN DEFAULT 1,
                    expires_at TIMESTAMP,
                    last_used_at TIMESTAMP,
                    last_presence_at TIMESTAMP,
                    adobe_cloning_enabled INTEGER DEFAULT 1,
                    adobe_metadata_enabled INTEGER DEFAULT 1,
                    adobe_tracker_enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (client_id) REFERENCES api_clients(id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS api_key_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(api_key_id, account_id),
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS extension_worker_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_key TEXT NOT NULL UNIQUE,
                    api_key_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS captcha_worker_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_prefix TEXT NOT NULL UNIQUE,
                    key_hash TEXT NOT NULL UNIQUE,
                    label TEXT DEFAULT '',
                    key_plaintext TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    last_instance_id TEXT,
                    last_seen_at TIMESTAMP,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS api_key_rate_limits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key_id INTEGER NOT NULL,
                    endpoint TEXT NOT NULL,
                    rpm INTEGER DEFAULT 0,
                    rph INTEGER DEFAULT 0,
                    burst INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(api_key_id, endpoint),
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS api_key_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key_id INTEGER,
                    endpoint TEXT NOT NULL,
                    account_id INTEGER,
                    status_code INTEGER NOT NULL,
                    detail TEXT,
                    ip TEXT,
                    user_agent TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                )
            """)

            # Existing DBs: tables were created before api_key_id; CREATE IF NOT EXISTS does not add columns.
            await self._ensure_api_key_ownership_columns(db)
            await self._ensure_task_async_columns(db)
            await self._ensure_tokens_use_extension_for_generation_column(db)
            await self._ensure_tokens_browser_profile_columns(db)

            # Create indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_id ON tasks(task_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_st ON tokens(st)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_project_id ON projects(project_id)")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_api_key_created_at ON projects(api_key_id, created_at DESC)"
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tokens_email ON tokens(email)")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tokens_is_active_last_used_at ON tokens(is_active, last_used_at)"
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_client_id ON api_keys(client_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_api_key_accounts_key_id ON api_key_accounts(api_key_id)")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_key_rl_key_endpoint ON api_key_rate_limits(api_key_id, endpoint)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_key_audit_created_at ON api_key_audit_logs(created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_captcha_worker_keys_active ON captcha_worker_keys(is_active)"
            )

            # Migrate request_logs table if needed
            await self._migrate_request_logs(db)

            # Request logs query indexes (列表按 created_at 排序 / token 过滤)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at DESC)")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_logs_token_id_created_at ON request_logs(token_id, created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_logs_api_key_id_created_at ON request_logs(api_key_id, created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_logs_operation_created_at ON request_logs(operation, created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_logs_status_created_at ON request_logs(status_code, status_text, created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_geminigen_tasks_retention ON geminigen_tasks(status, completed_at, updated_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_key_audit_logs_created_at ON api_key_audit_logs(created_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_logs_api_key_op_created ON request_logs(api_key_id, operation, created_at DESC)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_files_api_key_filename ON cache_files(api_key_id, filename)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_files_api_key_project ON cache_files(api_key_id, flow_project_id)"
            )

            # Token stats lookup index
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_stats_token_id ON token_stats(token_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_operation_stats_operation ON operation_stats(operation)")

            await db.commit()

    async def _ensure_operation_stats_table(self, db) -> None:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS operation_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT NOT NULL UNIQUE,
                success_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                today_success_count INTEGER DEFAULT 0,
                today_error_count INTEGER DEFAULT 0,
                today_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    async def _ensure_tokens_use_extension_for_generation_column(self, db) -> None:
        """Add use_extension_for_generation when upgrading older tokens schemas."""
        try:
            if not await self._table_exists(db, "tokens"):
                return
            if not await self._column_exists(db, "tokens", "use_extension_for_generation"):
                await db.execute(
                    "ALTER TABLE tokens ADD COLUMN use_extension_for_generation INTEGER NOT NULL DEFAULT 1"
                )
                print("  OK Added column 'use_extension_for_generation' to tokens table")
        except Exception as e:
            print(f"  ERR tokens use_extension_for_generation migration failed: {e}")

    async def _ensure_tokens_browser_profile_columns(self, db) -> None:
        """Add headed persistent-browser profile metadata to tokens."""
        try:
            if not await self._table_exists(db, "tokens"):
                return
            columns_to_add = [
                ("auth_mode", "TEXT NOT NULL DEFAULT 'session_token'"),
                ("browser_profile_path", "TEXT"),
                ("browser_profile_status", "TEXT DEFAULT 'not_created'"),
                ("browser_profile_email", "TEXT"),
                ("browser_profile_name", "TEXT"),
                ("browser_profile_login_state", "TEXT DEFAULT 'unknown'"),
                ("browser_profile_cookie_status", "TEXT DEFAULT 'unknown'"),
                ("browser_profile_st_status", "TEXT DEFAULT 'unknown'"),
                ("browser_profile_at_status", "TEXT DEFAULT 'unknown'"),
                ("browser_profile_last_opened_at", "TIMESTAMP"),
                ("browser_profile_last_sync_at", "TIMESTAMP"),
                ("browser_profile_last_refresh_at", "TIMESTAMP"),
                ("browser_profile_last_error", "TEXT"),
            ]
            for col_name, col_type in columns_to_add:
                if not await self._column_exists(db, "tokens", col_name):
                    await db.execute(f"ALTER TABLE tokens ADD COLUMN {col_name} {col_type}")
                    print(f"  OK Added column '{col_name}' to tokens table")
        except Exception as e:
            print(f"  ERR tokens browser profile migration failed: {e}")
            raise

    async def _ensure_api_key_ownership_columns(self, db):
        """Add api_key_id to core tables when upgrading from older schemas (before indexes on those columns)."""
        try:
            if await self._table_exists(db, "projects"):
                if not await self._column_exists(db, "projects", "api_key_id"):
                    await db.execute("ALTER TABLE projects ADD COLUMN api_key_id INTEGER")
                    print("  OK Added column 'api_key_id' to projects (init_db upgrade)")
            if await self._table_exists(db, "tasks"):
                if not await self._column_exists(db, "tasks", "api_key_id"):
                    await db.execute("ALTER TABLE tasks ADD COLUMN api_key_id INTEGER")
                    print("  OK Added column 'api_key_id' to tasks (init_db upgrade)")
            if await self._table_exists(db, "request_logs"):
                if not await self._column_exists(db, "request_logs", "api_key_id"):
                    await db.execute("ALTER TABLE request_logs ADD COLUMN api_key_id INTEGER")
                    print("  OK Added column 'api_key_id' to request_logs (init_db upgrade)")
            if await self._table_exists(db, "cache_files"):
                await self._ensure_cache_backend_columns(db)
        except Exception as e:
            print(f"  ERR api_key_id column upgrade failed: {e}")
            raise

    async def _ensure_cache_backend_columns(self, db):
        """Upgrade cache tables for selectable storage providers."""
        cache_file_columns = {
            "flow_project_id": "TEXT",
            "storage_provider": "TEXT DEFAULT 'local'",
            "object_key": "TEXT",
            "delivery_mode": "TEXT DEFAULT 'proxy'",
            "size_bytes": "INTEGER",
        }
        if await self._table_exists(db, "cache_files"):
            for column_name, column_type in cache_file_columns.items():
                if not await self._column_exists(db, "cache_files", column_name):
                    await db.execute(f"ALTER TABLE cache_files ADD COLUMN {column_name} {column_type}")
                    print(f"  OK Added column '{column_name}' to cache_files")
            await db.execute("UPDATE cache_files SET object_key = filename WHERE object_key IS NULL OR object_key = ''")

        if await self._table_exists(db, "cache_config"):
            for column_name, column_type in {
                "cache_provider": "TEXT DEFAULT 'local'",
                "cache_delivery_mode": "TEXT DEFAULT 'proxy'",
            }.items():
                if not await self._column_exists(db, "cache_config", column_name):
                    await db.execute(f"ALTER TABLE cache_config ADD COLUMN {column_name} {column_type}")
                    print(f"  OK Added column '{column_name}' to cache_config")

    async def _ensure_task_async_columns(self, db):
        """Add async job metadata columns to tasks for polling payload enrichment."""
        try:
            if not await self._table_exists(db, "tasks"):
                return
            async_columns = {
                "project_id": "TEXT",
                "base_result_urls": "TEXT",
                "delivery_urls": "TEXT",
                "requested_resolution": "TEXT",
                "output_resolution": "TEXT",
                "upscale_status": "TEXT",
                "upscale_error_message": "TEXT",
                "job_phase": "TEXT",
                "captcha_status": "TEXT",
                "captcha_detail": "TEXT",
            }
            for column_name, column_type in async_columns.items():
                if not await self._column_exists(db, "tasks", column_name):
                    await db.execute(f"ALTER TABLE tasks ADD COLUMN {column_name} {column_type}")
                    print(f"  OK Added column '{column_name}' to tasks (init_db upgrade)")
        except Exception as e:
            print(f"  ERR tasks async columns upgrade failed: {e}")
            raise

    async def _migrate_request_logs(self, db):
        """Migrate request_logs table from old schema to new schema"""
        try:
            has_model = await self._column_exists(db, "request_logs", "model")
            has_operation = await self._column_exists(db, "request_logs", "operation")

            if has_model and not has_operation:
                print("?? ?????request_logs???,????...")
                await db.execute("ALTER TABLE request_logs RENAME TO request_logs_old")
                await db.execute("""
                    CREATE TABLE request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_id INTEGER,
                        api_key_id INTEGER,
                        operation TEXT NOT NULL,
                        request_body TEXT,
                        response_body TEXT,
                        status_code INTEGER NOT NULL,
                        duration FLOAT NOT NULL,
                        status_text TEXT DEFAULT '',
                        progress INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (token_id) REFERENCES tokens(id),
                        FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                    )
                """)
                await db.execute("""
                    INSERT INTO request_logs (token_id, api_key_id, operation, request_body, status_code, duration, status_text, progress, created_at, updated_at)
                    SELECT
                        token_id,
                        NULL AS api_key_id,
                        model as operation,
                        json_object('model', model, 'prompt', substr(prompt, 1, 100)) as request_body,
                        CASE
                            WHEN status = 'completed' THEN 200
                            WHEN status = 'failed' THEN 500
                            ELSE 102
                        END as status_code,
                        response_time as duration,
                        CASE
                            WHEN status = 'completed' THEN 'completed'
                            WHEN status = 'failed' THEN 'failed'
                            ELSE 'processing'
                        END as status_text,
                        CASE
                            WHEN status = 'completed' THEN 100
                            WHEN status = 'failed' THEN 0
                            ELSE 0
                        END as progress,
                        created_at,
                        created_at
                    FROM request_logs_old
                """)
                await db.execute("DROP TABLE request_logs_old")
                print("? request_logs?????")

            if not await self._column_exists(db, "request_logs", "status_text"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN status_text TEXT DEFAULT ''")
            if not await self._column_exists(db, "request_logs", "progress"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN progress INTEGER DEFAULT 0")
            if not await self._column_exists(db, "request_logs", "updated_at"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN updated_at TIMESTAMP")
            if not await self._column_exists(db, "request_logs", "api_key_id"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN api_key_id INTEGER")
            summary_columns = {
                "request_excerpt": "TEXT DEFAULT ''",
                "response_excerpt": "TEXT DEFAULT ''",
                "payload_available": "BOOLEAN DEFAULT 0",
                "payload_object_key": "TEXT",
                "payload_storage_error": "TEXT",
                "request_size_bytes": "INTEGER DEFAULT 0",
                "response_size_bytes": "INTEGER DEFAULT 0",
            }
            for column_name, column_type in summary_columns.items():
                if not await self._column_exists(db, "request_logs", column_name):
                    await db.execute(f"ALTER TABLE request_logs ADD COLUMN {column_name} {column_type}")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS redis_persisted_events (
                    cursor TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    persisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute("UPDATE request_logs SET updated_at = created_at WHERE updated_at IS NULL")
        except Exception as e:
            print(f"?? request_logs?????: {e}")
            # Continue even if migration fails

    # Token operations
    async def add_token(self, token: Token) -> int:
        """Add a new token"""
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO tokens (st, at, at_expires, email, name, remark, is_active,
                                   credits, user_paygate_tier, current_project_id, current_project_name,
                                   image_enabled, video_enabled, image_concurrency, video_concurrency,
                                   captcha_proxy_url, extension_route_key, use_extension_for_generation,
                                   auth_mode, browser_profile_path, browser_profile_status,
                                   browser_profile_email, browser_profile_name, browser_profile_login_state,
                                   browser_profile_cookie_status, browser_profile_st_status, browser_profile_at_status,
                                   browser_profile_last_opened_at, browser_profile_last_sync_at,
                                   browser_profile_last_refresh_at, browser_profile_last_error,
                                   protocol_mode, google_cookies, login_account, login_password, proxy_url,
                                   auto_refresh_enabled, refresh_interval_minutes,
                                   last_st_refresh_at, last_st_refresh_result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    token.st,
                    token.at,
                    token.at_expires,
                    token.email,
                    token.name,
                    token.remark,
                    token.is_active,
                    token.credits,
                    token.user_paygate_tier,
                    token.current_project_id,
                    token.current_project_name,
                    token.image_enabled,
                    token.video_enabled,
                    token.image_concurrency,
                    token.video_concurrency,
                    token.captcha_proxy_url,
                    token.extension_route_key,
                    1 if getattr(token, "use_extension_for_generation", True) else 0,
                    getattr(token, "auth_mode", "session_token"),
                    getattr(token, "browser_profile_path", None),
                    getattr(token, "browser_profile_status", "not_created"),
                    getattr(token, "browser_profile_email", None),
                    getattr(token, "browser_profile_name", None),
                    getattr(token, "browser_profile_login_state", "unknown"),
                    getattr(token, "browser_profile_cookie_status", "unknown"),
                    getattr(token, "browser_profile_st_status", "unknown"),
                    getattr(token, "browser_profile_at_status", "unknown"),
                    getattr(token, "browser_profile_last_opened_at", None),
                    getattr(token, "browser_profile_last_sync_at", None),
                    getattr(token, "browser_profile_last_refresh_at", None),
                    getattr(token, "browser_profile_last_error", None),
                    getattr(token, "protocol_mode", "session"),
                    getattr(token, "google_cookies", ""),
                    getattr(token, "login_account", ""),
                    getattr(token, "login_password", ""),
                    getattr(token, "proxy_url", ""),
                    1 if getattr(token, "auto_refresh_enabled", True) else 0,
                    max(1, min(10080, int(getattr(token, "refresh_interval_minutes", 120) or 120))),
                    getattr(token, "last_st_refresh_at", None),
                    getattr(token, "last_st_refresh_result", ""),
                ),
            )
            await db.commit()
            token_id = cursor.lastrowid

            # Create stats entry
            await db.execute(
                """
                INSERT INTO token_stats (token_id) VALUES (?)
            """,
                (token_id,),
            )
            await db.commit()

            return token_id

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None

    async def get_token_by_st(self, st: str) -> Optional[Token]:
        """Get token by ST"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE st = ?", (st,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None

    async def get_token_by_email(self, email: str) -> Optional[Token]:
        """Get token by email"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE email = ?", (email,))
            row = await cursor.fetchone()
            if row:
                return Token(**dict(row))
            return None

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            return [Token(**dict(row)) for row in rows]

    async def get_all_tokens_with_stats(self) -> List[Dict[str, Any]]:
        """Get all tokens with merged statistics in one query"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            today = self._current_stats_date()
            cursor = await db.execute(
                """
                SELECT
                    t.*,
                    COALESCE(ts.image_count, 0) AS image_count,
                    COALESCE(ts.video_count, 0) AS video_count,
                    COALESCE(ts.error_count, 0) AS error_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_image_count ELSE 0 END, 0) AS today_image_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_video_count ELSE 0 END, 0) AS today_video_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_error_count ELSE 0 END, 0) AS today_error_count,
                    COALESCE(ts.consecutive_error_count, 0) AS consecutive_error_count,
                    ts.last_error_at AS last_error_at
                FROM tokens t
                LEFT JOIN token_stats ts ON ts.token_id = t.id
                ORDER BY t.created_at DESC
            """,
                (today, today, today),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_dashboard_stats(self) -> Dict[str, int]:
        """Get dashboard counters with aggregated SQL queries"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            today = self._current_stats_date()

            token_cursor = await db.execute("""
                SELECT
                    COUNT(*) AS total_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens
                FROM tokens
            """)
            token_row = await token_cursor.fetchone()

            stats_cursor = await db.execute(
                """
                SELECT
                    COALESCE(SUM(image_count), 0) AS total_images,
                    COALESCE(SUM(video_count), 0) AS total_videos,
                    COALESCE(SUM(error_count), 0) AS total_errors,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_image_count ELSE 0 END), 0) AS today_images,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_video_count ELSE 0 END), 0) AS today_videos,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_error_count ELSE 0 END), 0) AS today_errors
                FROM token_stats
            """,
                (today, today, today),
            )
            stats_row = await stats_cursor.fetchone()

            geminigen_cursor = await db.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'completed' AND kind = 'image' THEN 1 ELSE 0 END), 0) AS total_images,
                    COALESCE(SUM(CASE WHEN status = 'completed' AND kind = 'video' THEN 1 ELSE 0 END), 0) AS total_videos,
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS total_errors,
                    COALESCE(SUM(CASE
                        WHEN status = 'completed' AND kind = 'image'
                             AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_images,
                    COALESCE(SUM(CASE
                        WHEN status = 'completed' AND kind = 'video'
                             AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_videos,
                    COALESCE(SUM(CASE
                        WHEN status = 'failed'
                             AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_errors
                FROM geminigen_tasks
            """,
                (today, today, today),
            )
            geminigen_row = await geminigen_cursor.fetchone()

            token_data = dict(token_row) if token_row else {}
            stats_data = dict(stats_row) if stats_row else {}
            geminigen_data = dict(geminigen_row) if geminigen_row else {}
            metadata_stats = await self.get_operation_stats("adobe:metadata")

            return {
                "total_tokens": int(token_data.get("total_tokens") or 0),
                "active_tokens": int(token_data.get("active_tokens") or 0),
                "total_images": int(stats_data.get("total_images") or 0) + int(geminigen_data.get("total_images") or 0),
                "total_videos": int(stats_data.get("total_videos") or 0) + int(geminigen_data.get("total_videos") or 0),
                "total_errors": int(stats_data.get("total_errors") or 0) + int(geminigen_data.get("total_errors") or 0),
                "today_images": int(stats_data.get("today_images") or 0) + int(geminigen_data.get("today_images") or 0),
                "today_videos": int(stats_data.get("today_videos") or 0) + int(geminigen_data.get("today_videos") or 0),
                "today_errors": int(stats_data.get("today_errors") or 0) + int(geminigen_data.get("today_errors") or 0),
                "total_metadata": int(metadata_stats.get("success_count") or 0),
                "today_metadata": int(metadata_stats.get("today_success_count") or 0),
                "metadata_errors": int(metadata_stats.get("error_count") or 0),
                "today_metadata_errors": int(metadata_stats.get("today_error_count") or 0),
            }

    async def get_api_key_generation_stats(
        self,
        api_key_id: int,
        *,
        native_image_models: Optional[List[str]] = None,
        native_video_models: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Dashboard-style durable generation counters for one managed API key."""
        today = self._current_stats_date()
        stats = {
            "total_images": 0,
            "total_videos": 0,
            "total_errors": 0,
            "today_images": 0,
            "today_videos": 0,
            "today_errors": 0,
        }

        def add_row(row: Optional[aiosqlite.Row]) -> None:
            data = dict(row) if row else {}
            for key in stats:
                stats[key] += int(data.get(key) or 0)

        def model_in_condition(column_name: str, values: List[str]) -> tuple[str, List[str]]:
            cleaned = [str(value).strip() for value in values if str(value).strip()]
            if not cleaned:
                return "0", []
            placeholders = ",".join("?" for _ in cleaned)
            return f"{column_name} IN ({placeholders})", cleaned

        image_condition, image_params = model_in_condition("model", native_image_models or [])
        video_condition, video_params = model_in_condition("model", native_video_models or [])

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row

            native_params: List[Any] = (
                image_params + video_params + image_params + [today] + video_params + [today, today, api_key_id]
            )
            native_cursor = await db.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'completed' AND {image_condition} THEN 1 ELSE 0 END), 0) AS total_images,
                    COALESCE(SUM(CASE WHEN status = 'completed' AND {video_condition} THEN 1 ELSE 0 END), 0) AS total_videos,
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS total_errors,
                    COALESCE(SUM(CASE
                        WHEN status = 'completed' AND {image_condition}
                             AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_images,
                    COALESCE(SUM(CASE
                        WHEN status = 'completed' AND {video_condition}
                             AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_videos,
                    COALESCE(SUM(CASE
                        WHEN status = 'failed' AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_errors
                FROM tasks
                WHERE api_key_id = ?
                """,
                native_params,
            )
            add_row(await native_cursor.fetchone())

            geminigen_cursor = await db.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'completed' AND kind = 'image' THEN 1 ELSE 0 END), 0) AS total_images,
                    COALESCE(SUM(CASE WHEN status = 'completed' AND kind = 'video' THEN 1 ELSE 0 END), 0) AS total_videos,
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS total_errors,
                    COALESCE(SUM(CASE
                        WHEN status = 'completed' AND kind = 'image'
                             AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_images,
                    COALESCE(SUM(CASE
                        WHEN status = 'completed' AND kind = 'video'
                             AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_videos,
                    COALESCE(SUM(CASE
                        WHEN status = 'failed' AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_errors
                FROM geminigen_tasks
                WHERE api_key_id = ?
                """,
                (today, today, today, api_key_id),
            )
            add_row(await geminigen_cursor.fetchone())

            runway_cursor = await db.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN rt.status = 'completed' AND rm.kind = 'image' THEN 1 ELSE 0 END), 0) AS total_images,
                    COALESCE(SUM(CASE WHEN rt.status = 'completed' AND rm.kind = 'video' THEN 1 ELSE 0 END), 0) AS total_videos,
                    COALESCE(SUM(CASE WHEN rt.status = 'failed' THEN 1 ELSE 0 END), 0) AS total_errors,
                    COALESCE(SUM(CASE
                        WHEN rt.status = 'completed' AND rm.kind = 'image'
                             AND DATE(rt.completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_images,
                    COALESCE(SUM(CASE
                        WHEN rt.status = 'completed' AND rm.kind = 'video'
                             AND DATE(rt.completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_videos,
                    COALESCE(SUM(CASE
                        WHEN rt.status = 'failed' AND DATE(rt.completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS today_errors
                FROM runway_tasks rt
                LEFT JOIN runway_models rm ON rm.public_model_id = rt.public_model_id
                WHERE rt.api_key_id = ?
                """,
                (today, today, today, api_key_id),
            )
            add_row(await runway_cursor.fetchone())

        return stats

    async def get_system_info_stats(self) -> Dict[str, int]:
        """Get lightweight system counters used by admin dashboard"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT
                    COUNT(*) AS total_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN credits ELSE 0 END), 0) AS total_credits
                FROM tokens
            """)
            row = await cursor.fetchone()
            data = dict(row) if row else {}
            return {
                "total_tokens": int(data.get("total_tokens") or 0),
                "active_tokens": int(data.get("active_tokens") or 0),
                "total_credits": int(data.get("total_credits") or 0),
            }

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE is_active = 1 ORDER BY last_used_at ASC")
            rows = await cursor.fetchall()
            return [Token(**dict(row)) for row in rows]

    async def update_token(self, token_id: int, **kwargs):
        """Update token fields"""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                updates.append(f"{key} = ?")
                params.append(value)

            if updates:
                params.append(token_id)
                query = f"UPDATE tokens SET {', '.join(updates)} WHERE id = ?"
                await db.execute(query, params)
                await db.commit()

    async def delete_token(self, token_id: int):
        """Delete token and related data"""
        async with self._connect(write=True) as db:
            await db.execute("UPDATE request_logs SET token_id = NULL WHERE token_id = ?", (token_id,))
            await db.execute("UPDATE cache_files SET token_id = NULL WHERE token_id = ?", (token_id,))
            if await self._table_exists(db, "dedicated_extension_workers"):
                # Existing databases may still have inert legacy rows with a token FK.
                await db.execute(
                    "UPDATE dedicated_extension_workers SET token_id = NULL WHERE token_id = ?", (token_id,)
                )
            await db.execute("DELETE FROM tasks WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM token_stats WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM projects WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM api_key_accounts WHERE account_id = ?", (token_id,))
            await db.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            await db.commit()

    # Project operations
    async def add_project(self, project: Project) -> int:
        """Add a new project"""
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO projects (project_id, token_id, api_key_id, project_name, tool_name, is_active)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    project.project_id,
                    project.token_id,
                    project.api_key_id,
                    project.project_name,
                    project.tool_name,
                    project.is_active,
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def deactivate_projects_for_token_scope(self, token_id: int, api_key_id: Optional[int] = None) -> int:
        """Set prior projects inactive for one token scope.

        Scope is token + api_key when api_key_id is provided; otherwise token + NULL api_key.
        Returns affected row count.
        """
        async with self._connect(write=True) as db:
            if api_key_id is None:
                cursor = await db.execute(
                    """
                    UPDATE projects
                    SET is_active = 0
                    WHERE token_id = ? AND api_key_id IS NULL AND is_active = 1
                    """,
                    (token_id,),
                )
            else:
                cursor = await db.execute(
                    """
                    UPDATE projects
                    SET is_active = 0
                    WHERE token_id = ? AND api_key_id = ? AND is_active = 1
                    """,
                    (token_id, api_key_id),
                )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def get_project_by_id(self, project_id: str, api_key_id: Optional[int] = None) -> Optional[Project]:
        """Get project by UUID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if api_key_id is None:
                cursor = await db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
            else:
                cursor = await db.execute(
                    "SELECT * FROM projects WHERE project_id = ? AND api_key_id = ?",
                    (project_id, api_key_id),
                )
            row = await cursor.fetchone()
            if row:
                return Project(**dict(row))
            return None

    async def get_projects_by_token(self, token_id: int, api_key_id: Optional[int] = None) -> List[Project]:
        """Get all projects for a token"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if api_key_id is None:
                cursor = await db.execute(
                    "SELECT * FROM projects WHERE token_id = ? ORDER BY created_at DESC", (token_id,)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM projects WHERE token_id = ? AND api_key_id = ? ORDER BY created_at DESC",
                    (token_id, api_key_id),
                )
            rows = await cursor.fetchall()
            return [Project(**dict(row)) for row in rows]

    async def count_projects_by_api_key(self, api_key_id: int) -> int:
        """Count projects rows scoped to a managed API key."""
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM projects WHERE api_key_id = ?",
                (api_key_id,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    async def count_projects_for_api_key_account(self, api_key_id: int, token_id: int) -> int:
        """Count active projects for one token scoped to a managed API key."""
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM projects
                WHERE api_key_id = ? AND token_id = ? AND is_active = 1
                """,
                (api_key_id, token_id),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    async def list_projects_for_api_key_account(
        self, api_key_id: int, token_id: int, limit: int = 100, offset: int = 0
    ) -> List[Project]:
        """Paginated active projects for a token under a managed API key."""
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM projects
                WHERE api_key_id = ? AND token_id = ? AND is_active = 1
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (api_key_id, token_id, limit, offset),
            )
            rows = await cursor.fetchall()
            return [Project(**dict(row)) for row in rows]

    async def list_projects_by_api_key(self, api_key_id: int, limit: int = 10, offset: int = 0) -> List[Project]:
        """Paginated projects for a managed API key, newest first."""
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM projects
                WHERE api_key_id = ?
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (api_key_id, limit, offset),
            )
            rows = await cursor.fetchall()
            return [Project(**dict(row)) for row in rows]

    async def delete_project(self, project_id: str):
        """Delete project"""
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            await db.commit()

    # Task operations
    async def create_task(self, task: Task) -> int:
        """Create a new task"""
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO tasks (
                    task_id, token_id, api_key_id, project_id, model, prompt, status, progress,
                    result_urls, base_result_urls, delivery_urls,
                    requested_resolution, output_resolution, upscale_status, upscale_error_message,
                    scene_id, job_phase, captcha_status, captcha_detail
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    task.task_id,
                    task.token_id,
                    task.api_key_id,
                    task.project_id,
                    task.model,
                    task.prompt,
                    task.status,
                    task.progress,
                    json.dumps(task.result_urls) if isinstance(task.result_urls, list) else task.result_urls,
                    json.dumps(task.base_result_urls)
                    if isinstance(task.base_result_urls, list)
                    else task.base_result_urls,
                    json.dumps(task.delivery_urls) if isinstance(task.delivery_urls, list) else task.delivery_urls,
                    task.requested_resolution,
                    task.output_resolution,
                    task.upscale_status,
                    task.upscale_error_message,
                    task.scene_id,
                    task.job_phase,
                    task.captcha_status,
                    task.captcha_detail,
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = await cursor.fetchone()
            if row:
                task_dict = dict(row)
                # Parse result_urls from JSON
                if task_dict.get("result_urls"):
                    task_dict["result_urls"] = json.loads(task_dict["result_urls"])
                if task_dict.get("base_result_urls"):
                    task_dict["base_result_urls"] = json.loads(task_dict["base_result_urls"])
                if task_dict.get("delivery_urls"):
                    task_dict["delivery_urls"] = json.loads(task_dict["delivery_urls"])
                return Task(**task_dict)
            return None

    async def update_task(self, task_id: str, **kwargs):
        """Update task"""
        runtime = self.event_runtime
        status = str(kwargs.get("status") or "").strip().lower()
        terminal = status in {"completed", "failed", "cancelled", "canceled"} or kwargs.get("completed_at") is not None
        if runtime is not None and runtime.ready:
            await runtime.publish_task_progress("native", task_id, kwargs, terminal=terminal)
            progress_only = set(kwargs).issubset(
                {"status", "progress", "job_phase", "captcha_status", "captcha_detail"}
            )
            now = time.monotonic()
            progress_key = f"native:{task_id}"
            last_persisted = self._last_progress_persist.get(progress_key, 0.0)
            if progress_only and not terminal and now - last_persisted < 2.0:
                return
            self._last_progress_persist[progress_key] = now
            if terminal:
                self._last_progress_persist.pop(progress_key, None)
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    # Convert list to JSON string for result_urls
                    if key in {"result_urls", "base_result_urls", "delivery_urls"} and isinstance(value, list):
                        value = json.dumps(value)
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                params.append(task_id)
                query = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?"
                await db.execute(query, params)
                await db.commit()

    def _coerce_runway_task_row(self, row: Dict[str, Any]) -> RunwayTask:
        for key in ("raw_artifact_urls", "cached_artifact_urls"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    row[key] = json.loads(value)
                except Exception:
                    row[key] = [value]
        return RunwayTask(**row)

    async def get_runway_config(self) -> RunwayConfig:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM runway_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return RunwayConfig(**dict(row))
            return RunwayConfig()

    async def update_runway_config(
        self,
        *,
        enabled: Optional[bool] = None,
        base_url: Optional[str] = None,
        poll_interval_sec: Optional[float] = None,
        timeout_sec: Optional[float] = None,
        cache_outputs: Optional[bool] = None,
    ) -> RunwayConfig:
        current = await self.get_runway_config()
        values = {
            "enabled": int(current.enabled if enabled is None else bool(enabled)),
            "base_url": (base_url if base_url is not None else current.base_url).strip().rstrip("/")
            or "https://api.runwayml.com/v1",
            "poll_interval_sec": max(
                0.5, float(current.poll_interval_sec if poll_interval_sec is None else poll_interval_sec)
            ),
            "timeout_sec": max(10.0, float(current.timeout_sec if timeout_sec is None else timeout_sec)),
            "cache_outputs": int(current.cache_outputs if cache_outputs is None else bool(cache_outputs)),
        }
        async with self._connect(write=True) as db:
            await db.execute(
                """
                INSERT INTO runway_config (
                    id, enabled, base_url, poll_interval_sec, timeout_sec, cache_outputs, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    enabled = excluded.enabled,
                    base_url = excluded.base_url,
                    poll_interval_sec = excluded.poll_interval_sec,
                    timeout_sec = excluded.timeout_sec,
                    cache_outputs = excluded.cache_outputs,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    values["enabled"],
                    values["base_url"],
                    values["poll_interval_sec"],
                    values["timeout_sec"],
                    values["cache_outputs"],
                ),
            )
            await db.commit()
        return await self.get_runway_config()

    async def list_runway_accounts(self) -> List[RunwayAccount]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM runway_accounts ORDER BY is_active DESC, id ASC")
            rows = await cursor.fetchall()
            return [RunwayAccount(**dict(row)) for row in rows]

    async def get_runway_account(self, account_id: int) -> Optional[RunwayAccount]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM runway_accounts WHERE id = ?", (account_id,))
            row = await cursor.fetchone()
            return RunwayAccount(**dict(row)) if row else None

    async def create_runway_account(
        self,
        *,
        label: str,
        raw_credential: str,
        is_active: bool = True,
        workspace_id: Optional[str] = None,
        team_id: Optional[str] = None,
        concurrency_limit: int = 1,
        last_status: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> int:
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO runway_accounts (
                    label, raw_credential, is_active, workspace_id, team_id,
                    concurrency_limit, last_status, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (label or "").strip(),
                    raw_credential or "",
                    int(bool(is_active)),
                    workspace_id,
                    team_id,
                    int(concurrency_limit or 1),
                    last_status,
                    last_error,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def update_runway_account(self, account_id: int, **kwargs) -> None:
        allowed = {
            "label",
            "raw_credential",
            "is_active",
            "workspace_id",
            "team_id",
            "concurrency_limit",
            "in_flight",
            "last_status",
            "last_error",
            "last_used_at",
        }
        updates = []
        params = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key in {"is_active"}:
                value = int(bool(value))
            if key in {"concurrency_limit", "in_flight"} and value is not None:
                value = int(value)
            updates.append(f"{key} = ?")
            params.append(value)
        if not updates:
            return
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(account_id)
        async with self._connect(write=True) as db:
            await db.execute(
                f"UPDATE runway_accounts SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await db.commit()

    async def delete_runway_account(self, account_id: int) -> None:
        async with self._connect(write=True) as db:
            await db.execute(
                """
                UPDATE runway_tasks
                SET account_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ?
                """,
                (account_id,),
            )
            await db.execute("DELETE FROM runway_accounts WHERE id = ?", (account_id,))
            await db.commit()

    async def acquire_runway_account(self) -> Optional[RunwayAccount]:
        """Round-robin-ish active account acquisition with a simple in-flight guard."""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM runway_accounts
                WHERE is_active = 1
                  AND TRIM(COALESCE(raw_credential, '')) != ''
                  AND (concurrency_limit < 0 OR in_flight < concurrency_limit)
                ORDER BY COALESCE(last_used_at, '1970-01-01 00:00:00') ASC, id ASC
                LIMIT 1
                """
            )
            row = await cursor.fetchone()
            if not row:
                return None
            account = RunwayAccount(**dict(row))
            await db.execute(
                """
                UPDATE runway_accounts
                SET in_flight = in_flight + 1,
                    last_used_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (account.id,),
            )
            await db.commit()
            account.in_flight += 1
            return account

    async def release_runway_account(self, account_id: Optional[int]) -> None:
        if not account_id:
            return
        async with self._connect(write=True) as db:
            await db.execute(
                """
                UPDATE runway_accounts
                SET in_flight = CASE WHEN in_flight > 0 THEN in_flight - 1 ELSE 0 END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(account_id),),
            )
            await db.commit()

    async def list_runway_models(self, enabled_only: bool = False) -> List[RunwayModel]:
        query = "SELECT * FROM runway_models"
        params: tuple[Any, ...] = ()
        if enabled_only:
            query += " WHERE is_enabled = 1 AND live_available = 1"
        query += " ORDER BY is_enabled DESC, public_model_id ASC"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            return [RunwayModel(**dict(row)) for row in rows]

    async def get_runway_model(self, public_model_id: str) -> Optional[RunwayModel]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM runway_models WHERE public_model_id = ?",
                ((public_model_id or "").strip(),),
            )
            row = await cursor.fetchone()
            return RunwayModel(**dict(row)) if row else None

    async def upsert_runway_model(
        self,
        *,
        public_model_id: str,
        display_name: str,
        kind: str,
        task_type: str,
        builder_key: str = "",
        default_options: str = "{}",
        request_mapping: str = "{}",
        capability_schema: str = "{}",
        media_roles: str = "[]",
        supported_modes: str = "[]",
        limits: str = "{}",
        feature_flags: str = "[]",
        cost_feature: str = "",
        source_version: str = "",
        live_available: bool = True,
        disabled_reason: str = "",
        is_enabled: bool = True,
    ) -> int:
        public_id = (public_model_id or "").strip()
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO runway_models (
                    public_model_id, display_name, kind, task_type, builder_key,
                    default_options, request_mapping, capability_schema, media_roles,
                    supported_modes, limits, feature_flags, cost_feature, source_version,
                    live_available, disabled_reason, last_synced_at, is_enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(public_model_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    kind = excluded.kind,
                    task_type = excluded.task_type,
                    builder_key = excluded.builder_key,
                    default_options = excluded.default_options,
                    request_mapping = excluded.request_mapping,
                    capability_schema = excluded.capability_schema,
                    media_roles = excluded.media_roles,
                    supported_modes = excluded.supported_modes,
                    limits = excluded.limits,
                    feature_flags = excluded.feature_flags,
                    cost_feature = excluded.cost_feature,
                    source_version = excluded.source_version,
                    live_available = excluded.live_available,
                    disabled_reason = excluded.disabled_reason,
                    last_synced_at = CURRENT_TIMESTAMP,
                    is_enabled = excluded.is_enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    public_id,
                    (display_name or "").strip(),
                    (kind or "image").strip(),
                    (task_type or "").strip(),
                    (builder_key or "").strip(),
                    default_options or "{}",
                    request_mapping or "{}",
                    capability_schema or "{}",
                    media_roles or "[]",
                    supported_modes or "[]",
                    limits or "{}",
                    feature_flags or "[]",
                    (cost_feature or "").strip(),
                    (source_version or "").strip(),
                    int(bool(live_available)),
                    disabled_reason or "",
                    int(bool(is_enabled)),
                ),
            )
            await db.commit()
            if cursor.lastrowid:
                return int(cursor.lastrowid)
        model = await self.get_runway_model(public_id)
        return int(model.id or 0) if model else 0

    async def delete_runway_model(self, model_id: int) -> None:
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM runway_models WHERE id = ?", (model_id,))
            await db.commit()

    async def sync_default_runway_models(self, live_features: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
        count = 0
        blocked_count = 0
        live_features = live_features if isinstance(live_features, dict) else {}
        disabled_features, disabled_task_type_count = _extract_runway_disabled_features(live_features)
        synced_public_ids: Set[str] = set()
        for model in RUNWAY_DEFAULT_MODELS:
            existing = await self.get_runway_model(model["public_model_id"])
            synced_public_ids.add(str(model["public_model_id"]))
            builder_available = model.get("builder_key") != "unsupported"
            availability_keys = _runway_model_availability_keys(model)
            matched_disabled_key = next((key for key in availability_keys if key in disabled_features), "")
            disabled_by_features = bool(matched_disabled_key)
            live_available = bool(builder_available and not disabled_by_features)
            disabled_reason = str(model.get("disabled_reason") or "")
            if disabled_by_features and builder_available:
                reason = disabled_features.get(matched_disabled_key) or "runway_disabled"
                disabled_reason = f"Runway disabled task type: {matched_disabled_key}"
                if reason and reason not in {"runway_disabled", "permission_disabled"}:
                    disabled_reason = f"{disabled_reason} ({reason})"
                blocked_count += 1
            elif not builder_available and not disabled_reason:
                disabled_reason = "Exact task builder is not available"
            await self.upsert_runway_model(
                public_model_id=model["public_model_id"],
                display_name=model["display_name"],
                kind=model["kind"],
                task_type=model["task_type"],
                builder_key=model.get("builder_key", ""),
                default_options=json.dumps(model["default_options"], ensure_ascii=False),
                request_mapping=json.dumps(model.get("request_mapping", {}), ensure_ascii=False),
                capability_schema=json.dumps(model.get("option_schema", {}), ensure_ascii=False),
                media_roles=json.dumps(model.get("media_roles", []), ensure_ascii=False),
                supported_modes=json.dumps(model.get("supported_modes", []), ensure_ascii=False),
                limits=json.dumps(model.get("limits", {}), ensure_ascii=False),
                feature_flags=json.dumps(model.get("feature_flags", []), ensure_ascii=False),
                cost_feature=model.get("cost_feature", ""),
                source_version=RUNWAY_MANIFEST_VERSION,
                live_available=live_available,
                disabled_reason=disabled_reason,
                is_enabled=bool(existing.is_enabled)
                if existing
                else bool(model.get("default_enabled", True) and live_available),
            )
            count += 1
        for existing_model in await self.list_runway_models(enabled_only=False):
            public_id = str(existing_model.public_model_id or "")
            if public_id in synced_public_ids:
                continue
            availability_keys = _runway_model_availability_keys(
                {
                    "task_type": existing_model.task_type,
                    "cost_feature": existing_model.cost_feature,
                    "feature_flags": _runway_json_list(existing_model.feature_flags),
                }
            )
            matched_disabled_key = next((key for key in availability_keys if key in disabled_features), "")
            if not matched_disabled_key:
                continue
            if str(existing_model.builder_key or "") == "unsupported":
                continue
            reason = disabled_features.get(matched_disabled_key) or "runway_disabled"
            disabled_reason = f"Runway disabled task type: {matched_disabled_key}"
            if reason and reason not in {"runway_disabled", "permission_disabled"}:
                disabled_reason = f"{disabled_reason} ({reason})"
            await self.mark_runway_model_unavailable(public_id, disabled_reason)
            blocked_count += 1
        return {
            "synced": count,
            "blocked": blocked_count,
            "disabled_task_types": disabled_task_type_count,
        }

    async def mark_runway_model_unavailable(self, public_model_id: str, disabled_reason: str) -> None:
        async with self._connect(write=True) as db:
            await db.execute(
                """
                UPDATE runway_models
                SET live_available = 0, disabled_reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE public_model_id = ?
                """,
                (disabled_reason or "", (public_model_id or "").strip()),
            )
            await db.commit()

    async def create_runway_task(self, task: RunwayTask) -> int:
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO runway_tasks (
                    job_id, upstream_task_id, account_id, api_key_id, public_model_id,
                    prompt, status, progress, raw_artifact_urls, cached_artifact_urls,
                    request_payload, response_payload, error_message, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.job_id,
                    task.upstream_task_id,
                    task.account_id,
                    task.api_key_id,
                    task.public_model_id,
                    task.prompt,
                    task.status,
                    task.progress,
                    json.dumps(task.raw_artifact_urls)
                    if isinstance(task.raw_artifact_urls, list)
                    else task.raw_artifact_urls,
                    json.dumps(task.cached_artifact_urls)
                    if isinstance(task.cached_artifact_urls, list)
                    else task.cached_artifact_urls,
                    task.request_payload,
                    task.response_payload,
                    task.error_message,
                    task.completed_at,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def get_runway_task(self, job_id: str) -> Optional[RunwayTask]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM runway_tasks WHERE job_id = ?", (job_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return self._coerce_runway_task_row(dict(row))

    async def update_runway_task(self, job_id: str, **kwargs) -> None:
        runtime = self.event_runtime
        status = str(kwargs.get("status") or "").strip().lower()
        terminal = status in {"completed", "failed", "cancelled", "canceled"} or kwargs.get("completed_at") is not None
        if runtime is not None and runtime.ready:
            await runtime.publish_task_progress("runway", job_id, kwargs, terminal=terminal)
            progress_only = set(kwargs).issubset({"status", "progress"})
            now = time.monotonic()
            progress_key = f"runway:{job_id}"
            last_persisted = self._last_progress_persist.get(progress_key, 0.0)
            if progress_only and not terminal and now - last_persisted < 2.0:
                return
            self._last_progress_persist[progress_key] = now
            if terminal:
                self._last_progress_persist.pop(progress_key, None)
        allowed = {
            "upstream_task_id",
            "account_id",
            "api_key_id",
            "public_model_id",
            "prompt",
            "status",
            "progress",
            "raw_artifact_urls",
            "cached_artifact_urls",
            "request_payload",
            "response_payload",
            "error_message",
            "completed_at",
        }
        updates = []
        params = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key in {"raw_artifact_urls", "cached_artifact_urls"} and isinstance(value, list):
                value = json.dumps(value)
            updates.append(f"{key} = ?")
            params.append(value)
        if not updates:
            return
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(job_id)
        async with self._connect(write=True) as db:
            await db.execute(
                f"UPDATE runway_tasks SET {', '.join(updates)} WHERE job_id = ?",
                params,
            )
            await db.commit()

    def _coerce_geminigen_task_row(self, row: Dict[str, Any]) -> GeminiGenTask:
        for key in ("raw_artifact_urls", "cached_artifact_urls"):
            value = row.get(key)
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    row[key] = parsed if isinstance(parsed, list) else []
                except Exception:
                    row[key] = [value] if value else []
        return GeminiGenTask(**row)

    async def get_geminigen_config(self) -> GeminiGenConfig:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM geminigen_config WHERE id = 1")
            row = await cursor.fetchone()
            return GeminiGenConfig(**dict(row)) if row else GeminiGenConfig()

    async def update_geminigen_config(
        self,
        *,
        enabled: Optional[bool] = None,
        video_enabled: Optional[bool] = None,
        base_url: Optional[str] = None,
        poll_interval_image_sec: Optional[float] = None,
        poll_interval_video_sec: Optional[float] = None,
        timeout_image_sec: Optional[float] = None,
        timeout_video_sec: Optional[float] = None,
        global_image_concurrency: Optional[int] = None,
        global_video_concurrency: Optional[int] = None,
        cache_outputs: Optional[bool] = None,
    ) -> GeminiGenConfig:
        current = await self.get_geminigen_config()
        values = {
            "enabled": int(current.enabled if enabled is None else bool(enabled)),
            "video_enabled": int(current.video_enabled if video_enabled is None else bool(video_enabled)),
            "base_url": (base_url if base_url is not None else current.base_url).strip().rstrip("/")
            or "https://api.geminigen.ai",
            "poll_interval_image_sec": max(
                1.0,
                float(current.poll_interval_image_sec if poll_interval_image_sec is None else poll_interval_image_sec),
            ),
            "poll_interval_video_sec": max(
                2.0,
                float(current.poll_interval_video_sec if poll_interval_video_sec is None else poll_interval_video_sec),
            ),
            "timeout_image_sec": max(
                30.0, float(current.timeout_image_sec if timeout_image_sec is None else timeout_image_sec)
            ),
            "timeout_video_sec": max(
                60.0, float(current.timeout_video_sec if timeout_video_sec is None else timeout_video_sec)
            ),
            "global_image_concurrency": max(
                -1,
                int(current.global_image_concurrency if global_image_concurrency is None else global_image_concurrency),
            ),
            "global_video_concurrency": max(
                -1,
                int(current.global_video_concurrency if global_video_concurrency is None else global_video_concurrency),
            ),
            "cache_outputs": int(current.cache_outputs if cache_outputs is None else bool(cache_outputs)),
        }
        async with self._connect(write=True) as db:
            await db.execute(
                """
                INSERT INTO geminigen_config (
                    id, enabled, video_enabled, base_url, poll_interval_image_sec, poll_interval_video_sec,
                    timeout_image_sec, timeout_video_sec, global_image_concurrency,
                    global_video_concurrency, cache_outputs, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    enabled = excluded.enabled,
                    video_enabled = excluded.video_enabled,
                    base_url = excluded.base_url,
                    poll_interval_image_sec = excluded.poll_interval_image_sec,
                    poll_interval_video_sec = excluded.poll_interval_video_sec,
                    timeout_image_sec = excluded.timeout_image_sec,
                    timeout_video_sec = excluded.timeout_video_sec,
                    global_image_concurrency = excluded.global_image_concurrency,
                    global_video_concurrency = excluded.global_video_concurrency,
                    cache_outputs = excluded.cache_outputs,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    values["enabled"],
                    values["video_enabled"],
                    values["base_url"],
                    values["poll_interval_image_sec"],
                    values["poll_interval_video_sec"],
                    values["timeout_image_sec"],
                    values["timeout_video_sec"],
                    values["global_image_concurrency"],
                    values["global_video_concurrency"],
                    values["cache_outputs"],
                ),
            )
            await db.commit()
        return await self.get_geminigen_config()

    async def list_geminigen_accounts(self) -> List[GeminiGenAccount]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM geminigen_accounts ORDER BY is_active DESC, id ASC")
            return [GeminiGenAccount(**dict(row)) for row in await cursor.fetchall()]

    async def get_geminigen_generation_capacity(self) -> Dict[str, int]:
        """Return finite Nexus thread totals for active credentialed GeminiGen accounts."""
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT
                    COALESCE(SUM(
                        CASE
                            WHEN image_concurrency < 0 THEN 5
                            WHEN image_concurrency > 0 THEN image_concurrency
                            ELSE 0
                        END
                    ), 0) AS image_threads,
                    COALESCE(SUM(
                        CASE
                            WHEN video_concurrency < 0 THEN 5
                            WHEN video_concurrency > 0 THEN video_concurrency
                            ELSE 0
                        END
                    ), 0) AS video_threads
                FROM geminigen_accounts
                WHERE is_active = 1
                  AND TRIM(COALESCE(bearer_token, '')) != ''
                """
            )
            row = await cursor.fetchone()
            return {
                "image_threads": max(0, int(row[0] or 0)) if row else 0,
                "video_threads": max(0, int(row[1] or 0)) if row else 0,
            }

    async def get_geminigen_account_generation_stats(
        self,
        account_ids: Optional[List[int]] = None,
    ) -> Dict[int, Dict[str, int]]:
        today = self._current_stats_date()
        cleaned_ids = sorted({int(account_id) for account_id in (account_ids or []) if account_id})
        account_filter = ""
        params: List[Any] = [today, today]
        if cleaned_ids:
            placeholders = ",".join("?" for _ in cleaned_ids)
            account_filter = f" AND account_id IN ({placeholders})"
            params.extend(cleaned_ids)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT
                    account_id,
                    COALESCE(SUM(CASE WHEN kind = 'image' THEN 1 ELSE 0 END), 0) AS image_generated_total,
                    COALESCE(SUM(CASE
                        WHEN kind = 'image'
                             AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS image_generated_today,
                    COALESCE(SUM(CASE WHEN kind = 'video' THEN 1 ELSE 0 END), 0) AS video_generated_total,
                    COALESCE(SUM(CASE
                        WHEN kind = 'video'
                             AND DATE(completed_at, 'localtime') = ? THEN 1 ELSE 0 END), 0) AS video_generated_today
                FROM geminigen_tasks
                WHERE status = 'completed'
                  AND account_id IS NOT NULL
                  {account_filter}
                GROUP BY account_id
                """,
                params,
            )
            rows = await cursor.fetchall()
            return {
                int(row["account_id"]): {
                    "image_generated_today": int(row["image_generated_today"] or 0),
                    "image_generated_total": int(row["image_generated_total"] or 0),
                    "video_generated_today": int(row["video_generated_today"] or 0),
                    "video_generated_total": int(row["video_generated_total"] or 0),
                }
                for row in rows
                if row["account_id"] is not None
            }

    async def get_geminigen_account(self, account_id: int) -> Optional[GeminiGenAccount]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM geminigen_accounts WHERE id = ?", (int(account_id),))
            row = await cursor.fetchone()
            return GeminiGenAccount(**dict(row)) if row else None

    async def create_geminigen_account(
        self,
        *,
        label: str,
        raw_cookie: str,
        bearer_token: str = "",
        refresh_token: str = "",
        guard_id: str = "",
        turnstile_token: str = "",
        is_active: bool = True,
        image_concurrency: int = 5,
        video_concurrency: int = 5,
        last_status: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> int:
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO geminigen_accounts (
                    label, raw_cookie, bearer_token, refresh_token, guard_id, turnstile_token, is_active,
                    image_concurrency, video_concurrency, last_status, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (label or "").strip(),
                    raw_cookie or "",
                    bearer_token or "",
                    refresh_token or "",
                    guard_id or "",
                    turnstile_token or "",
                    int(bool(is_active)),
                    int(image_concurrency or 5),
                    int(video_concurrency or 5),
                    last_status,
                    last_error,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def update_geminigen_account(self, account_id: int, **kwargs) -> None:
        allowed = {
            "label",
            "raw_cookie",
            "bearer_token",
            "refresh_token",
            "guard_id",
            "turnstile_token",
            "is_active",
            "image_concurrency",
            "video_concurrency",
            "image_in_flight",
            "video_in_flight",
            "last_status",
            "last_error",
            "last_used_at",
            "image_gen_daily_limit_reset_at",
            "grok_image_daily_limit_reset_at",
            "video_daily_limit_reset_at",
            "profile_user_id",
            "profile_uuid",
            "profile_email",
            "profile_full_name",
            "profile_is_active",
            "available_credit",
            "plan_credit",
            "purchased_credit",
            "locked_credit",
            "subscription_credit",
            "plan_name",
            "plan_expire_at",
            "active_benefits_json",
            "remaining_bulk_videos",
            "is_image_gen_max",
            "is_image_premium",
            "image_gen_concurrent_streams",
            "remaining_image_gen_max_daily_images",
            "remaining_image_gen_free_daily_images",
            "image_gen_quota_synced_at",
            "is_grok_image_max",
            "grok_image_concurrent_streams",
            "remaining_grok_image_max_daily_images",
            "remaining_grok_image_free_daily_images",
            "grok_image_quota_synced_at",
            "is_grok_max",
            "grok_max_concurrent_streams",
            "grok_max_quota_synced_at",
            "remaining_daily_videos",
            "remaining_grok_max_daily_videos",
            "remaining_grok_max_daily_720p_videos",
            "remaining_grok_max_daily_10s_videos",
            "remaining_grok_max_daily_15s_videos",
            "quota_synced_at",
            "quota_sync_status",
            "quota_sync_error",
            "profile_synced_at",
            "profile_sync_status",
            "profile_sync_error",
        }
        updates = []
        params = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key == "is_active":
                value = int(bool(value))
            if key == "profile_is_active" and value is not None:
                value = int(bool(value))
            if (
                key in {"is_image_gen_max", "is_image_premium", "is_grok_image_max", "is_grok_max"}
                and value is not None
            ):
                value = int(bool(value))
            if (
                key
                in {
                    "image_concurrency",
                    "video_concurrency",
                    "image_in_flight",
                    "video_in_flight",
                    "profile_user_id",
                    "available_credit",
                    "plan_credit",
                    "purchased_credit",
                    "locked_credit",
                    "subscription_credit",
                    "remaining_bulk_videos",
                    "remaining_daily_videos",
                    "remaining_grok_max_daily_videos",
                    "remaining_grok_max_daily_720p_videos",
                    "remaining_grok_max_daily_10s_videos",
                    "remaining_grok_max_daily_15s_videos",
                    "image_gen_concurrent_streams",
                    "remaining_image_gen_max_daily_images",
                    "remaining_image_gen_free_daily_images",
                    "grok_image_concurrent_streams",
                    "remaining_grok_image_max_daily_images",
                    "remaining_grok_image_free_daily_images",
                    "grok_max_concurrent_streams",
                }
                and value is not None
            ):
                value = int(value)
            updates.append(f"{key} = ?")
            params.append(value)
        if not updates:
            return
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(int(account_id))
        async with self._connect(write=True) as db:
            await db.execute(f"UPDATE geminigen_accounts SET {', '.join(updates)} WHERE id = ?", params)
            await db.commit()

    async def delete_geminigen_account(self, account_id: int) -> bool:
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                "SELECT 1 FROM geminigen_accounts WHERE id = ? LIMIT 1",
                (int(account_id),),
            )
            if not await cursor.fetchone():
                return False
            # Keep completed/cancelled task history while removing the credential.
            # The nullable foreign key must be detached before deleting the account.
            await db.execute(
                """
                UPDATE geminigen_tasks
                SET account_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ?
                """,
                (int(account_id),),
            )
            await db.execute("DELETE FROM geminigen_accounts WHERE id = ?", (int(account_id),))
            await db.commit()
            return True

    @staticmethod
    def _geminigen_daily_limit_column(kind: str, endpoint_type: Optional[str] = None) -> str:
        endpoint = str(endpoint_type or "").strip().lower()
        if endpoint == "grok-image":
            return "grok_image_daily_limit_reset_at"
        if endpoint == "veo-video" or str(kind or "").lower() == "video":
            return "video_daily_limit_reset_at"
        return "image_gen_daily_limit_reset_at"

    async def acquire_geminigen_account(
        self,
        kind: str,
        excluded_account_ids: Optional[List[int]] = None,
        endpoint_type: Optional[str] = None,
    ) -> Optional[GeminiGenAccount]:
        is_video = str(kind or "").lower() == "video"
        limit_col = "video_concurrency" if is_video else "image_concurrency"
        inflight_col = "video_in_flight" if is_video else "image_in_flight"
        daily_limit_col = self._geminigen_daily_limit_column(kind, endpoint_type)
        excluded = [int(account_id) for account_id in (excluded_account_ids or []) if account_id]
        exclusion_clause = ""
        params: List[Any] = []
        if excluded:
            placeholders = ", ".join("?" for _ in excluded)
            exclusion_clause = f"AND id NOT IN ({placeholders})"
            params.extend(excluded)
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT * FROM geminigen_accounts
                WHERE is_active = 1
                  AND TRIM(COALESCE(bearer_token, '')) != ''
                  AND ({daily_limit_col} IS NULL OR {daily_limit_col} <= CURRENT_TIMESTAMP)
                  AND ({limit_col} < 0 OR {inflight_col} < {limit_col})
                  {exclusion_clause}
                ORDER BY COALESCE(last_used_at, '1970-01-01 00:00:00') ASC, id ASC
                LIMIT 1
                """,
                params,
            )
            row = await cursor.fetchone()
            if not row:
                return None
            account = GeminiGenAccount(**dict(row))
            await db.execute(
                f"""
                UPDATE geminigen_accounts
                SET {inflight_col} = {inflight_col} + 1,
                    last_used_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (account.id,),
            )
            await db.commit()
            if is_video:
                account.video_in_flight += 1
            else:
                account.image_in_flight += 1
            return account

    async def get_geminigen_daily_limit_pool_state(
        self,
        kind: str,
        endpoint_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Summarize persisted daily-limit eligibility for active credentialed accounts."""
        daily_limit_col = self._geminigen_daily_limit_column(kind, endpoint_type)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT
                    COUNT(*) AS account_count,
                    COALESCE(SUM(
                        CASE WHEN {daily_limit_col} > CURRENT_TIMESTAMP THEN 1 ELSE 0 END
                    ), 0) AS daily_limited_count,
                    MIN(
                        CASE WHEN {daily_limit_col} > CURRENT_TIMESTAMP
                             THEN {daily_limit_col} ELSE NULL END
                    ) AS earliest_reset_at
                FROM geminigen_accounts
                WHERE is_active = 1
                  AND TRIM(COALESCE(bearer_token, '')) != ''
                """
            )
            row = await cursor.fetchone()
            return {
                "account_count": int(row["account_count"] or 0) if row else 0,
                "daily_limited_count": int(row["daily_limited_count"] or 0) if row else 0,
                "earliest_reset_at": row["earliest_reset_at"] if row else None,
            }

    async def decrement_geminigen_cached_quota(self, account_id: int, endpoint_type: str) -> bool:
        """Atomically decrement a same-day cached image quota after submission."""
        endpoint = str(endpoint_type or "").strip().lower()
        quota_columns = {
            "imagen": (
                "is_image_gen_max",
                "remaining_image_gen_max_daily_images",
                "remaining_image_gen_free_daily_images",
                "image_gen_daily_limit_reset_at",
                "daily_limited_image_gen",
                "image_gen_quota_synced_at",
            ),
            "grok-image": (
                "is_grok_image_max",
                "remaining_grok_image_max_daily_images",
                "remaining_grok_image_free_daily_images",
                "grok_image_daily_limit_reset_at",
                "daily_limited_grok_image",
                "grok_image_quota_synced_at",
            ),
        }
        columns = quota_columns.get(endpoint)
        if not columns:
            return False
        flag_col, max_remaining_col, free_remaining_col, reset_col, limited_status, synced_col = columns
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT {flag_col} FROM geminigen_accounts WHERE id = ?",
                (int(account_id),),
            )
            row = await cursor.fetchone()
            if not row:
                return False
            remaining_col = max_remaining_col if bool(row[flag_col]) else free_remaining_col
            cursor = await db.execute(
                f"""
                UPDATE geminigen_accounts
                SET {remaining_col} = {remaining_col} - 1,
                    {reset_col} = CASE
                        WHEN {remaining_col} = 1 THEN datetime(date('now', '+1 day'))
                        ELSE {reset_col}
                    END,
                    last_status = CASE
                        WHEN {remaining_col} = 1 THEN ? ELSE last_status
                    END,
                    last_error = CASE
                        WHEN {remaining_col} = 1 THEN 'DAILY_LIMIT_EXCEEDED' ELSE last_error
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND DATE({synced_col}) = DATE('now')
                  AND {remaining_col} IS NOT NULL
                  AND {remaining_col} > 0
                """,
                (limited_status, int(account_id)),
            )
            await db.commit()
            return int(cursor.rowcount or 0) > 0

    async def release_geminigen_account(self, account_id: Optional[int], kind: str) -> None:
        if not account_id:
            return
        inflight_col = "video_in_flight" if str(kind or "").lower() == "video" else "image_in_flight"
        async with self._connect(write=True) as db:
            await db.execute(
                f"""
                UPDATE geminigen_accounts
                SET {inflight_col} = CASE WHEN {inflight_col} > 0 THEN {inflight_col} - 1 ELSE 0 END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(account_id),),
            )
            await db.commit()

    async def clear_geminigen_queue(
        self, account_id: Optional[int] = None, reason: str = "Manually cleared by admin"
    ) -> Dict[str, int]:
        now = datetime.utcnow()
        response = json.dumps(
            {"status": "manual_cleared", "reason": reason},
            ensure_ascii=False,
        )
        reset_params: List[Any] = []
        reset_where = ""
        task_params: List[Any] = []
        task_where = "status IN ('queued', 'processing')"
        if account_id is not None:
            reset_where = " WHERE id = ?"
            reset_params.append(int(account_id))
            task_where += " AND account_id = ?"
            task_params.append(int(account_id))
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            account_cursor = await db.execute(
                f"SELECT COUNT(*) FROM geminigen_accounts{reset_where}",
                reset_params,
            )
            account_count = int((await account_cursor.fetchone())[0] or 0)
            cursor = await db.execute(
                f"SELECT job_id, request_log_id FROM geminigen_tasks WHERE {task_where}",
                task_params,
            )
            rows = await cursor.fetchall()
            await db.execute(
                f"""
                UPDATE geminigen_accounts
                SET image_in_flight = 0,
                    video_in_flight = 0,
                    updated_at = CURRENT_TIMESTAMP
                {reset_where}
                """,
                reset_params,
            )
            await db.execute(
                f"""
                UPDATE geminigen_tasks
                SET status = 'cancelled',
                    error_message = ?,
                    completed_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE {task_where}
                """,
                [reason, now, *task_params],
            )
            log_count = 0
            for row in rows:
                log_id = row["request_log_id"]
                if not log_id:
                    continue
                body = json.dumps(
                    {
                        "status": "manual_cleared",
                        "job_id": row["job_id"],
                        "reason": reason,
                    },
                    ensure_ascii=False,
                )
                await db.execute(
                    """
                    UPDATE request_logs
                    SET response_body = ?,
                        status_code = 499,
                        status_text = 'manual_cleared',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (body or response, int(log_id)),
                )
                log_count += 1
            await db.commit()
            return {
                "accounts_reset": account_count,
                "tasks_cleared": len(rows),
                "logs_updated": log_count,
            }

    async def cancel_geminigen_task_by_log_id(self, log_id: int, reason: str = "Cancelled by admin") -> Dict[str, Any]:
        now = datetime.utcnow()
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            log_cursor = await db.execute(
                "SELECT id, operation, request_body, status_code, status_text FROM request_logs WHERE id = ? LIMIT 1",
                (int(log_id),),
            )
            log_row = await log_cursor.fetchone()
            if not log_row:
                return {"success": False, "error": "Request log not found", "status_code": 404}
            operation = str(log_row["operation"] or "")
            if operation not in {"geminigen_image", "geminigen_video"}:
                return {"success": False, "error": "Log is not a GeminiGen generation", "status_code": 400}

            task_cursor = await db.execute(
                """
                SELECT *
                FROM geminigen_tasks
                WHERE request_log_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(log_id),),
            )
            task = await task_cursor.fetchone()
            if not task:
                return {"success": False, "error": "GeminiGen task not found for this log", "status_code": 404}

            job_id = str(task["job_id"] or "")
            status = str(task["status"] or "")
            if status in {"completed", "failed", "cancelled"}:
                return {
                    "success": True,
                    "job_id": job_id,
                    "status": status,
                    "already_terminal": True,
                    "task_cancelled": False,
                    "slot_released": False,
                }

            account_id = task["account_id"]
            kind = str(task["kind"] or "image")
            await db.execute(
                """
                UPDATE geminigen_tasks
                SET status = 'cancelled',
                    error_message = ?,
                    completed_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                """,
                (reason, now, job_id),
            )
            slot_released = False
            if account_id:
                inflight_col = "video_in_flight" if kind.lower() == "video" else "image_in_flight"
                await db.execute(
                    f"""
                    UPDATE geminigen_accounts
                    SET {inflight_col} = CASE WHEN {inflight_col} > 0 THEN {inflight_col} - 1 ELSE 0 END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (int(account_id),),
                )
                slot_released = True

            await db.execute(
                """
                UPDATE request_logs
                SET response_body = ?,
                    status_code = 499,
                    status_text = 'cancelled',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    json.dumps(
                        {
                            "status": "cancelled",
                            "job_id": job_id,
                            "reason": reason,
                        },
                        ensure_ascii=False,
                    ),
                    int(log_id),
                ),
            )
            await db.commit()
            return {
                "success": True,
                "job_id": job_id,
                "status": "cancelled",
                "already_terminal": False,
                "task_cancelled": True,
                "slot_released": slot_released,
            }

    async def create_geminigen_task(self, task: GeminiGenTask) -> int:
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO geminigen_tasks (
                    job_id, upstream_uuid, account_id, api_key_id, request_log_id, public_model_id, kind,
                    endpoint_type, prompt, status, progress, raw_artifact_urls,
                    cached_artifact_urls, request_payload, response_payload, error_message,
                    error_code, retry_at, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.job_id,
                    task.upstream_uuid,
                    task.account_id,
                    task.api_key_id,
                    task.request_log_id,
                    task.public_model_id,
                    task.kind,
                    task.endpoint_type,
                    task.prompt,
                    task.status,
                    task.progress,
                    json.dumps(task.raw_artifact_urls)
                    if isinstance(task.raw_artifact_urls, list)
                    else task.raw_artifact_urls,
                    json.dumps(task.cached_artifact_urls)
                    if isinstance(task.cached_artifact_urls, list)
                    else task.cached_artifact_urls,
                    task.request_payload,
                    task.response_payload,
                    task.error_message,
                    task.error_code,
                    task.retry_at,
                    task.started_at,
                    task.completed_at,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def create_geminigen_task_with_request_log(
        self,
        task: GeminiGenTask,
        log: RequestLog,
    ) -> int:
        """Create the initial GeminiGen task and request summary atomically."""
        raw_request_body = log.request_body
        raw_response_body = log.response_body
        summary = (
            self.log_payload_manager.summary_fields(raw_request_body, raw_response_body)
            if self.log_payload_manager is not None
            else {
                "request_body": raw_request_body,
                "response_body": raw_response_body,
                "request_excerpt": str(raw_request_body or "")[:1024],
                "response_excerpt": str(raw_response_body or "")[:1024],
                "request_size_bytes": len(str(raw_request_body or "").encode("utf-8", errors="replace")),
                "response_size_bytes": len(str(raw_response_body or "").encode("utf-8", errors="replace")),
            }
        )
        async with self._connect(write=True) as db:
            log_cursor = await db.execute(
                """
                INSERT INTO request_logs (
                    token_id, api_key_id, operation, request_body, response_body,
                    status_code, duration, status_text, progress,
                    request_excerpt, response_excerpt, request_size_bytes, response_size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log.token_id,
                    log.api_key_id,
                    log.operation,
                    summary["request_body"],
                    summary["response_body"],
                    log.status_code,
                    log.duration,
                    log.status_text or "",
                    log.progress,
                    summary["request_excerpt"],
                    summary["response_excerpt"],
                    summary["request_size_bytes"],
                    summary["response_size_bytes"],
                ),
            )
            request_log_id = int(log_cursor.lastrowid)
            task.request_log_id = request_log_id
            await db.execute(
                """
                INSERT INTO geminigen_tasks (
                    job_id, upstream_uuid, account_id, api_key_id, request_log_id, public_model_id, kind,
                    endpoint_type, prompt, status, progress, raw_artifact_urls,
                    cached_artifact_urls, request_payload, response_payload, error_message,
                    error_code, retry_at, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.job_id,
                    task.upstream_uuid,
                    task.account_id,
                    task.api_key_id,
                    request_log_id,
                    task.public_model_id,
                    task.kind,
                    task.endpoint_type,
                    task.prompt,
                    task.status,
                    task.progress,
                    json.dumps(task.raw_artifact_urls)
                    if isinstance(task.raw_artifact_urls, list)
                    else task.raw_artifact_urls,
                    json.dumps(task.cached_artifact_urls)
                    if isinstance(task.cached_artifact_urls, list)
                    else task.cached_artifact_urls,
                    task.request_payload,
                    task.response_payload,
                    task.error_message,
                    task.error_code,
                    task.retry_at,
                    task.started_at,
                    task.completed_at,
                ),
            )
            await db.commit()
        if self.log_payload_manager is not None:
            self.log_payload_manager.capture_initial(
                request_log_id,
                request_body=raw_request_body,
                response_body=raw_response_body,
                status_code=log.status_code,
                status_text=log.status_text or "",
            )
        if self.event_runtime is not None and self.event_runtime.ready:
            await self.event_runtime.publish(
                "request_summary_created",
                {
                    "id": request_log_id,
                    "api_key_id": log.api_key_id,
                    "operation": log.operation,
                    "status_code": log.status_code,
                    "status_text": log.status_text or "",
                    "progress": log.progress,
                    "job_id": task.job_id,
                },
                persist=False,
            )
        return request_log_id

    async def get_geminigen_task(self, job_id: str) -> Optional[GeminiGenTask]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM geminigen_tasks WHERE job_id = ?", ((job_id or "").strip(),))
            row = await cursor.fetchone()
            return self._coerce_geminigen_task_row(dict(row)) if row else None

    async def list_active_geminigen_tasks(self, limit: int = 100) -> List[GeminiGenTask]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM geminigen_tasks
                WHERE status IN ('queued', 'processing')
                  AND TRIM(COALESCE(upstream_uuid, '')) != ''
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            )
            rows = await cursor.fetchall()
            return [self._coerce_geminigen_task_row(dict(row)) for row in rows]

    async def update_geminigen_task(self, job_id: str, **kwargs) -> None:
        runtime = self.event_runtime
        status = str(kwargs.get("status") or "").strip().lower()
        terminal = status in {"completed", "failed", "cancelled", "canceled"} or kwargs.get("completed_at") is not None
        if runtime is not None and runtime.ready:
            await runtime.publish_task_progress("geminigen", job_id, kwargs, terminal=terminal)
            progress_only = set(kwargs).issubset({"status", "progress"})
            now = time.monotonic()
            progress_key = f"geminigen:{job_id}"
            last_persisted = self._last_progress_persist.get(progress_key, 0.0)
            if progress_only and not terminal and now - last_persisted < 2.0:
                return
            self._last_progress_persist[progress_key] = now
            if terminal:
                self._last_progress_persist.pop(progress_key, None)
        allowed = {
            "upstream_uuid",
            "account_id",
            "api_key_id",
            "request_log_id",
            "public_model_id",
            "kind",
            "endpoint_type",
            "prompt",
            "status",
            "progress",
            "raw_artifact_urls",
            "cached_artifact_urls",
            "request_payload",
            "response_payload",
            "error_message",
            "error_code",
            "retry_at",
            "started_at",
            "completed_at",
        }
        updates = []
        params = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key in {"raw_artifact_urls", "cached_artifact_urls"} and isinstance(value, list):
                value = json.dumps(value)
            updates.append(f"{key} = ?")
            params.append(value)
        if not updates:
            return
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append((job_id or "").strip())
        async with self._connect(write=True) as db:
            await db.execute(f"UPDATE geminigen_tasks SET {', '.join(updates)} WHERE job_id = ?", params)
            await db.commit()

    # Token stats operations (kept for compatibility, now delegates to specific methods)
    async def increment_token_stats(self, token_id: int, stat_type: str):
        """Increment token statistics (delegates to specific methods)"""
        if stat_type == "image":
            await self.increment_image_count(token_id)
        elif stat_type == "video":
            await self.increment_video_count(token_id)
        elif stat_type == "error":
            await self.increment_error_count(token_id)

    async def increment_operation_stat(self, operation: str, *, success: bool) -> None:
        """Increment durable counters for non-token operations such as Adobe metadata."""
        op = str(operation or "").strip()
        if not op:
            return
        today = self._current_stats_date()
        total_col = "success_count" if success else "error_count"
        today_col = "today_success_count" if success else "today_error_count"
        async with self._connect(write=True) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO operation_stats (operation, today_date)
                VALUES (?, ?)
                """,
                (op, today),
            )
            await db.execute(
                """
                UPDATE operation_stats
                SET today_success_count = 0,
                    today_error_count = 0,
                    today_date = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE operation = ?
                  AND COALESCE(today_date, '') != ?
                """,
                (today, op, today),
            )
            await db.execute(
                f"""
                UPDATE operation_stats
                SET {total_col} = {total_col} + 1,
                    {today_col} = {today_col} + 1,
                    today_date = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE operation = ?
                """,
                (today, op),
            )
            await db.commit()

    async def get_operation_stats(self, operation: str) -> Dict[str, int]:
        """Return durable operation counters with daily values scoped to today's date."""
        op = str(operation or "").strip()
        if not op:
            return {
                "success_count": 0,
                "error_count": 0,
                "today_success_count": 0,
                "today_error_count": 0,
            }
        today = self._current_stats_date()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT success_count, error_count, today_success_count, today_error_count, today_date
                FROM operation_stats
                WHERE operation = ?
                """,
                (op,),
            )
            row = await cursor.fetchone()
        if not row:
            return {
                "success_count": 0,
                "error_count": 0,
                "today_success_count": 0,
                "today_error_count": 0,
            }
        data = dict(row)
        same_day = str(data.get("today_date") or "") == today
        return {
            "success_count": int(data.get("success_count") or 0),
            "error_count": int(data.get("error_count") or 0),
            "today_success_count": int(data.get("today_success_count") or 0) if same_day else 0,
            "today_error_count": int(data.get("today_error_count") or 0) if same_day else 0,
        }

    async def get_token_stats(self, token_id: int) -> Optional[TokenStats]:
        """Get token statistics"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return TokenStats(**dict(row))
            return None

    async def increment_image_count(self, token_id: int):
        """Increment image generation count with daily reset"""
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's image usage.
            if row and row[0] != today:
                await db.execute(
                    """
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = 1,
                        today_video_count = 0,
                        today_error_count = 0,
                        today_date = ?
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            else:
                # Same day, just increment both
                await db.execute(
                    """
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = today_image_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            await db.commit()

    async def increment_video_count(self, token_id: int):
        """Increment video generation count with daily reset"""
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's video usage.
            if row and row[0] != today:
                await db.execute(
                    """
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_image_count = 0,
                        today_video_count = 1,
                        today_error_count = 0,
                        today_date = ?
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            else:
                # Same day, just increment both
                await db.execute(
                    """
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = today_video_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            await db.commit()

    async def increment_error_count(self, token_id: int):
        """Increment error count with daily reset

        Updates two counters:
        - error_count: Historical total errors (never reset)
        - consecutive_error_count: Consecutive errors (reset on success/enable)
        - today_error_count: Today's errors (reset on date change)
        """
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's error.
            if row and row[0] != today:
                await db.execute(
                    """
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_image_count = 0,
                        today_video_count = 0,
                        today_error_count = 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            else:
                # Same day, just increment all counters
                await db.execute(
                    """
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = today_error_count + 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """,
                    (today, token_id),
                )
            await db.commit()

    async def reset_error_count(self, token_id: int):
        """Reset consecutive error count (only reset consecutive_error_count, keep error_count and today_error_count)

        This is called when:
        - Token is manually enabled by admin
        - Request succeeds (resets consecutive error counter)

        Note: error_count (total historical errors) is NEVER reset
        """
        async with self._connect(write=True) as db:
            await db.execute(
                """
                UPDATE token_stats SET consecutive_error_count = 0 WHERE token_id = ?
            """,
                (token_id,),
            )
            await db.commit()

    # Config operations
    async def get_admin_config(self) -> Optional[AdminConfig]:
        """Get admin configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM admin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return AdminConfig(**dict(row))
            return None

    async def update_admin_config(self, **kwargs):
        """Update admin configuration"""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                query = f"UPDATE admin_config SET {', '.join(updates)} WHERE id = 1"
                await db.execute(query, params)
                await db.commit()

    async def insert_admin_session(self, token: str, expires_at_unix: int) -> None:
        now = int(datetime.now().timestamp())
        async with self._connect(write=True) as db:
            await db.execute(
                """
                INSERT INTO admin_sessions (token, created_at, expires_at, last_used_at)
                VALUES (?, ?, ?, ?)
                """,
                (token, now, expires_at_unix, now),
            )
            await db.commit()

    async def is_admin_session_valid(self, token: str) -> bool:
        now = int(datetime.now().timestamp())
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                "SELECT expires_at FROM admin_sessions WHERE token = ?",
                (token,),
            )
            row = await cursor.fetchone()
            if not row:
                return False
            expires_at = int(row[0])
            if expires_at <= now:
                try:
                    await db.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
                    await db.commit()
                except Exception as exc:
                    if not is_sqlite_storage_full_error(exc):
                        raise
                return False
            try:
                await db.execute(
                    "UPDATE admin_sessions SET last_used_at = ? WHERE token = ?",
                    (now, token),
                )
                await db.commit()
            except Exception as exc:
                # Activity timestamps are housekeeping. Keep an already valid
                # session usable so the admin can reclaim cached disk space.
                if not is_sqlite_storage_full_error(exc):
                    raise
            return True

    async def is_admin_session_recent(self, token: str, max_age_seconds: int = 900) -> bool:
        """Return whether the admin authenticated within the requested safety window."""
        cutoff = int(datetime.now().timestamp()) - max(60, int(max_age_seconds))
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT created_at FROM admin_sessions WHERE token = ?",
                (token,),
            )
            row = await cursor.fetchone()
            return bool(row and int(row[0]) >= cutoff)

    async def delete_admin_session(self, token: str) -> None:
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
            await db.commit()

    async def delete_all_admin_sessions(self) -> None:
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM admin_sessions")
            await db.commit()

    async def get_proxy_config(self) -> Optional[ProxyConfig]:
        """Get proxy configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return ProxyConfig(**dict(row))
            return None

    async def update_proxy_config(
        self,
        enabled: bool,
        proxy_url: Optional[str] = None,
        media_proxy_enabled: Optional[bool] = None,
        media_proxy_url: Optional[str] = None,
    ):
        """Update proxy configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                new_media_proxy_enabled = (
                    media_proxy_enabled
                    if media_proxy_enabled is not None
                    else current.get("media_proxy_enabled", False)
                )
                new_media_proxy_url = media_proxy_url if media_proxy_url is not None else current.get("media_proxy_url")

                await db.execute(
                    """
                    UPDATE proxy_config
                    SET enabled = ?, proxy_url = ?,
                        media_proxy_enabled = ?, media_proxy_url = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (enabled, proxy_url, new_media_proxy_enabled, new_media_proxy_url),
                )
            else:
                new_media_proxy_enabled = media_proxy_enabled if media_proxy_enabled is not None else False
                new_media_proxy_url = media_proxy_url
                await db.execute(
                    """
                    INSERT INTO proxy_config (id, enabled, proxy_url, media_proxy_enabled, media_proxy_url)
                    VALUES (1, ?, ?, ?, ?)
                """,
                    (enabled, proxy_url, new_media_proxy_enabled, new_media_proxy_url),
                )

            await db.commit()

    async def get_generation_config(self) -> Optional[GenerationConfig]:
        """Get generation configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return GenerationConfig(**dict(row))
            return None

    async def update_generation_config(
        self,
        image_timeout: Optional[int] = None,
        video_timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        extension_generation_enabled: Optional[bool] = None,
        extension_generation_fallback_mode: Optional[str] = None,
        sub2gen_gemini_api_keys: Optional[str] = None,
        sub2gen_openai_api_keys: Optional[str] = None,
        sub2gen_openrouter_api_keys: Optional[str] = None,
        sub2gen_third_party_gemini_api_keys: Optional[str] = None,
        sub2gen_third_party_gemini_base_url: Optional[str] = None,
        cloudflare_account_id: Optional[str] = None,
        cloudflare_api_token: Optional[str] = None,
        sub2gen_csvgen_cookie: Optional[str] = None,
        sub2gen_csvgen_api_keys: Optional[str] = None,
        sub2gen_cloning_model: Optional[str] = None,
        sub2gen_cloning_backend: Optional[str] = None,
        sub2gen_cloning_provider_order: Optional[str] = None,
        sub2gen_cloning_enabled_providers: Optional[str] = None,
        sub2gen_cloning_provider_retry_count: Optional[int] = None,
        sub2gen_cloning_gemini_api_keys: Optional[str] = None,
        sub2gen_cloning_openai_api_keys: Optional[str] = None,
        sub2gen_cloning_openrouter_api_keys: Optional[str] = None,
        sub2gen_cloning_third_party_gemini_api_keys: Optional[str] = None,
        sub2gen_cloning_third_party_gemini_base_url: Optional[str] = None,
        sub2gen_cloning_cloudflare_account_id: Optional[str] = None,
        sub2gen_cloning_cloudflare_api_token: Optional[str] = None,
        sub2gen_metadata_backend: Optional[str] = None,
        sub2gen_metadata_provider_order: Optional[str] = None,
        sub2gen_metadata_enabled_providers: Optional[str] = None,
        sub2gen_metadata_provider_retry_count: Optional[int] = None,
        sub2gen_metadata_model: Optional[str] = None,
        sub2gen_metadata_enabled_models: Optional[str] = None,
        sub2gen_metadata_primary_model: Optional[str] = None,
        sub2gen_metadata_fallback_models: Optional[str] = None,
        metadata_system_prompt: Optional[str] = None,
        cloning_image_system_prompt: Optional[str] = None,
        cloning_video_system_prompt: Optional[str] = None,
        task_tracker_device_id: Optional[str] = None,
        task_tracker_device_name: Optional[str] = None,
        task_tracker_cookies: Optional[str] = None,
        task_tracker_device_token: Optional[str] = None,
        task_tracker_turnstile_token: Optional[str] = None,
        task_tracker_tls_profile: Optional[str] = None,
    ):
        """Update generation configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            current = dict(row) if row else {}

            normalized_image_timeout = image_timeout if image_timeout is not None else current.get("image_timeout", 300)
            normalized_video_timeout = (
                video_timeout if video_timeout is not None else current.get("video_timeout", 1500)
            )
            try:
                normalized_max_retries = (
                    max(1, int(max_retries)) if max_retries is not None else max(1, int(current.get("max_retries", 3)))
                )
            except Exception:
                normalized_max_retries = 3
            normalized_extension_generation_enabled = (
                bool(extension_generation_enabled)
                if extension_generation_enabled is not None
                else bool(current.get("extension_generation_enabled", False))
            )
            normalized_extension_generation_fallback_mode = (
                str(
                    extension_generation_fallback_mode
                    if extension_generation_fallback_mode is not None
                    else current.get("extension_generation_fallback_mode", "local_http_on_recaptcha")
                )
                .strip()
                .lower()
            )
            if normalized_extension_generation_fallback_mode not in {"none", "local_http_on_recaptcha"}:
                normalized_extension_generation_fallback_mode = "local_http_on_recaptcha"
            normalized_sub2gen_gemini_api_keys = (
                str(sub2gen_gemini_api_keys)
                if sub2gen_gemini_api_keys is not None
                else str(current.get("sub2gen_gemini_api_keys", "") or "")
            )
            normalized_sub2gen_openai_api_keys = (
                str(sub2gen_openai_api_keys)
                if sub2gen_openai_api_keys is not None
                else str(current.get("sub2gen_openai_api_keys", "") or "")
            )
            normalized_sub2gen_openrouter_api_keys = (
                str(sub2gen_openrouter_api_keys)
                if sub2gen_openrouter_api_keys is not None
                else str(current.get("sub2gen_openrouter_api_keys", "") or "")
            )
            normalized_sub2gen_third_party_gemini_api_keys = (
                str(sub2gen_third_party_gemini_api_keys)
                if sub2gen_third_party_gemini_api_keys is not None
                else str(current.get("sub2gen_third_party_gemini_api_keys", "") or "")
            )
            normalized_sub2gen_third_party_gemini_base_url = (
                str(sub2gen_third_party_gemini_base_url)
                if sub2gen_third_party_gemini_base_url is not None
                else str(current.get("sub2gen_third_party_gemini_base_url", "") or "")
            )
            normalized_cloudflare_account_id = (
                str(cloudflare_account_id)
                if cloudflare_account_id is not None
                else str(current.get("cloudflare_account_id", "") or "")
            )
            normalized_cloudflare_api_token = (
                str(cloudflare_api_token)
                if cloudflare_api_token is not None
                else str(current.get("cloudflare_api_token", "") or "")
            )
            normalized_sub2gen_csvgen_cookie = (
                str(sub2gen_csvgen_cookie)
                if sub2gen_csvgen_cookie is not None
                else str(current.get("sub2gen_csvgen_cookie", "") or "")
            )
            normalized_sub2gen_csvgen_api_keys = (
                str(sub2gen_csvgen_api_keys)
                if sub2gen_csvgen_api_keys is not None
                else str(current.get("sub2gen_csvgen_api_keys", "") or "")
            )
            if sub2gen_cloning_model is not None:
                raw_cm = str(sub2gen_cloning_model).strip()
                normalized_sub2gen_cloning_model = (
                    raw_cm
                    if raw_cm
                    else str(current.get("sub2gen_cloning_model", "gemini-2.5-flash") or "gemini-2.5-flash")
                )
            else:
                normalized_sub2gen_cloning_model = str(
                    current.get("sub2gen_cloning_model", "gemini-2.5-flash") or "gemini-2.5-flash"
                )
            if sub2gen_cloning_backend is not None:
                raw_be = str(sub2gen_cloning_backend).strip()
                normalized_sub2gen_cloning_backend = (
                    raw_be
                    if raw_be
                    else str(current.get("sub2gen_cloning_backend", "gemini_native") or "gemini_native")
                )
            else:
                normalized_sub2gen_cloning_backend = str(
                    current.get("sub2gen_cloning_backend", "gemini_native") or "gemini_native"
                )
            normalized_sub2gen_cloning_provider_order = (
                str(sub2gen_cloning_provider_order)
                if sub2gen_cloning_provider_order is not None
                else str(current.get("sub2gen_cloning_provider_order", "") or "")
            )
            normalized_sub2gen_cloning_enabled_providers = (
                str(sub2gen_cloning_enabled_providers)
                if sub2gen_cloning_enabled_providers is not None
                else str(current.get("sub2gen_cloning_enabled_providers", "") or "")
            )
            try:
                normalized_sub2gen_cloning_provider_retry_count = (
                    max(0, min(5, int(sub2gen_cloning_provider_retry_count)))
                    if sub2gen_cloning_provider_retry_count is not None
                    else max(0, min(5, int(current.get("sub2gen_cloning_provider_retry_count", 1) or 1)))
                )
            except Exception:
                normalized_sub2gen_cloning_provider_retry_count = 1
            normalized_sub2gen_cloning_gemini_api_keys = (
                str(sub2gen_cloning_gemini_api_keys)
                if sub2gen_cloning_gemini_api_keys is not None
                else str(current.get("sub2gen_cloning_gemini_api_keys", "") or "")
            )
            normalized_sub2gen_cloning_openai_api_keys = (
                str(sub2gen_cloning_openai_api_keys)
                if sub2gen_cloning_openai_api_keys is not None
                else str(current.get("sub2gen_cloning_openai_api_keys", "") or "")
            )
            normalized_sub2gen_cloning_openrouter_api_keys = (
                str(sub2gen_cloning_openrouter_api_keys)
                if sub2gen_cloning_openrouter_api_keys is not None
                else str(current.get("sub2gen_cloning_openrouter_api_keys", "") or "")
            )
            normalized_sub2gen_cloning_third_party_gemini_api_keys = (
                str(sub2gen_cloning_third_party_gemini_api_keys)
                if sub2gen_cloning_third_party_gemini_api_keys is not None
                else str(current.get("sub2gen_cloning_third_party_gemini_api_keys", "") or "")
            )
            normalized_sub2gen_cloning_third_party_gemini_base_url = (
                str(sub2gen_cloning_third_party_gemini_base_url)
                if sub2gen_cloning_third_party_gemini_base_url is not None
                else str(current.get("sub2gen_cloning_third_party_gemini_base_url", "") or "")
            )
            normalized_sub2gen_cloning_cloudflare_account_id = (
                str(sub2gen_cloning_cloudflare_account_id)
                if sub2gen_cloning_cloudflare_account_id is not None
                else str(current.get("sub2gen_cloning_cloudflare_account_id", "") or "")
            )
            normalized_sub2gen_cloning_cloudflare_api_token = (
                str(sub2gen_cloning_cloudflare_api_token)
                if sub2gen_cloning_cloudflare_api_token is not None
                else str(current.get("sub2gen_cloning_cloudflare_api_token", "") or "")
            )
            normalized_sub2gen_metadata_backend = (
                str(sub2gen_metadata_backend)
                if sub2gen_metadata_backend is not None
                else str(current.get("sub2gen_metadata_backend", "gemini_native") or "gemini_native")
            )
            normalized_sub2gen_metadata_provider_order = (
                str(sub2gen_metadata_provider_order)
                if sub2gen_metadata_provider_order is not None
                else str(current.get("sub2gen_metadata_provider_order", "") or "")
            )
            normalized_sub2gen_metadata_enabled_providers = (
                str(sub2gen_metadata_enabled_providers)
                if sub2gen_metadata_enabled_providers is not None
                else str(current.get("sub2gen_metadata_enabled_providers", "") or "")
            )
            try:
                normalized_sub2gen_metadata_provider_retry_count = (
                    max(0, min(5, int(sub2gen_metadata_provider_retry_count)))
                    if sub2gen_metadata_provider_retry_count is not None
                    else max(0, min(5, int(current.get("sub2gen_metadata_provider_retry_count", 1) or 1)))
                )
            except Exception:
                normalized_sub2gen_metadata_provider_retry_count = 1
            normalized_sub2gen_metadata_model = (
                str(sub2gen_metadata_model)
                if sub2gen_metadata_model is not None
                else str(current.get("sub2gen_metadata_model", "gemini-2.5-flash") or "gemini-2.5-flash")
            )
            normalized_sub2gen_metadata_enabled_models = (
                str(sub2gen_metadata_enabled_models)
                if sub2gen_metadata_enabled_models is not None
                else str(current.get("sub2gen_metadata_enabled_models", "") or "")
            )
            normalized_sub2gen_metadata_primary_model = (
                str(sub2gen_metadata_primary_model)
                if sub2gen_metadata_primary_model is not None
                else str(current.get("sub2gen_metadata_primary_model", "") or "")
            )
            normalized_sub2gen_metadata_fallback_models = (
                str(sub2gen_metadata_fallback_models)
                if sub2gen_metadata_fallback_models is not None
                else str(current.get("sub2gen_metadata_fallback_models", "") or "")
            )
            if sub2gen_metadata_model is None and sub2gen_metadata_primary_model is not None:
                normalized_sub2gen_metadata_model = (
                    str(sub2gen_metadata_primary_model or "").strip() or normalized_sub2gen_metadata_model
                )
            if not normalized_sub2gen_metadata_primary_model.strip():
                normalized_sub2gen_metadata_primary_model = normalized_sub2gen_metadata_model
            if not normalized_sub2gen_metadata_enabled_models.strip():
                enabled = [normalized_sub2gen_metadata_primary_model]
                enabled.extend(
                    [m.strip() for m in normalized_sub2gen_metadata_fallback_models.split(",") if m.strip()]
                )
                normalized_sub2gen_metadata_enabled_models = ",".join(list(dict.fromkeys([m for m in enabled if m])))
            normalized_metadata_system_prompt = (
                str(metadata_system_prompt)
                if metadata_system_prompt is not None
                else str(current.get("metadata_system_prompt", "") or "")
            )
            normalized_cloning_image_system_prompt = (
                str(cloning_image_system_prompt)
                if cloning_image_system_prompt is not None
                else str(current.get("cloning_image_system_prompt", "") or "")
            )
            normalized_cloning_video_system_prompt = (
                str(cloning_video_system_prompt)
                if cloning_video_system_prompt is not None
                else str(current.get("cloning_video_system_prompt", "") or "")
            )
            normalized_task_tracker_device_id = (
                str(task_tracker_device_id)
                if task_tracker_device_id is not None
                else str(current.get("task_tracker_device_id", "") or "")
            )
            normalized_task_tracker_device_name = (
                str(task_tracker_device_name)
                if task_tracker_device_name is not None
                else str(current.get("task_tracker_device_name", "") or "")
            )
            normalized_task_tracker_cookies = (
                str(task_tracker_cookies)
                if task_tracker_cookies is not None
                else str(current.get("task_tracker_cookies", "") or "")
            )
            normalized_task_tracker_device_token = (
                str(task_tracker_device_token)
                if task_tracker_device_token is not None
                else str(current.get("task_tracker_device_token", "") or "")
            )
            normalized_task_tracker_turnstile_token = (
                str(task_tracker_turnstile_token)
                if task_tracker_turnstile_token is not None
                else str(current.get("task_tracker_turnstile_token", "") or "")
            )
            normalized_task_tracker_tls_profile = (
                str(task_tracker_tls_profile)
                if task_tracker_tls_profile is not None
                else str(current.get("task_tracker_tls_profile", "") or "")
            )

            if row:
                await db.execute(
                    """
                    UPDATE generation_config
                    SET image_timeout = ?,
                        video_timeout = ?,
                        max_retries = ?,
                        extension_generation_enabled = ?,
                        extension_generation_fallback_mode = ?,
                        sub2gen_gemini_api_keys = ?,
                        sub2gen_openai_api_keys = ?,
                        sub2gen_openrouter_api_keys = ?,
                        sub2gen_third_party_gemini_api_keys = ?,
                        sub2gen_third_party_gemini_base_url = ?,
                        cloudflare_account_id = ?,
                        cloudflare_api_token = ?,
                        sub2gen_csvgen_cookie = ?,
                        sub2gen_csvgen_api_keys = ?,
                        sub2gen_cloning_model = ?,
                        sub2gen_cloning_backend = ?,
                        sub2gen_cloning_provider_order = ?,
                        sub2gen_cloning_enabled_providers = ?,
                        sub2gen_cloning_provider_retry_count = ?,
                        sub2gen_cloning_gemini_api_keys = ?,
                        sub2gen_cloning_openai_api_keys = ?,
                        sub2gen_cloning_openrouter_api_keys = ?,
                        sub2gen_cloning_third_party_gemini_api_keys = ?,
                        sub2gen_cloning_third_party_gemini_base_url = ?,
                        sub2gen_cloning_cloudflare_account_id = ?,
                        sub2gen_cloning_cloudflare_api_token = ?,
                        sub2gen_metadata_backend = ?,
                        sub2gen_metadata_provider_order = ?,
                        sub2gen_metadata_enabled_providers = ?,
                        sub2gen_metadata_provider_retry_count = ?,
                        sub2gen_metadata_model = ?,
                        sub2gen_metadata_enabled_models = ?,
                        sub2gen_metadata_primary_model = ?,
                        sub2gen_metadata_fallback_models = ?,
                        metadata_system_prompt = ?,
                        cloning_image_system_prompt = ?,
                        cloning_video_system_prompt = ?,
                        task_tracker_device_id = ?,
                        task_tracker_device_name = ?,
                        task_tracker_cookies = ?,
                        task_tracker_device_token = ?,
                        task_tracker_turnstile_token = ?,
                        task_tracker_tls_profile = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (
                        normalized_image_timeout,
                        normalized_video_timeout,
                        normalized_max_retries,
                        normalized_extension_generation_enabled,
                        normalized_extension_generation_fallback_mode,
                        normalized_sub2gen_gemini_api_keys,
                        normalized_sub2gen_openai_api_keys,
                        normalized_sub2gen_openrouter_api_keys,
                        normalized_sub2gen_third_party_gemini_api_keys,
                        normalized_sub2gen_third_party_gemini_base_url,
                        normalized_cloudflare_account_id,
                        normalized_cloudflare_api_token,
                        normalized_sub2gen_csvgen_cookie,
                        normalized_sub2gen_csvgen_api_keys,
                        normalized_sub2gen_cloning_model,
                        normalized_sub2gen_cloning_backend,
                        normalized_sub2gen_cloning_provider_order,
                        normalized_sub2gen_cloning_enabled_providers,
                        normalized_sub2gen_cloning_provider_retry_count,
                        normalized_sub2gen_cloning_gemini_api_keys,
                        normalized_sub2gen_cloning_openai_api_keys,
                        normalized_sub2gen_cloning_openrouter_api_keys,
                        normalized_sub2gen_cloning_third_party_gemini_api_keys,
                        normalized_sub2gen_cloning_third_party_gemini_base_url,
                        normalized_sub2gen_cloning_cloudflare_account_id,
                        normalized_sub2gen_cloning_cloudflare_api_token,
                        normalized_sub2gen_metadata_backend,
                        normalized_sub2gen_metadata_provider_order,
                        normalized_sub2gen_metadata_enabled_providers,
                        normalized_sub2gen_metadata_provider_retry_count,
                        normalized_sub2gen_metadata_model,
                        normalized_sub2gen_metadata_enabled_models,
                        normalized_sub2gen_metadata_primary_model,
                        normalized_sub2gen_metadata_fallback_models,
                        normalized_metadata_system_prompt,
                        normalized_cloning_image_system_prompt,
                        normalized_cloning_video_system_prompt,
                        normalized_task_tracker_device_id,
                        normalized_task_tracker_device_name,
                        normalized_task_tracker_cookies,
                        normalized_task_tracker_device_token,
                        normalized_task_tracker_turnstile_token,
                        normalized_task_tracker_tls_profile,
                    ),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO generation_config (
                        id,
                        image_timeout,
                        video_timeout,
                        max_retries,
                        extension_generation_enabled,
                        extension_generation_fallback_mode,
                        sub2gen_gemini_api_keys,
                        sub2gen_openai_api_keys,
                        sub2gen_openrouter_api_keys,
                        sub2gen_third_party_gemini_api_keys,
                        sub2gen_third_party_gemini_base_url,
                        cloudflare_account_id,
                        cloudflare_api_token,
                        sub2gen_csvgen_cookie,
                        sub2gen_csvgen_api_keys,
                        sub2gen_cloning_model,
                        sub2gen_cloning_backend,
                        sub2gen_cloning_provider_order,
                        sub2gen_cloning_enabled_providers,
                        sub2gen_cloning_provider_retry_count,
                        sub2gen_cloning_gemini_api_keys,
                        sub2gen_cloning_openai_api_keys,
                        sub2gen_cloning_openrouter_api_keys,
                        sub2gen_cloning_third_party_gemini_api_keys,
                        sub2gen_cloning_third_party_gemini_base_url,
                        sub2gen_cloning_cloudflare_account_id,
                        sub2gen_cloning_cloudflare_api_token,
                        sub2gen_metadata_backend,
                        sub2gen_metadata_provider_order,
                        sub2gen_metadata_enabled_providers,
                        sub2gen_metadata_provider_retry_count,
                        sub2gen_metadata_model,
                        sub2gen_metadata_enabled_models,
                        sub2gen_metadata_primary_model,
                        sub2gen_metadata_fallback_models,
                        metadata_system_prompt,
                        cloning_image_system_prompt,
                        cloning_video_system_prompt,
                        task_tracker_device_id,
                        task_tracker_device_name,
                        task_tracker_cookies,
                        task_tracker_device_token,
                        task_tracker_turnstile_token,
                        task_tracker_tls_profile
                    )
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        normalized_image_timeout,
                        normalized_video_timeout,
                        normalized_max_retries,
                        normalized_extension_generation_enabled,
                        normalized_extension_generation_fallback_mode,
                        normalized_sub2gen_gemini_api_keys,
                        normalized_sub2gen_openai_api_keys,
                        normalized_sub2gen_openrouter_api_keys,
                        normalized_sub2gen_third_party_gemini_api_keys,
                        normalized_sub2gen_third_party_gemini_base_url,
                        normalized_cloudflare_account_id,
                        normalized_cloudflare_api_token,
                        normalized_sub2gen_csvgen_cookie,
                        normalized_sub2gen_csvgen_api_keys,
                        normalized_sub2gen_cloning_model,
                        normalized_sub2gen_cloning_backend,
                        normalized_sub2gen_cloning_provider_order,
                        normalized_sub2gen_cloning_enabled_providers,
                        normalized_sub2gen_cloning_provider_retry_count,
                        normalized_sub2gen_cloning_gemini_api_keys,
                        normalized_sub2gen_cloning_openai_api_keys,
                        normalized_sub2gen_cloning_openrouter_api_keys,
                        normalized_sub2gen_cloning_third_party_gemini_api_keys,
                        normalized_sub2gen_cloning_third_party_gemini_base_url,
                        normalized_sub2gen_cloning_cloudflare_account_id,
                        normalized_sub2gen_cloning_cloudflare_api_token,
                        normalized_sub2gen_metadata_backend,
                        normalized_sub2gen_metadata_provider_order,
                        normalized_sub2gen_metadata_enabled_providers,
                        normalized_sub2gen_metadata_provider_retry_count,
                        normalized_sub2gen_metadata_model,
                        normalized_sub2gen_metadata_enabled_models,
                        normalized_sub2gen_metadata_primary_model,
                        normalized_sub2gen_metadata_fallback_models,
                        normalized_metadata_system_prompt,
                        normalized_cloning_image_system_prompt,
                        normalized_cloning_video_system_prompt,
                        normalized_task_tracker_device_id,
                        normalized_task_tracker_device_name,
                        normalized_task_tracker_cookies,
                        normalized_task_tracker_device_token,
                        normalized_task_tracker_turnstile_token,
                        normalized_task_tracker_tls_profile,
                    ),
                )
            await db.commit()

    async def get_call_logic_config(self) -> CallLogicConfig:
        """Get token call logic configuration."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM call_logic_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                row_dict = dict(row)
                mode = row_dict.get("call_mode")
                if mode not in ("default", "polling"):
                    row_dict["call_mode"] = "polling" if row_dict.get("polling_mode_enabled") else "default"
                return CallLogicConfig(**row_dict)
            return CallLogicConfig(call_mode="default", polling_mode_enabled=False)

    async def update_call_logic_config(self, call_mode: str):
        """Update token call logic configuration."""
        normalized = "polling" if call_mode == "polling" else "default"
        polling_mode_enabled = normalized == "polling"
        async with self._connect(write=True) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO call_logic_config (id, call_mode, polling_mode_enabled, updated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
            """,
                (normalized, polling_mode_enabled),
            )
            await db.commit()

    # Request log operations
    async def add_request_log(self, log: RequestLog) -> int:
        """Add request log and return log id"""
        raw_request_body = log.request_body
        raw_response_body = log.response_body
        summary_fields = (
            self.log_payload_manager.summary_fields(raw_request_body, raw_response_body)
            if self.log_payload_manager is not None
            else {
                "request_body": raw_request_body,
                "response_body": raw_response_body,
                "request_excerpt": str(raw_request_body or "")[:1024],
                "response_excerpt": str(raw_response_body or "")[:1024],
                "request_size_bytes": len(str(raw_request_body or "").encode("utf-8", errors="replace")),
                "response_size_bytes": len(str(raw_response_body or "").encode("utf-8", errors="replace")),
            }
        )
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO request_logs (
                    token_id, api_key_id, operation, request_body, response_body,
                    status_code, duration, status_text, progress,
                    request_excerpt, response_excerpt, request_size_bytes, response_size_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    log.token_id,
                    log.api_key_id,
                    log.operation,
                    summary_fields["request_body"],
                    summary_fields["response_body"],
                    log.status_code,
                    log.duration,
                    log.status_text or "",
                    log.progress,
                    summary_fields["request_excerpt"],
                    summary_fields["response_excerpt"],
                    summary_fields["request_size_bytes"],
                    summary_fields["response_size_bytes"],
                ),
            )
            await db.commit()
            log_id = int(cursor.lastrowid)
        if self.log_payload_manager is not None:
            captured = self.log_payload_manager.capture_initial(
                log_id,
                request_body=raw_request_body,
                response_body=raw_response_body,
                status_code=log.status_code,
                status_text=log.status_text or "",
            )
            if captured.get("payload_storage_error"):
                await self.set_request_log_payload_metadata(
                    log_id,
                    payload_available=False,
                    payload_object_key=None,
                    payload_storage_error=str(captured["payload_storage_error"]),
                )
        if self.event_runtime is not None and self.event_runtime.ready:
            await self.event_runtime.publish(
                "request_summary_created",
                {
                    "id": log_id,
                    "token_id": log.token_id,
                    "api_key_id": log.api_key_id,
                    "operation": log.operation,
                    "status_code": log.status_code,
                    "duration": log.duration,
                    "status_text": log.status_text or "",
                    "progress": log.progress,
                },
                persist=False,
            )
        return log_id

    async def insert_managed_route_request_log(
        self,
        *,
        api_key_id: int,
        operation: str,
        request_body: str,
        response_body: str,
        status_code: int,
        duration: float,
        status_text: str = "",
    ) -> int:
        """Single-row log for managed-key routes without a Flow token (e.g. Adobe tools)."""
        log = RequestLog(
            token_id=None,
            api_key_id=api_key_id,
            operation=operation,
            request_body=request_body,
            response_body=response_body,
            status_code=status_code,
            duration=duration,
            status_text=status_text
            or ("completed" if status_code == 200 else "failed" if status_code >= 400 else "processing"),
            progress=100 if status_code == 200 else 0 if status_code >= 400 else 0,
        )
        return await self.add_request_log(log)

    async def get_adobe_usage_stats(
        self,
        api_key_id: int,
        months_back: int = 12,
    ) -> Dict[str, Any]:
        """Successful Adobe requests: all `adobe:%` operations from request_logs, by month and by endpoint."""
        safe_months = max(1, min(int(months_back or 12), 60))
        window = f"-{safe_months} months"
        params: List[Any] = [api_key_id, window]
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT strftime('%Y-%m', created_at) AS year_month, operation, COUNT(*) AS success_count
                FROM request_logs
                WHERE api_key_id = ?
                  AND status_code = 200
                  AND operation LIKE 'adobe:%'
                  AND datetime(created_at) >= datetime('now', ?)
                GROUP BY 1, 2
                ORDER BY 1 ASC, 2 ASC
                """,
                params,
            )
            detail_rows = await cursor.fetchall()

        by_op: List[Dict[str, Any]] = []
        totals_map: Dict[str, int] = {}
        for r in detail_rows:
            ym = str(r["year_month"])
            op = str(r["operation"])
            cnt = int(r["success_count"])
            by_op.append({"year_month": ym, "operation": op, "success_count": cnt})
            totals_map[ym] = totals_map.get(ym, 0) + cnt

        by_month = [{"year_month": ym, "success_count": totals_map[ym]} for ym in sorted(totals_map.keys())]
        return {"by_month": by_month, "by_month_by_operation": by_op}

    async def update_request_log(self, log_id: int, **kwargs):
        """Update an existing request log row."""
        if not kwargs:
            return

        allowed_fields = {
            "token_id",
            "api_key_id",
            "operation",
            "request_body",
            "response_body",
            "status_code",
            "duration",
            "status_text",
            "progress",
            "request_excerpt",
            "response_excerpt",
            "payload_available",
            "payload_object_key",
            "payload_storage_error",
            "request_size_bytes",
            "response_size_bytes",
        }
        update_fields = {key: value for key, value in kwargs.items() if key in allowed_fields}
        if not update_fields:
            return
        if self.log_payload_manager is not None:
            update_fields = self.log_payload_manager.capture_update(int(log_id), update_fields)

        status_text = str(update_fields.get("status_text") or "").strip().lower()
        status_code = int(update_fields.get("status_code") or 0)
        terminal = status_code >= 200 or status_text in {
            "completed",
            "failed",
            "cancelled",
            "canceled",
            "error",
            "timeout",
        }
        runtime = self.event_runtime
        if runtime is not None and runtime.ready:
            await runtime.publish_task_progress(
                "request_log",
                str(log_id),
                {key: value for key, value in update_fields.items() if key not in {"request_body", "response_body"}},
                terminal=terminal,
            )
            now = time.monotonic()
            progress_only = set(update_fields).issubset({"status_code", "duration", "status_text", "progress"})
            last_persisted = self._last_log_progress_persist.get(int(log_id), 0.0)
            if progress_only and not terminal and now - last_persisted < 2.0:
                return
            self._last_log_progress_persist[int(log_id)] = now
            if terminal:
                self._last_log_progress_persist.pop(int(log_id), None)

        clauses = []
        values = []
        for key, value in update_fields.items():
            clauses.append(f"{key} = ?")
            values.append(value)
        clauses.append("updated_at = CURRENT_TIMESTAMP")
        values.append(log_id)

        async with self._connect(write=True) as db:
            await db.execute(
                f"UPDATE request_logs SET {', '.join(clauses)} WHERE id = ?",
                values,
            )
            await db.commit()
        if runtime is not None and runtime.ready:
            await runtime.publish(
                "request_summary_updated",
                {
                    "id": int(log_id),
                    **{
                        key: value
                        for key, value in update_fields.items()
                        if key not in {"request_body", "response_body", "payload_object_key"}
                    },
                },
                persist=False,
            )

    async def count_request_logs(
        self,
        token_id: Optional[int] = None,
        api_key_id: Optional[int] = None,
        exclude_operations: Optional[List[str]] = None,
        search: Optional[str] = None,
    ) -> int:
        """Count rows matching the same filters as get_logs."""
        exc_ops = [str(x).strip() for x in (exclude_operations or []) if str(x).strip()]
        search_text = str(search or "").strip()
        exclude_sql = ""
        if exc_ops:
            ph = ",".join("?" * len(exc_ops))
            exclude_sql = f" AND rl.operation NOT IN ({ph})"
        async with self._connect() as db:
            has_status_text = await self._column_exists(db, "request_logs", "status_text")
            status_text_expr = "COALESCE(rl.status_text, '')" if has_status_text else "''"
            search_sql = ""
            search_params: List[Any] = []
            if search_text:
                search_sql = f"""
                    AND (
                        CAST(rl.id AS TEXT) LIKE ?
                        OR COALESCE(gt.job_id, '') LIKE ?
                        OR COALESCE(rl.operation, '') LIKE ?
                        OR {status_text_expr} LIKE ?
                        OR COALESCE(t.email, '') LIKE ?
                        OR COALESCE(t.name, '') LIKE ?
                        OR COALESCE(k.label, '') LIKE ?
                        OR COALESCE(k.key_prefix, '') LIKE ?
                    )
                """
                like = f"%{search_text}%"
                search_params = [like] * 8
            tail_params: List[Any] = [*exc_ops, *search_params]
            if token_id is not None:
                cursor = await db.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    LEFT JOIN api_keys k ON rl.api_key_id = k.id
                    LEFT JOIN geminigen_tasks gt ON gt.request_log_id = rl.id
                    WHERE rl.token_id = ?{exclude_sql}{search_sql}
                    """,
                    (token_id, *tail_params),
                )
            elif api_key_id is not None:
                cursor = await db.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    LEFT JOIN api_keys k ON rl.api_key_id = k.id
                    LEFT JOIN geminigen_tasks gt ON gt.request_log_id = rl.id
                    WHERE rl.api_key_id = ?{exclude_sql}{search_sql}
                    """,
                    (api_key_id, *tail_params),
                )
            else:
                cursor = await db.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    LEFT JOIN api_keys k ON rl.api_key_id = k.id
                    LEFT JOIN geminigen_tasks gt ON gt.request_log_id = rl.id
                    WHERE 1=1{exclude_sql}{search_sql}
                    """,
                    tuple(tail_params),
                )
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    async def get_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        token_id: Optional[int] = None,
        include_payload: bool = False,
        api_key_id: Optional[int] = None,
        exclude_operations: Optional[List[str]] = None,
        search: Optional[str] = None,
    ):
        """Get request logs with token info, optionally including payload fields"""
        safe_offset = max(0, int(offset or 0))
        exc_ops = [str(x).strip() for x in (exclude_operations or []) if str(x).strip()]
        search_text = str(search or "").strip()
        exclude_sql = ""
        exclude_params: List[Any] = []
        if exc_ops:
            ph = ",".join("?" * len(exc_ops))
            exclude_sql = f" AND rl.operation NOT IN ({ph})"
            exclude_params = list(exc_ops)

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            payload_columns = "rl.request_body, rl.response_body," if include_payload else ""
            has_summary = await self._column_exists(db, "request_logs", "response_excerpt")
            response_excerpt_column = (
                "COALESCE(rl.response_excerpt, '') as response_body_excerpt,"
                if has_summary
                else "substr(COALESCE(rl.response_body, ''), 1, 2048) as response_body_excerpt,"
            )
            payload_state_columns = (
                "COALESCE(rl.payload_available, 0) AS payload_available, rl.payload_storage_error,"
                if has_summary
                else "0 AS payload_available, NULL AS payload_storage_error,"
            )
            has_status_text = await self._column_exists(db, "request_logs", "status_text")
            has_progress = await self._column_exists(db, "request_logs", "progress")
            has_updated_at = await self._column_exists(db, "request_logs", "updated_at")
            status_text_column = "rl.status_text," if has_status_text else "'' as status_text,"
            progress_column = "rl.progress," if has_progress else "0 as progress,"
            updated_at_column = "rl.updated_at," if has_updated_at else "rl.created_at as updated_at,"
            key_cols = "k.label AS api_key_label, k.key_prefix AS api_key_prefix,"
            status_text_expr = "COALESCE(rl.status_text, '')" if has_status_text else "''"
            search_sql = ""
            search_params: List[Any] = []
            if search_text:
                search_sql = f"""
                    AND (
                        CAST(rl.id AS TEXT) LIKE ?
                        OR COALESCE(gt.job_id, '') LIKE ?
                        OR COALESCE(rl.operation, '') LIKE ?
                        OR {status_text_expr} LIKE ?
                        OR COALESCE(t.email, '') LIKE ?
                        OR COALESCE(t.name, '') LIKE ?
                        OR COALESCE(k.label, '') LIKE ?
                        OR COALESCE(k.key_prefix, '') LIKE ?
                    )
                """
                like = f"%{search_text}%"
                search_params = [like] * 8

            if token_id:
                cursor = await db.execute(
                    f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.api_key_id,
                        rl.operation,
                        {payload_columns}
                        {response_excerpt_column}
                        {payload_state_columns}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        {key_cols}
                        gt.job_id as job_id,
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    LEFT JOIN api_keys k ON rl.api_key_id = k.id
                    LEFT JOIN geminigen_tasks gt ON gt.request_log_id = rl.id
                    WHERE rl.token_id = ?{exclude_sql}{search_sql}
                    ORDER BY rl.created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (token_id, *exclude_params, *search_params, limit, safe_offset),
                )
            elif api_key_id is not None:
                cursor = await db.execute(
                    f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.api_key_id,
                        rl.operation,
                        {payload_columns}
                        {response_excerpt_column}
                        {payload_state_columns}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        {key_cols}
                        gt.job_id as job_id,
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    LEFT JOIN api_keys k ON rl.api_key_id = k.id
                    LEFT JOIN geminigen_tasks gt ON gt.request_log_id = rl.id
                    WHERE rl.api_key_id = ?{exclude_sql}{search_sql}
                    ORDER BY rl.created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (api_key_id, *exclude_params, *search_params, limit, safe_offset),
                )
            else:
                cursor = await db.execute(
                    f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.api_key_id,
                        rl.operation,
                        {payload_columns}
                        {response_excerpt_column}
                        {payload_state_columns}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        {key_cols}
                        gt.job_id as job_id,
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    LEFT JOIN api_keys k ON rl.api_key_id = k.id
                    LEFT JOIN geminigen_tasks gt ON gt.request_log_id = rl.id
                    WHERE 1=1{exclude_sql}{search_sql}
                    ORDER BY rl.created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (*exclude_params, *search_params, limit, safe_offset),
                )

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_log_detail(self, log_id: int, api_key_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Get single request log detail including payload fields"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            has_status_text = await self._column_exists(db, "request_logs", "status_text")
            has_progress = await self._column_exists(db, "request_logs", "progress")
            has_updated_at = await self._column_exists(db, "request_logs", "updated_at")
            has_summary = await self._column_exists(db, "request_logs", "payload_available")
            status_text_column = "rl.status_text," if has_status_text else "'' as status_text,"
            progress_column = "rl.progress," if has_progress else "0 as progress,"
            updated_at_column = "rl.updated_at," if has_updated_at else "rl.created_at as updated_at,"
            key_cols = "k.label AS api_key_label, k.key_prefix AS api_key_prefix,"
            payload_detail_columns = (
                "COALESCE(rl.payload_available, 0) AS payload_available, "
                "rl.payload_object_key, rl.payload_storage_error, "
                "COALESCE(rl.request_size_bytes, 0) AS request_size_bytes, "
                "COALESCE(rl.response_size_bytes, 0) AS response_size_bytes,"
                if has_summary
                else "0 AS payload_available, NULL AS payload_object_key, "
                "NULL AS payload_storage_error, 0 AS request_size_bytes, 0 AS response_size_bytes,"
            )
            if api_key_id is None:
                cursor = await db.execute(
                    f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.api_key_id,
                        rl.operation,
                        rl.request_body,
                        rl.response_body,
                        {payload_detail_columns}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        {key_cols}
                        gt.job_id as job_id,
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    LEFT JOIN api_keys k ON rl.api_key_id = k.id
                    LEFT JOIN geminigen_tasks gt ON gt.request_log_id = rl.id
                    WHERE rl.id = ?
                    LIMIT 1
                    """,
                    (log_id,),
                )
            else:
                cursor = await db.execute(
                    f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.api_key_id,
                        rl.operation,
                        rl.request_body,
                        rl.response_body,
                        {payload_detail_columns}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        {key_cols}
                        gt.job_id as job_id,
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    LEFT JOIN api_keys k ON rl.api_key_id = k.id
                    LEFT JOIN geminigen_tasks gt ON gt.request_log_id = rl.id
                    WHERE rl.id = ? AND rl.api_key_id = ?
                    LIMIT 1
                    """,
                    (log_id, api_key_id),
                )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def clear_all_logs(self):
        """Clear all request logs"""
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM request_logs")
            await db.commit()

    async def delete_request_logs_older_than(self, days: int = 7, batch_size: int = 500) -> int:
        """Delete request log rows older than the configured number of days."""
        safe_days = max(1, int(days or 7))
        safe_batch = max(1, min(int(batch_size or 500), 5000))
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                DELETE FROM request_logs
                WHERE id IN (
                    SELECT id FROM request_logs
                    WHERE datetime(created_at) < datetime('now', ?)
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
                (f"-{safe_days} days", safe_batch),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def cleanup_retention_batch(self, days: int = 7, batch_size: int = 500) -> Dict[str, int]:
        """Apply bounded seven-day history cleanup in dependency-safe order."""
        from ..services.failed_payload_store import summarize_payload

        safe_days = max(1, int(days or 7))
        safe_batch = max(1, min(int(batch_size or 500), 5000))
        cutoff = f"-{safe_days} days"
        counts = {
            "request_logs": 0,
            "api_key_audit_logs": 0,
            "geminigen_tasks": 0,
            "redis_persisted_events": 0,
            "payload_bodies_compacted": 0,
        }
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                SELECT id, request_body, response_body
                FROM request_logs
                WHERE datetime(created_at) >= datetime('now', ?)
                  AND (
                    length(COALESCE(request_body, '')) > 1024
                    OR length(COALESCE(response_body, '')) > 1024
                    OR (
                        COALESCE(request_body, '') != ''
                        AND COALESCE(request_excerpt, '') != COALESCE(request_body, '')
                    )
                    OR (
                        COALESCE(response_body, '') != ''
                        AND COALESCE(response_excerpt, '') != COALESCE(response_body, '')
                    )
                  )
                ORDER BY id ASC
                LIMIT ?
                """,
                (cutoff, safe_batch),
            )
            payload_rows = await cursor.fetchall()
            payload_updates = []
            for row in payload_rows:
                request_body = str(row[1] or "")
                response_body = str(row[2] or "")
                request_summary = summarize_payload(request_body)
                response_summary = summarize_payload(response_body)
                payload_updates.append(
                    (
                        len(request_body.encode("utf-8", errors="replace")),
                        len(response_body.encode("utf-8", errors="replace")),
                        request_summary,
                        response_summary,
                        request_summary,
                        response_summary,
                        int(row[0]),
                    )
                )
            if payload_updates:
                await db.executemany(
                    """
                    UPDATE request_logs
                    SET request_size_bytes = MAX(COALESCE(request_size_bytes, 0), ?),
                        response_size_bytes = MAX(COALESCE(response_size_bytes, 0), ?),
                        request_excerpt = ?, response_excerpt = ?,
                        request_body = ?, response_body = ?
                    WHERE id = ?
                    """,
                    payload_updates,
                )
            counts["payload_bodies_compacted"] = len(payload_updates)

            cursor = await db.execute(
                """
                DELETE FROM geminigen_tasks
                WHERE id IN (
                    SELECT id FROM geminigen_tasks
                    WHERE status NOT IN ('queued', 'processing')
                      AND datetime(COALESCE(completed_at, updated_at, created_at)) < datetime('now', ?)
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
                (cutoff, safe_batch),
            )
            counts["geminigen_tasks"] = int(cursor.rowcount or 0)

            cursor = await db.execute(
                """
                DELETE FROM api_key_audit_logs
                WHERE id IN (
                    SELECT id FROM api_key_audit_logs
                    WHERE datetime(created_at) < datetime('now', ?)
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
                (cutoff, safe_batch),
            )
            counts["api_key_audit_logs"] = int(cursor.rowcount or 0)

            cursor = await db.execute(
                """
                DELETE FROM request_logs
                WHERE id IN (
                    SELECT id FROM request_logs
                    WHERE datetime(created_at) < datetime('now', ?)
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
                (cutoff, safe_batch),
            )
            counts["request_logs"] = int(cursor.rowcount or 0)

            cursor = await db.execute(
                """
                DELETE FROM redis_persisted_events
                WHERE cursor IN (
                    SELECT cursor FROM redis_persisted_events
                    WHERE datetime(persisted_at) < datetime('now', ?)
                    ORDER BY persisted_at ASC
                    LIMIT ?
                )
                """,
                (cutoff, safe_batch),
            )
            counts["redis_persisted_events"] = int(cursor.rowcount or 0)
            await db.commit()
        return counts

    async def optimize_after_retention(self) -> None:
        """Run bounded SQLite maintenance after retention cleanup."""
        async with self._connect(write=True) as db:
            await db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            await db.execute("PRAGMA optimize")
            await db.execute("PRAGMA incremental_vacuum(2000)")
            await db.commit()

    async def set_request_log_payload_metadata(
        self,
        log_id: int,
        *,
        payload_available: bool,
        payload_object_key: Optional[str],
        payload_storage_error: Optional[str],
    ) -> None:
        async with self._connect(write=True) as db:
            await db.execute(
                """
                UPDATE request_logs
                SET payload_available = ?, payload_object_key = ?,
                    payload_storage_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    1 if payload_available else 0,
                    payload_object_key,
                    payload_storage_error,
                    int(log_id),
                ),
            )
            await db.commit()
        if self.event_runtime is not None and self.event_runtime.ready:
            await self.event_runtime.publish(
                "request_summary_updated",
                {
                    "id": int(log_id),
                    "payload_available": bool(payload_available),
                    "payload_storage_error": payload_storage_error,
                },
                persist=False,
            )

    async def upsert_cache_file(
        self,
        *,
        filename: str,
        api_key_id: int,
        token_id: Optional[int],
        media_type: str,
        source_url: Optional[str] = None,
        flow_project_id: Optional[str] = None,
        storage_provider: str = "local",
        object_key: Optional[str] = None,
        delivery_mode: str = "proxy",
        size_bytes: Optional[int] = None,
    ):
        """Upsert cache file ownership metadata."""
        async with self._connect(write=True) as db:
            await db.execute(
                """
                INSERT INTO cache_files (
                    filename, api_key_id, token_id, flow_project_id, media_type, source_url,
                    storage_provider, object_key, delivery_mode, size_bytes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(filename) DO UPDATE SET
                    api_key_id = excluded.api_key_id,
                    token_id = excluded.token_id,
                    flow_project_id = excluded.flow_project_id,
                    media_type = excluded.media_type,
                    source_url = excluded.source_url,
                    storage_provider = excluded.storage_provider,
                    object_key = excluded.object_key,
                    delivery_mode = excluded.delivery_mode,
                    size_bytes = excluded.size_bytes,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    filename,
                    api_key_id,
                    token_id,
                    flow_project_id,
                    media_type,
                    source_url,
                    storage_provider,
                    object_key or filename,
                    delivery_mode,
                    size_bytes,
                ),
            )
            await db.commit()

    async def delete_all_cache_file_metadata(self) -> int:
        async with self._connect(write=True) as db:
            cursor = await db.execute("DELETE FROM cache_files")
            await db.commit()
            return int(cursor.rowcount or 0)

    async def get_cache_file(self, filename: str) -> Optional[Dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM cache_files WHERE filename = ? LIMIT 1",
                (filename,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_cache_file_for_api_key(self, filename: str, api_key_id: int) -> Optional[Dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM cache_files WHERE filename = ? AND api_key_id = ? LIMIT 1",
                (filename, api_key_id),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_cache_files_for_api_key(
        self,
        api_key_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List cache_files rows for a managed API key (newest first)."""
        lim = max(1, min(int(limit), 500))
        off = max(0, int(offset))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM cache_files
                WHERE api_key_id = ?
                ORDER BY datetime(updated_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (api_key_id, lim, off),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def count_cache_files_for_api_key(self, api_key_id: int) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM cache_files WHERE api_key_id = ?",
                (api_key_id,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def list_cache_files_for_api_key_cleanup(self, api_key_id: int) -> List[Dict[str, Any]]:
        """Return every cache object owned by a key before that key is deleted."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM cache_files
                WHERE api_key_id = ?
                ORDER BY id ASC
                """,
                (int(api_key_id),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def list_cache_files_for_api_key_project(
        self,
        api_key_id: int,
        flow_project_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List cache_files rows for a managed API key scoped to one Flow project UUID."""
        lim = max(1, min(int(limit), 500))
        off = max(0, int(offset))
        pid = (flow_project_id or "").strip()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM cache_files
                WHERE api_key_id = ? AND flow_project_id = ?
                ORDER BY datetime(updated_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (api_key_id, pid, lim, off),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def count_cache_files_for_api_key_project(self, api_key_id: int, flow_project_id: str) -> int:
        pid = (flow_project_id or "").strip()
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM cache_files
                WHERE api_key_id = ? AND flow_project_id = ?
                """,
                (api_key_id, pid),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def init_config_from_toml(self, config_dict: dict, is_first_startup: bool = True):
        """
        Initialize database configuration from setting.toml

        Args:
            config_dict: Configuration dictionary from setting.toml
            is_first_startup: If True, initialize all config rows from setting.toml.
                            If False (upgrade mode), only ensure missing config rows exist with default values.
        """
        async with self._connect(write=True) as db:
            if is_first_startup:
                # First startup: Initialize all config tables with values from setting.toml
                await self._ensure_config_rows(db, config_dict)
            else:
                # Upgrade mode: Only ensure missing config rows exist (with default values, not from TOML)
                await self._ensure_config_rows(db, config_dict=None)

            await db.commit()

    async def reload_config_to_memory(self):
        """
        Reload all configuration from database to in-memory Config instance.
        This should be called after any configuration update to ensure hot-reload.

        Includes:
        - Admin config (username, password, api_key)
        - Cache config (enabled, timeout, base_url)
        - Generation config (image_timeout, video_timeout)
        - Proxy config will be handled by ProxyManager
        """
        from .config import config

        # Reload admin config
        admin_config = await self.get_admin_config()
        if admin_config:
            config.set_admin_username_from_db(admin_config.username)
            config.set_admin_password_from_db(admin_config.password)
            config.api_key = admin_config.api_key

        # Reload cache config
        cache_config = await self.get_cache_config()
        if cache_config:
            config.set_cache_enabled(cache_config.cache_enabled)
            config.set_cache_timeout(cache_config.cache_timeout)
            config.set_cache_base_url(cache_config.cache_base_url or "")
            config.set_cache_provider(cache_config.cache_provider)
            config.set_cache_delivery_mode(cache_config.cache_delivery_mode)

        # Reload generation config
        generation_config = await self.get_generation_config()
        if generation_config:
            config.set_image_timeout(generation_config.image_timeout)
            config.set_video_timeout(generation_config.video_timeout)
            config.set_flow_max_retries(generation_config.max_retries)
            config.set_extension_generation_enabled(
                bool(getattr(generation_config, "extension_generation_enabled", False))
            )
            config.set_extension_generation_fallback_mode(
                str(getattr(generation_config, "extension_generation_fallback_mode", "local_http_on_recaptcha"))
            )
            config.set_sub2gen_gemini_api_keys(str(getattr(generation_config, "sub2gen_gemini_api_keys", "") or ""))
            config.set_sub2gen_openai_api_keys(str(getattr(generation_config, "sub2gen_openai_api_keys", "") or ""))
            config.set_sub2gen_openrouter_api_keys(
                str(getattr(generation_config, "sub2gen_openrouter_api_keys", "") or "")
            )
            config.set_sub2gen_third_party_gemini_api_keys(
                str(getattr(generation_config, "sub2gen_third_party_gemini_api_keys", "") or "")
            )
            config.set_sub2gen_third_party_gemini_base_url(
                str(getattr(generation_config, "sub2gen_third_party_gemini_base_url", "") or "")
            )
            config.set_cloudflare_account_id(str(getattr(generation_config, "cloudflare_account_id", "") or ""))
            config.set_cloudflare_api_token(str(getattr(generation_config, "cloudflare_api_token", "") or ""))
            config.set_sub2gen_csvgen_cookie(str(getattr(generation_config, "sub2gen_csvgen_cookie", "") or ""))
            config.set_sub2gen_csvgen_api_keys(str(getattr(generation_config, "sub2gen_csvgen_api_keys", "") or ""))
            config.set_sub2gen_cloning_model(
                str(getattr(generation_config, "sub2gen_cloning_model", "gemini-2.5-flash") or "gemini-2.5-flash")
            )
            config.set_sub2gen_cloning_backend(
                str(getattr(generation_config, "sub2gen_cloning_backend", "gemini_native") or "gemini_native")
            )
            config.set_sub2gen_cloning_provider_order(
                str(getattr(generation_config, "sub2gen_cloning_provider_order", "") or "")
            )
            config.set_sub2gen_cloning_enabled_providers(
                str(getattr(generation_config, "sub2gen_cloning_enabled_providers", "") or "")
            )
            config.set_sub2gen_cloning_provider_retry_count(
                int(getattr(generation_config, "sub2gen_cloning_provider_retry_count", 1) or 1)
            )
            config.set_sub2gen_cloning_gemini_api_keys(
                str(getattr(generation_config, "sub2gen_cloning_gemini_api_keys", "") or "")
            )
            config.set_sub2gen_cloning_openai_api_keys(
                str(getattr(generation_config, "sub2gen_cloning_openai_api_keys", "") or "")
            )
            config.set_sub2gen_cloning_openrouter_api_keys(
                str(getattr(generation_config, "sub2gen_cloning_openrouter_api_keys", "") or "")
            )
            config.set_sub2gen_cloning_third_party_gemini_api_keys(
                str(getattr(generation_config, "sub2gen_cloning_third_party_gemini_api_keys", "") or "")
            )
            config.set_sub2gen_cloning_third_party_gemini_base_url(
                str(getattr(generation_config, "sub2gen_cloning_third_party_gemini_base_url", "") or "")
            )
            config.set_sub2gen_cloning_cloudflare_account_id(
                str(getattr(generation_config, "sub2gen_cloning_cloudflare_account_id", "") or "")
            )
            config.set_sub2gen_cloning_cloudflare_api_token(
                str(getattr(generation_config, "sub2gen_cloning_cloudflare_api_token", "") or "")
            )
            config.set_sub2gen_metadata_backend(
                str(getattr(generation_config, "sub2gen_metadata_backend", "gemini_native") or "gemini_native")
            )
            config.set_sub2gen_metadata_provider_order(
                str(getattr(generation_config, "sub2gen_metadata_provider_order", "") or "")
            )
            config.set_sub2gen_metadata_enabled_providers(
                str(getattr(generation_config, "sub2gen_metadata_enabled_providers", "") or "")
            )
            config.set_sub2gen_metadata_provider_retry_count(
                int(getattr(generation_config, "sub2gen_metadata_provider_retry_count", 1) or 1)
            )
            config.set_sub2gen_metadata_model(
                str(getattr(generation_config, "sub2gen_metadata_model", "gemini-2.5-flash") or "gemini-2.5-flash")
            )
            config.set_sub2gen_metadata_enabled_models(
                str(getattr(generation_config, "sub2gen_metadata_enabled_models", "") or "")
            )
            config.set_sub2gen_metadata_primary_model(
                str(getattr(generation_config, "sub2gen_metadata_primary_model", "") or "")
            )
            config.set_sub2gen_metadata_fallback_models(
                str(getattr(generation_config, "sub2gen_metadata_fallback_models", "") or "")
            )
            config.set_metadata_system_prompt(str(getattr(generation_config, "metadata_system_prompt", "") or ""))
            config.set_cloning_image_system_prompt(
                str(getattr(generation_config, "cloning_image_system_prompt", "") or "")
            )
            config.set_cloning_video_system_prompt(
                str(getattr(generation_config, "cloning_video_system_prompt", "") or "")
            )
            config.set_task_tracker_device_id(str(getattr(generation_config, "task_tracker_device_id", "") or ""))
            config.set_task_tracker_device_name(str(getattr(generation_config, "task_tracker_device_name", "") or ""))
            config.set_task_tracker_cookies(str(getattr(generation_config, "task_tracker_cookies", "") or ""))
            config.set_task_tracker_device_token(str(getattr(generation_config, "task_tracker_device_token", "") or ""))
            config.set_task_tracker_turnstile_token(
                str(getattr(generation_config, "task_tracker_turnstile_token", "") or "")
            )
            config.set_task_tracker_tls_profile(str(getattr(generation_config, "task_tracker_tls_profile", "") or ""))

        # Reload call logic config
        call_logic_config = await self.get_call_logic_config()
        if call_logic_config:
            config.set_call_logic_mode(call_logic_config.call_mode)

        # Reload debug config
        debug_config = await self.get_debug_config()
        if debug_config:
            config.set_debug_enabled(debug_config.enabled)

        # Reload captcha config
        captcha_config = await self.get_captcha_config()
        if captcha_config:
            config.set_captcha_method(captcha_config.captcha_method)
            config.set_yescaptcha_api_key(captcha_config.yescaptcha_api_key)
            config.set_yescaptcha_base_url(captcha_config.yescaptcha_base_url)
            config.set_yescaptcha_task_type(captcha_config.yescaptcha_task_type)
            config.set_capmonster_api_key(captcha_config.capmonster_api_key)
            config.set_capmonster_base_url(captcha_config.capmonster_base_url)
            config.set_capmonster_min_score(captcha_config.capmonster_min_score)
            config.set_ezcaptcha_api_key(captcha_config.ezcaptcha_api_key)
            config.set_ezcaptcha_base_url(captcha_config.ezcaptcha_base_url)
            config.set_capsolver_api_key(captcha_config.capsolver_api_key)
            config.set_capsolver_base_url(captcha_config.capsolver_base_url)
            config.set_remote_browser_base_url(captcha_config.remote_browser_base_url)
            config.set_remote_browser_api_key(captcha_config.remote_browser_api_key)
            config.set_remote_browser_timeout(captcha_config.remote_browser_timeout)
            config.set_browser_fallback_to_remote_browser(
                bool(getattr(captcha_config, "browser_fallback_to_remote_browser", True))
            )
            config.set_browser_count(int(getattr(captcha_config, "browser_count", 1) or 1))
            config.set_browser_personal_fresh_restart_every_n_solves(
                int(getattr(captcha_config, "browser_personal_fresh_restart_every_n_solves", 10) or 10)
            )
            config.set_personal_project_pool_size(captcha_config.personal_project_pool_size)
            config.set_personal_max_resident_tabs(captcha_config.personal_max_resident_tabs)
            config.set_personal_idle_tab_ttl_seconds(captcha_config.personal_idle_tab_ttl_seconds)
            config.set_browser_captcha_page_url(getattr(captcha_config, "browser_captcha_page_url", None) or "")
            config.set_session_refresh_enabled(bool(getattr(captcha_config, "session_refresh_enabled", True)))
            config.set_session_refresh_browser_first(
                bool(getattr(captcha_config, "session_refresh_browser_first", True))
            )
            config.set_session_refresh_inject_st_cookie(
                bool(getattr(captcha_config, "session_refresh_inject_st_cookie", True))
            )
            config.set_session_refresh_warmup_urls(getattr(captcha_config, "session_refresh_warmup_urls", ""))
            config.set_session_refresh_wait_seconds_per_url(
                int(getattr(captcha_config, "session_refresh_wait_seconds_per_url", 60) or 60)
            )
            config.set_session_refresh_overall_timeout_seconds(
                int(getattr(captcha_config, "session_refresh_overall_timeout_seconds", 180) or 180)
            )
            config.set_session_refresh_update_st_from_cookie(
                bool(getattr(captcha_config, "session_refresh_update_st_from_cookie", True))
            )
            config.set_session_refresh_fail_if_st_refresh_fails(
                bool(getattr(captcha_config, "session_refresh_fail_if_st_refresh_fails", True))
            )
            config.set_session_refresh_local_only(bool(getattr(captcha_config, "session_refresh_local_only", True)))
            config.set_session_refresh_scheduler_enabled(
                bool(getattr(captcha_config, "session_refresh_scheduler_enabled", False))
            )
            config.set_session_refresh_scheduler_interval_minutes(
                int(getattr(captcha_config, "session_refresh_scheduler_interval_minutes", 30) or 30)
            )
            config.set_session_refresh_scheduler_batch_size(
                int(getattr(captcha_config, "session_refresh_scheduler_batch_size", 10) or 10)
            )
            config.set_session_refresh_scheduler_only_expiring_within_minutes(
                int(getattr(captcha_config, "session_refresh_scheduler_only_expiring_within_minutes", 60) or 60)
            )
            config.set_st_only_refresh_scheduler_enabled(
                bool(getattr(captcha_config, "st_only_refresh_scheduler_enabled", False))
            )
            config.set_st_only_refresh_scheduler_interval_minutes(
                int(getattr(captcha_config, "st_only_refresh_scheduler_interval_minutes", 5) or 5)
            )
            config.set_st_only_refresh_scheduler_batch_size(
                int(getattr(captcha_config, "st_only_refresh_scheduler_batch_size", 20) or 20)
            )
            config.set_st_only_refresh_scheduler_expiring_within_minutes(
                int(getattr(captcha_config, "st_only_refresh_scheduler_expiring_within_minutes", 5) or 5)
            )
            config.set_dedicated_extension_enabled(bool(getattr(captcha_config, "dedicated_extension_enabled", False)))
            config.set_dedicated_extension_captcha_timeout_seconds(
                int(getattr(captcha_config, "dedicated_extension_captcha_timeout_seconds", 25) or 25)
            )
            config.set_dedicated_extension_st_refresh_timeout_seconds(
                int(getattr(captcha_config, "dedicated_extension_st_refresh_timeout_seconds", 45) or 45)
            )
            config.set_extension_fallback_to_managed_on_dedicated_failure(
                bool(getattr(captcha_config, "extension_fallback_to_managed_on_dedicated_failure", False))
            )

    # Cache config operations
    async def get_cache_config(self) -> CacheConfig:
        """Get cache configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CacheConfig(**dict(row))
            # Return default if not found
            return CacheConfig(cache_enabled=False, cache_timeout=7200)

    async def update_cache_config(
        self,
        enabled: bool = None,
        timeout: int = None,
        base_url: Optional[str] = None,
        provider: Optional[str] = None,
        delivery_mode: Optional[str] = None,
    ):
        """Update cache configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("cache_enabled", False)
                new_timeout = timeout if timeout is not None else current.get("cache_timeout", 7200)
                new_base_url = base_url if base_url is not None else current.get("cache_base_url")
                new_provider = provider if provider is not None else current.get("cache_provider", "local")
                new_delivery_mode = (
                    delivery_mode if delivery_mode is not None else current.get("cache_delivery_mode", "proxy")
                )

                # If base_url is explicitly set to empty string, treat as None
                if base_url == "":
                    new_base_url = None

                await db.execute(
                    """
                    UPDATE cache_config
                    SET cache_enabled = ?, cache_timeout = ?, cache_base_url = ?,
                        cache_provider = ?, cache_delivery_mode = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (new_enabled, new_timeout, new_base_url, new_provider, new_delivery_mode),
                )
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_timeout = timeout if timeout is not None else 7200
                new_base_url = base_url if base_url is not None else None
                new_provider = provider if provider is not None else "local"
                new_delivery_mode = delivery_mode if delivery_mode is not None else "proxy"

                await db.execute(
                    """
                    INSERT INTO cache_config (
                        id, cache_enabled, cache_timeout, cache_base_url,
                        cache_provider, cache_delivery_mode
                    ) VALUES (1, ?, ?, ?, ?, ?)
                """,
                    (new_enabled, new_timeout, new_base_url, new_provider, new_delivery_mode),
                )

            await db.commit()

    # Debug config operations
    async def get_debug_config(self) -> "DebugConfig":
        """Get debug configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return DebugConfig(**dict(row))
            # Return default if not found
            return DebugConfig(enabled=False, log_requests=True, log_responses=True, mask_token=True)

    async def update_debug_config(
        self, enabled: bool = None, log_requests: bool = None, log_responses: bool = None, mask_token: bool = None
    ):
        """Update debug configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("enabled", False)
                new_log_requests = log_requests if log_requests is not None else current.get("log_requests", True)
                new_log_responses = log_responses if log_responses is not None else current.get("log_responses", True)
                new_mask_token = mask_token if mask_token is not None else current.get("mask_token", True)

                await db.execute(
                    """
                    UPDATE debug_config
                    SET enabled = ?, log_requests = ?, log_responses = ?, mask_token = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (new_enabled, new_log_requests, new_log_responses, new_mask_token),
                )
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_log_requests = log_requests if log_requests is not None else True
                new_log_responses = log_responses if log_responses is not None else True
                new_mask_token = mask_token if mask_token is not None else True

                await db.execute(
                    """
                    INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                    VALUES (1, ?, ?, ?, ?)
                """,
                    (new_enabled, new_log_requests, new_log_responses, new_mask_token),
                )

            await db.commit()

    # Captcha config operations
    async def get_captcha_config(self) -> CaptchaConfig:
        """Get captcha configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CaptchaConfig(**dict(row))
            return CaptchaConfig()

    async def update_captcha_config(
        self,
        captcha_method: str = None,
        yescaptcha_api_key: str = None,
        yescaptcha_base_url: str = None,
        yescaptcha_task_type: str = None,
        capmonster_api_key: str = None,
        capmonster_base_url: str = None,
        capmonster_min_score: float = None,
        ezcaptcha_api_key: str = None,
        ezcaptcha_base_url: str = None,
        capsolver_api_key: str = None,
        capsolver_base_url: str = None,
        remote_browser_base_url: str = None,
        remote_browser_api_key: str = None,
        remote_browser_timeout: int = None,
        browser_fallback_to_remote_browser: bool = None,
        browser_proxy_enabled: bool = None,
        browser_proxy_url: str = None,
        browser_count: int = None,
        browser_personal_fresh_restart_every_n_solves: int = None,
        personal_project_pool_size: int = None,
        personal_max_resident_tabs: int = None,
        personal_idle_tab_ttl_seconds: int = None,
        browser_captcha_page_url: str = None,
        session_refresh_enabled: bool = None,
        session_refresh_browser_first: bool = None,
        session_refresh_inject_st_cookie: bool = None,
        session_refresh_warmup_urls: str = None,
        session_refresh_wait_seconds_per_url: int = None,
        session_refresh_overall_timeout_seconds: int = None,
        session_refresh_update_st_from_cookie: bool = None,
        session_refresh_fail_if_st_refresh_fails: bool = None,
        session_refresh_local_only: bool = None,
        session_refresh_scheduler_enabled: bool = None,
        session_refresh_scheduler_interval_minutes: int = None,
        session_refresh_scheduler_batch_size: int = None,
        session_refresh_scheduler_only_expiring_within_minutes: int = None,
        st_only_refresh_scheduler_enabled: bool = None,
        st_only_refresh_scheduler_interval_minutes: int = None,
        st_only_refresh_scheduler_batch_size: int = None,
        st_only_refresh_scheduler_expiring_within_minutes: int = None,
        extension_queue_wait_timeout_seconds: int = None,
        dedicated_extension_enabled: bool = None,
        dedicated_extension_captcha_timeout_seconds: int = None,
        dedicated_extension_st_refresh_timeout_seconds: int = None,
        extension_fallback_to_managed_on_dedicated_failure: bool = None,
    ):
        """Update captcha configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                new_method = (
                    captcha_method if captcha_method is not None else current.get("captcha_method", "yescaptcha")
                )
                new_yes_key = (
                    yescaptcha_api_key if yescaptcha_api_key is not None else current.get("yescaptcha_api_key", "")
                )
                new_yes_url = (
                    yescaptcha_base_url
                    if yescaptcha_base_url is not None
                    else current.get("yescaptcha_base_url", "https://api.yescaptcha.com")
                )
                new_yes_task_type = normalize_yescaptcha_task_type(
                    yescaptcha_task_type if yescaptcha_task_type is not None else current.get("yescaptcha_task_type")
                )
                new_cap_key = (
                    capmonster_api_key if capmonster_api_key is not None else current.get("capmonster_api_key", "")
                )
                new_cap_url = (
                    capmonster_base_url
                    if capmonster_base_url is not None
                    else current.get("capmonster_base_url", "https://api.capmonster.cloud")
                )
                new_cap_min_score = normalize_capmonster_min_score(
                    capmonster_min_score
                    if capmonster_min_score is not None
                    else current.get("capmonster_min_score", DEFAULT_CAPMONSTER_MIN_SCORE)
                )
                new_ez_key = (
                    ezcaptcha_api_key if ezcaptcha_api_key is not None else current.get("ezcaptcha_api_key", "")
                )
                new_ez_url = (
                    ezcaptcha_base_url
                    if ezcaptcha_base_url is not None
                    else current.get("ezcaptcha_base_url", "https://api.ez-captcha.com")
                )
                new_cs_key = (
                    capsolver_api_key if capsolver_api_key is not None else current.get("capsolver_api_key", "")
                )
                new_cs_url = (
                    capsolver_base_url
                    if capsolver_base_url is not None
                    else current.get("capsolver_base_url", "https://api.capsolver.com")
                )
                new_remote_base_url = (
                    remote_browser_base_url
                    if remote_browser_base_url is not None
                    else current.get("remote_browser_base_url", "")
                )
                new_remote_api_key = (
                    remote_browser_api_key
                    if remote_browser_api_key is not None
                    else current.get("remote_browser_api_key", "")
                )
                new_remote_timeout = (
                    remote_browser_timeout
                    if remote_browser_timeout is not None
                    else current.get("remote_browser_timeout", 60)
                )
                new_browser_fallback = (
                    browser_fallback_to_remote_browser
                    if browser_fallback_to_remote_browser is not None
                    else current.get("browser_fallback_to_remote_browser", True)
                )
                new_proxy_enabled = (
                    browser_proxy_enabled
                    if browser_proxy_enabled is not None
                    else current.get("browser_proxy_enabled", False)
                )
                new_proxy_url = browser_proxy_url if browser_proxy_url is not None else current.get("browser_proxy_url")
                new_browser_count = browser_count if browser_count is not None else current.get("browser_count", 1)
                new_browser_personal_fresh_restart_every_n_solves = (
                    browser_personal_fresh_restart_every_n_solves
                    if browser_personal_fresh_restart_every_n_solves is not None
                    else current.get("browser_personal_fresh_restart_every_n_solves", 10)
                )
                new_personal_project_pool_size = (
                    personal_project_pool_size
                    if personal_project_pool_size is not None
                    else current.get("personal_project_pool_size", 4)
                )
                new_personal_max_tabs = (
                    personal_max_resident_tabs
                    if personal_max_resident_tabs is not None
                    else current.get("personal_max_resident_tabs", 5)
                )
                new_personal_idle_ttl = (
                    personal_idle_tab_ttl_seconds
                    if personal_idle_tab_ttl_seconds is not None
                    else current.get("personal_idle_tab_ttl_seconds", 600)
                )
                default_page_url = "https://labs.google/fx/api/auth/providers"
                new_browser_captcha_page_url = (
                    browser_captcha_page_url
                    if browser_captcha_page_url is not None
                    else current.get("browser_captcha_page_url", default_page_url)
                )
                new_browser_captcha_page_url = (
                    new_browser_captcha_page_url or default_page_url
                ).strip() or default_page_url
                new_session_refresh_enabled = (
                    session_refresh_enabled
                    if session_refresh_enabled is not None
                    else current.get("session_refresh_enabled", True)
                )
                new_session_refresh_browser_first = (
                    session_refresh_browser_first
                    if session_refresh_browser_first is not None
                    else current.get("session_refresh_browser_first", True)
                )
                new_session_refresh_inject_st_cookie = (
                    session_refresh_inject_st_cookie
                    if session_refresh_inject_st_cookie is not None
                    else current.get("session_refresh_inject_st_cookie", True)
                )
                new_session_refresh_warmup_urls = (
                    session_refresh_warmup_urls
                    if session_refresh_warmup_urls is not None
                    else current.get(
                        "session_refresh_warmup_urls",
                        "https://labs.google/fx/tools/flow,https://labs.google/fx",
                    )
                )
                new_session_refresh_wait_seconds_per_url = (
                    session_refresh_wait_seconds_per_url
                    if session_refresh_wait_seconds_per_url is not None
                    else current.get("session_refresh_wait_seconds_per_url", 60)
                )
                new_session_refresh_overall_timeout_seconds = (
                    session_refresh_overall_timeout_seconds
                    if session_refresh_overall_timeout_seconds is not None
                    else current.get("session_refresh_overall_timeout_seconds", 180)
                )
                new_session_refresh_update_st_from_cookie = (
                    session_refresh_update_st_from_cookie
                    if session_refresh_update_st_from_cookie is not None
                    else current.get("session_refresh_update_st_from_cookie", True)
                )
                new_session_refresh_fail_if_st_refresh_fails = (
                    session_refresh_fail_if_st_refresh_fails
                    if session_refresh_fail_if_st_refresh_fails is not None
                    else current.get("session_refresh_fail_if_st_refresh_fails", True)
                )
                new_session_refresh_local_only = (
                    session_refresh_local_only
                    if session_refresh_local_only is not None
                    else current.get("session_refresh_local_only", True)
                )
                new_session_refresh_scheduler_enabled = (
                    session_refresh_scheduler_enabled
                    if session_refresh_scheduler_enabled is not None
                    else current.get("session_refresh_scheduler_enabled", False)
                )
                new_session_refresh_scheduler_interval_minutes = (
                    session_refresh_scheduler_interval_minutes
                    if session_refresh_scheduler_interval_minutes is not None
                    else current.get("session_refresh_scheduler_interval_minutes", 30)
                )
                new_session_refresh_scheduler_batch_size = (
                    session_refresh_scheduler_batch_size
                    if session_refresh_scheduler_batch_size is not None
                    else current.get("session_refresh_scheduler_batch_size", 10)
                )
                new_session_refresh_scheduler_only_expiring_within_minutes = (
                    session_refresh_scheduler_only_expiring_within_minutes
                    if session_refresh_scheduler_only_expiring_within_minutes is not None
                    else current.get("session_refresh_scheduler_only_expiring_within_minutes", 60)
                )
                new_st_only_refresh_scheduler_enabled = (
                    st_only_refresh_scheduler_enabled
                    if st_only_refresh_scheduler_enabled is not None
                    else current.get("st_only_refresh_scheduler_enabled", False)
                )
                new_st_only_refresh_scheduler_interval_minutes = (
                    st_only_refresh_scheduler_interval_minutes
                    if st_only_refresh_scheduler_interval_minutes is not None
                    else current.get("st_only_refresh_scheduler_interval_minutes", 5)
                )
                new_st_only_refresh_scheduler_batch_size = (
                    st_only_refresh_scheduler_batch_size
                    if st_only_refresh_scheduler_batch_size is not None
                    else current.get("st_only_refresh_scheduler_batch_size", 20)
                )
                new_st_only_refresh_scheduler_expiring_within_minutes = (
                    st_only_refresh_scheduler_expiring_within_minutes
                    if st_only_refresh_scheduler_expiring_within_minutes is not None
                    else current.get("st_only_refresh_scheduler_expiring_within_minutes", 5)
                )
                new_extension_queue_wait_timeout = (
                    extension_queue_wait_timeout_seconds
                    if extension_queue_wait_timeout_seconds is not None
                    else current.get("extension_queue_wait_timeout_seconds", 20)
                )
                new_dedicated_extension_enabled = (
                    dedicated_extension_enabled
                    if dedicated_extension_enabled is not None
                    else current.get("dedicated_extension_enabled", False)
                )
                new_dedicated_extension_captcha_timeout = (
                    dedicated_extension_captcha_timeout_seconds
                    if dedicated_extension_captcha_timeout_seconds is not None
                    else current.get("dedicated_extension_captcha_timeout_seconds", 25)
                )
                new_dedicated_extension_st_refresh_timeout = (
                    dedicated_extension_st_refresh_timeout_seconds
                    if dedicated_extension_st_refresh_timeout_seconds is not None
                    else current.get("dedicated_extension_st_refresh_timeout_seconds", 45)
                )
                new_remote_timeout = max(5, int(new_remote_timeout)) if new_remote_timeout is not None else 60
                new_browser_count = max(1, min(20, int(new_browser_count)))
                new_browser_personal_fresh_restart_every_n_solves = max(
                    0,
                    int(new_browser_personal_fresh_restart_every_n_solves),
                )
                new_personal_project_pool_size = max(1, min(50, int(new_personal_project_pool_size)))
                new_personal_max_tabs = max(1, min(50, int(new_personal_max_tabs)))  # 限制1-50
                new_personal_idle_ttl = max(60, int(new_personal_idle_ttl))  # 最少60秒
                new_session_refresh_wait_seconds_per_url = max(
                    0, min(600, int(new_session_refresh_wait_seconds_per_url))
                )
                new_session_refresh_overall_timeout_seconds = max(
                    10, min(1800, int(new_session_refresh_overall_timeout_seconds))
                )
                new_session_refresh_scheduler_interval_minutes = max(
                    1, min(1440, int(new_session_refresh_scheduler_interval_minutes))
                )
                new_session_refresh_scheduler_batch_size = max(
                    1, min(200, int(new_session_refresh_scheduler_batch_size))
                )
                new_session_refresh_scheduler_only_expiring_within_minutes = max(
                    1, min(10080, int(new_session_refresh_scheduler_only_expiring_within_minutes))
                )
                new_st_only_refresh_scheduler_interval_minutes = max(
                    1, min(1440, int(new_st_only_refresh_scheduler_interval_minutes))
                )
                new_st_only_refresh_scheduler_batch_size = max(
                    1, min(200, int(new_st_only_refresh_scheduler_batch_size))
                )
                new_st_only_refresh_scheduler_expiring_within_minutes = max(
                    1, min(10080, int(new_st_only_refresh_scheduler_expiring_within_minutes))
                )
                new_extension_queue_wait_timeout = max(1, min(120, int(new_extension_queue_wait_timeout)))
                new_dedicated_extension_captcha_timeout = max(5, min(180, int(new_dedicated_extension_captcha_timeout)))
                new_dedicated_extension_st_refresh_timeout = max(
                    10, min(300, int(new_dedicated_extension_st_refresh_timeout))
                )
                new_extension_fallback_to_managed_on_dedicated_failure = (
                    bool(extension_fallback_to_managed_on_dedicated_failure)
                    if extension_fallback_to_managed_on_dedicated_failure is not None
                    else bool(current.get("extension_fallback_to_managed_on_dedicated_failure", False))
                )
                new_session_refresh_warmup_urls = (
                    str(new_session_refresh_warmup_urls or "").strip()
                    or "https://labs.google/fx/tools/flow,https://labs.google/fx"
                )

                await db.execute(
                    """
                    UPDATE captcha_config
                    SET captcha_method = ?, yescaptcha_api_key = ?, yescaptcha_base_url = ?,
                        yescaptcha_task_type = ?,
                        capmonster_api_key = ?, capmonster_base_url = ?, capmonster_min_score = ?,
                        ezcaptcha_api_key = ?, ezcaptcha_base_url = ?,
                        capsolver_api_key = ?, capsolver_base_url = ?,
                        remote_browser_base_url = ?, remote_browser_api_key = ?, remote_browser_timeout = ?,
                        browser_fallback_to_remote_browser = ?,
                        browser_proxy_enabled = ?, browser_proxy_url = ?, browser_count = ?,
                        browser_personal_fresh_restart_every_n_solves = ?,
                        personal_project_pool_size = ?,
                        personal_max_resident_tabs = ?, personal_idle_tab_ttl_seconds = ?,
                        browser_captcha_page_url = ?,
                        session_refresh_enabled = ?, session_refresh_browser_first = ?,
                        session_refresh_inject_st_cookie = ?, session_refresh_warmup_urls = ?,
                        session_refresh_wait_seconds_per_url = ?, session_refresh_overall_timeout_seconds = ?,
                        session_refresh_update_st_from_cookie = ?, session_refresh_fail_if_st_refresh_fails = ?,
                        session_refresh_local_only = ?, session_refresh_scheduler_enabled = ?,
                        session_refresh_scheduler_interval_minutes = ?, session_refresh_scheduler_batch_size = ?,
                        session_refresh_scheduler_only_expiring_within_minutes = ?,
                        st_only_refresh_scheduler_enabled = ?,
                        st_only_refresh_scheduler_interval_minutes = ?,
                        st_only_refresh_scheduler_batch_size = ?,
                        st_only_refresh_scheduler_expiring_within_minutes = ?,
                        extension_queue_wait_timeout_seconds = ?,
                        dedicated_extension_enabled = ?,
                        dedicated_extension_captcha_timeout_seconds = ?,
                        dedicated_extension_st_refresh_timeout_seconds = ?,
                        extension_fallback_to_managed_on_dedicated_failure = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (
                        new_method,
                        new_yes_key,
                        new_yes_url,
                        new_yes_task_type,
                        new_cap_key,
                        new_cap_url,
                        new_cap_min_score,
                        new_ez_key,
                        new_ez_url,
                        new_cs_key,
                        new_cs_url,
                        (new_remote_base_url or "").strip(),
                        (new_remote_api_key or "").strip(),
                        new_remote_timeout,
                        bool(new_browser_fallback),
                        new_proxy_enabled,
                        new_proxy_url,
                        new_browser_count,
                        new_browser_personal_fresh_restart_every_n_solves,
                        new_personal_project_pool_size,
                        new_personal_max_tabs,
                        new_personal_idle_ttl,
                        new_browser_captcha_page_url,
                        bool(new_session_refresh_enabled),
                        bool(new_session_refresh_browser_first),
                        bool(new_session_refresh_inject_st_cookie),
                        new_session_refresh_warmup_urls,
                        new_session_refresh_wait_seconds_per_url,
                        new_session_refresh_overall_timeout_seconds,
                        bool(new_session_refresh_update_st_from_cookie),
                        bool(new_session_refresh_fail_if_st_refresh_fails),
                        bool(new_session_refresh_local_only),
                        bool(new_session_refresh_scheduler_enabled),
                        new_session_refresh_scheduler_interval_minutes,
                        new_session_refresh_scheduler_batch_size,
                        new_session_refresh_scheduler_only_expiring_within_minutes,
                        bool(new_st_only_refresh_scheduler_enabled),
                        new_st_only_refresh_scheduler_interval_minutes,
                        new_st_only_refresh_scheduler_batch_size,
                        new_st_only_refresh_scheduler_expiring_within_minutes,
                        new_extension_queue_wait_timeout,
                        bool(new_dedicated_extension_enabled),
                        new_dedicated_extension_captcha_timeout,
                        new_dedicated_extension_st_refresh_timeout,
                        bool(new_extension_fallback_to_managed_on_dedicated_failure),
                    ),
                )
            else:
                new_method = captcha_method if captcha_method is not None else "yescaptcha"
                new_yes_key = yescaptcha_api_key if yescaptcha_api_key is not None else ""
                new_yes_url = yescaptcha_base_url if yescaptcha_base_url is not None else "https://api.yescaptcha.com"
                new_yes_task_type = normalize_yescaptcha_task_type(yescaptcha_task_type)
                new_cap_key = capmonster_api_key if capmonster_api_key is not None else ""
                new_cap_url = capmonster_base_url if capmonster_base_url is not None else "https://api.capmonster.cloud"
                new_cap_min_score = normalize_capmonster_min_score(
                    capmonster_min_score if capmonster_min_score is not None else DEFAULT_CAPMONSTER_MIN_SCORE
                )
                new_ez_key = ezcaptcha_api_key if ezcaptcha_api_key is not None else ""
                new_ez_url = ezcaptcha_base_url if ezcaptcha_base_url is not None else "https://api.ez-captcha.com"
                new_cs_key = capsolver_api_key if capsolver_api_key is not None else ""
                new_cs_url = capsolver_base_url if capsolver_base_url is not None else "https://api.capsolver.com"
                new_remote_base_url = remote_browser_base_url if remote_browser_base_url is not None else ""
                new_remote_api_key = remote_browser_api_key if remote_browser_api_key is not None else ""
                new_remote_timeout = remote_browser_timeout if remote_browser_timeout is not None else 60
                new_browser_fallback = (
                    bool(browser_fallback_to_remote_browser) if browser_fallback_to_remote_browser is not None else True
                )
                new_proxy_enabled = browser_proxy_enabled if browser_proxy_enabled is not None else False
                new_proxy_url = browser_proxy_url
                new_browser_count = browser_count if browser_count is not None else 1
                new_browser_personal_fresh_restart_every_n_solves = (
                    browser_personal_fresh_restart_every_n_solves
                    if browser_personal_fresh_restart_every_n_solves is not None
                    else 10
                )
                new_personal_project_pool_size = (
                    personal_project_pool_size if personal_project_pool_size is not None else 4
                )
                new_personal_max_tabs = personal_max_resident_tabs if personal_max_resident_tabs is not None else 5
                new_personal_idle_ttl = (
                    personal_idle_tab_ttl_seconds if personal_idle_tab_ttl_seconds is not None else 600
                )
                default_page_url = "https://labs.google/fx/api/auth/providers"
                new_browser_captcha_page_url = (
                    (browser_captcha_page_url or default_page_url).strip()
                    if browser_captcha_page_url is not None
                    else default_page_url
                )
                new_browser_captcha_page_url = new_browser_captcha_page_url or default_page_url
                new_remote_timeout = max(5, int(new_remote_timeout))
                new_browser_count = max(1, min(20, int(new_browser_count)))
                new_browser_personal_fresh_restart_every_n_solves = max(
                    0,
                    int(new_browser_personal_fresh_restart_every_n_solves),
                )
                new_personal_project_pool_size = max(1, min(50, int(new_personal_project_pool_size)))
                new_personal_max_tabs = max(1, min(50, int(new_personal_max_tabs)))
                new_personal_idle_ttl = max(60, int(new_personal_idle_ttl))
                new_session_refresh_enabled = (
                    bool(session_refresh_enabled) if session_refresh_enabled is not None else True
                )
                new_session_refresh_browser_first = (
                    bool(session_refresh_browser_first) if session_refresh_browser_first is not None else True
                )
                new_session_refresh_inject_st_cookie = (
                    bool(session_refresh_inject_st_cookie) if session_refresh_inject_st_cookie is not None else True
                )
                new_session_refresh_warmup_urls = (
                    str(session_refresh_warmup_urls or "").strip()
                    if session_refresh_warmup_urls is not None
                    else "https://labs.google/fx/tools/flow,https://labs.google/fx"
                ) or "https://labs.google/fx/tools/flow,https://labs.google/fx"
                new_session_refresh_wait_seconds_per_url = max(
                    0,
                    min(
                        600,
                        int(
                            session_refresh_wait_seconds_per_url
                            if session_refresh_wait_seconds_per_url is not None
                            else 60
                        ),
                    ),
                )
                new_session_refresh_overall_timeout_seconds = max(
                    10,
                    min(
                        1800,
                        int(
                            session_refresh_overall_timeout_seconds
                            if session_refresh_overall_timeout_seconds is not None
                            else 180
                        ),
                    ),
                )
                new_session_refresh_update_st_from_cookie = (
                    bool(session_refresh_update_st_from_cookie)
                    if session_refresh_update_st_from_cookie is not None
                    else True
                )
                new_session_refresh_fail_if_st_refresh_fails = (
                    bool(session_refresh_fail_if_st_refresh_fails)
                    if session_refresh_fail_if_st_refresh_fails is not None
                    else True
                )
                new_session_refresh_local_only = (
                    bool(session_refresh_local_only) if session_refresh_local_only is not None else True
                )
                new_session_refresh_scheduler_enabled = (
                    bool(session_refresh_scheduler_enabled) if session_refresh_scheduler_enabled is not None else False
                )
                new_session_refresh_scheduler_interval_minutes = max(
                    1,
                    min(
                        1440,
                        int(
                            session_refresh_scheduler_interval_minutes
                            if session_refresh_scheduler_interval_minutes is not None
                            else 30
                        ),
                    ),
                )
                new_session_refresh_scheduler_batch_size = max(
                    1,
                    min(
                        200,
                        int(
                            session_refresh_scheduler_batch_size
                            if session_refresh_scheduler_batch_size is not None
                            else 10
                        ),
                    ),
                )
                new_session_refresh_scheduler_only_expiring_within_minutes = max(
                    1,
                    min(
                        10080,
                        int(
                            session_refresh_scheduler_only_expiring_within_minutes
                            if session_refresh_scheduler_only_expiring_within_minutes is not None
                            else 60
                        ),
                    ),
                )
                new_st_only_refresh_scheduler_enabled = (
                    bool(st_only_refresh_scheduler_enabled) if st_only_refresh_scheduler_enabled is not None else False
                )
                new_st_only_refresh_scheduler_interval_minutes = max(
                    1,
                    min(
                        1440,
                        int(
                            st_only_refresh_scheduler_interval_minutes
                            if st_only_refresh_scheduler_interval_minutes is not None
                            else 5
                        ),
                    ),
                )
                new_st_only_refresh_scheduler_batch_size = max(
                    1,
                    min(
                        200,
                        int(
                            st_only_refresh_scheduler_batch_size
                            if st_only_refresh_scheduler_batch_size is not None
                            else 20
                        ),
                    ),
                )
                new_st_only_refresh_scheduler_expiring_within_minutes = max(
                    1,
                    min(
                        10080,
                        int(
                            st_only_refresh_scheduler_expiring_within_minutes
                            if st_only_refresh_scheduler_expiring_within_minutes is not None
                            else 5
                        ),
                    ),
                )
                new_extension_queue_wait_timeout = max(
                    1,
                    min(
                        120,
                        int(
                            extension_queue_wait_timeout_seconds
                            if extension_queue_wait_timeout_seconds is not None
                            else 20
                        ),
                    ),
                )
                new_dedicated_extension_enabled = (
                    bool(dedicated_extension_enabled) if dedicated_extension_enabled is not None else False
                )
                new_dedicated_extension_captcha_timeout = max(
                    5,
                    min(
                        180,
                        int(
                            dedicated_extension_captcha_timeout_seconds
                            if dedicated_extension_captcha_timeout_seconds is not None
                            else 25
                        ),
                    ),
                )
                new_dedicated_extension_st_refresh_timeout = max(
                    10,
                    min(
                        300,
                        int(
                            dedicated_extension_st_refresh_timeout_seconds
                            if dedicated_extension_st_refresh_timeout_seconds is not None
                            else 45
                        ),
                    ),
                )
                new_extension_fallback_to_managed_on_dedicated_failure = (
                    bool(extension_fallback_to_managed_on_dedicated_failure)
                    if extension_fallback_to_managed_on_dedicated_failure is not None
                    else False
                )

                await db.execute(
                    """
                    INSERT INTO captcha_config (id, captcha_method, yescaptcha_api_key, yescaptcha_base_url,
                        yescaptcha_task_type,
                        capmonster_api_key, capmonster_base_url, capmonster_min_score,
                        ezcaptcha_api_key, ezcaptcha_base_url,
                        capsolver_api_key, capsolver_base_url,
                        remote_browser_base_url, remote_browser_api_key, remote_browser_timeout,
                        browser_fallback_to_remote_browser,
                        browser_proxy_enabled, browser_proxy_url, browser_count,
                        browser_personal_fresh_restart_every_n_solves,
                        personal_project_pool_size,
                        personal_max_resident_tabs, personal_idle_tab_ttl_seconds,
                        browser_captcha_page_url,
                        session_refresh_enabled, session_refresh_browser_first,
                        session_refresh_inject_st_cookie, session_refresh_warmup_urls,
                        session_refresh_wait_seconds_per_url, session_refresh_overall_timeout_seconds,
                        session_refresh_update_st_from_cookie, session_refresh_fail_if_st_refresh_fails,
                        session_refresh_local_only, session_refresh_scheduler_enabled,
                        session_refresh_scheduler_interval_minutes, session_refresh_scheduler_batch_size,
                        session_refresh_scheduler_only_expiring_within_minutes,
                        st_only_refresh_scheduler_enabled,
                        st_only_refresh_scheduler_interval_minutes,
                        st_only_refresh_scheduler_batch_size,
                        st_only_refresh_scheduler_expiring_within_minutes,
                        extension_queue_wait_timeout_seconds,
                        dedicated_extension_enabled, dedicated_extension_captcha_timeout_seconds,
                        dedicated_extension_st_refresh_timeout_seconds,
                        extension_fallback_to_managed_on_dedicated_failure)
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        new_method,
                        new_yes_key,
                        new_yes_url,
                        new_yes_task_type,
                        new_cap_key,
                        new_cap_url,
                        new_cap_min_score,
                        new_ez_key,
                        new_ez_url,
                        new_cs_key,
                        new_cs_url,
                        (new_remote_base_url or "").strip(),
                        (new_remote_api_key or "").strip(),
                        new_remote_timeout,
                        new_browser_fallback,
                        new_proxy_enabled,
                        new_proxy_url,
                        new_browser_count,
                        new_browser_personal_fresh_restart_every_n_solves,
                        new_personal_project_pool_size,
                        new_personal_max_tabs,
                        new_personal_idle_ttl,
                        new_browser_captcha_page_url,
                        new_session_refresh_enabled,
                        new_session_refresh_browser_first,
                        new_session_refresh_inject_st_cookie,
                        new_session_refresh_warmup_urls,
                        new_session_refresh_wait_seconds_per_url,
                        new_session_refresh_overall_timeout_seconds,
                        new_session_refresh_update_st_from_cookie,
                        new_session_refresh_fail_if_st_refresh_fails,
                        new_session_refresh_local_only,
                        new_session_refresh_scheduler_enabled,
                        new_session_refresh_scheduler_interval_minutes,
                        new_session_refresh_scheduler_batch_size,
                        new_session_refresh_scheduler_only_expiring_within_minutes,
                        bool(new_st_only_refresh_scheduler_enabled),
                        new_st_only_refresh_scheduler_interval_minutes,
                        new_st_only_refresh_scheduler_batch_size,
                        new_st_only_refresh_scheduler_expiring_within_minutes,
                        new_extension_queue_wait_timeout,
                        new_dedicated_extension_enabled,
                        new_dedicated_extension_captcha_timeout,
                        new_dedicated_extension_st_refresh_timeout,
                        bool(new_extension_fallback_to_managed_on_dedicated_failure),
                    ),
                )

            await db.commit()

    # Plugin config operations
    async def get_plugin_config(self) -> PluginConfig:
        """Get plugin configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM plugin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return PluginConfig(**dict(row))
            return PluginConfig()

    async def update_plugin_config(self, connection_token: str, auto_enable_on_update: bool = True):
        """Update plugin configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM plugin_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                await db.execute(
                    """
                    UPDATE plugin_config
                    SET connection_token = ?, auto_enable_on_update = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """,
                    (connection_token, auto_enable_on_update),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO plugin_config (id, connection_token, auto_enable_on_update)
                    VALUES (1, ?, ?)
                """,
                    (connection_token, auto_enable_on_update),
                )

            await db.commit()

    # API key manager operations
    async def _get_or_create_api_client(self, client_name: str) -> int:
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT id FROM api_clients WHERE name = ?", (client_name,))
            row = await cursor.fetchone()
            if row:
                return int(row["id"])
            cursor = await db.execute(
                "INSERT INTO api_clients (name, is_active) VALUES (?, 1)",
                (client_name,),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def create_client_api_key(
        self,
        *,
        client_name: str,
        label: str,
        key_prefix: str,
        key_plaintext: Optional[str],
        key_hash: str,
        scopes: str,
        account_ids: List[int],
        endpoint_limits: Dict[str, Dict[str, int]],
        expires_at: Optional[str],
    ) -> int:
        client_id = await self._get_or_create_api_client(client_name)
        ac, am, at_ = derive_adobe_ints_from_scopes_csv(scopes)
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO api_keys (
                    client_id, label, key_prefix, key_plaintext, key_hash, scopes, is_active, expires_at,
                    adobe_cloning_enabled, adobe_metadata_enabled, adobe_tracker_enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    label,
                    key_prefix,
                    key_plaintext,
                    key_hash,
                    scopes,
                    expires_at,
                    ac,
                    am,
                    at_,
                ),
            )
            key_id = int(cursor.lastrowid)

            for account_id in sorted({int(x) for x in account_ids if int(x) > 0}):
                await db.execute(
                    "INSERT OR IGNORE INTO api_key_accounts (api_key_id, account_id) VALUES (?, ?)",
                    (key_id, account_id),
                )

            for endpoint, values in (endpoint_limits or {}).items():
                rpm = int((values or {}).get("rpm") or 0)
                rph = int((values or {}).get("rph") or 0)
                burst = int((values or {}).get("burst") or 0)
                await db.execute(
                    """
                    INSERT OR REPLACE INTO api_key_rate_limits (api_key_id, endpoint, rpm, rph, burst, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (key_id, endpoint, rpm, rph, burst),
                )

            await db.commit()
            return key_id

    async def get_client_api_key_by_hash(self, key_hash: str) -> Optional[Dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    k.id, k.label, k.key_prefix, k.scopes, k.is_active,
                    k.expires_at,
                    c.name AS client_name,
                    strftime('%s', k.expires_at) AS expires_unix
                FROM api_keys k
                JOIN api_clients c ON c.id = k.client_id
                WHERE k.key_hash = ?
                LIMIT 1
                """,
                (key_hash,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_api_key_auth_cache_seed(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """Return managed-key auth records and assignments for startup warming."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    k.id, k.key_hash, k.label, k.key_prefix, k.scopes, k.is_active,
                    k.expires_at, c.name AS client_name,
                    strftime('%s', k.expires_at) AS expires_unix
                FROM api_keys k
                JOIN api_clients c ON c.id = k.client_id
                ORDER BY k.id ASC
                LIMIT ?
                """,
                (max(1, min(int(limit), 10_000)),),
            )
            records = [dict(row) for row in await cursor.fetchall()]
        for record in records:
            record["account_ids"] = await self.get_api_key_account_ids(int(record["id"]))
        return records

    async def list_active_task_progress_seed(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """Return active task progress from every durable task table."""
        safe_limit = max(1, min(int(limit), 10_000))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT 'native' AS task_kind, task_id, status, progress, job_phase, created_at
                FROM tasks
                WHERE status NOT IN ('completed', 'failed', 'cancelled', 'canceled')
                UNION ALL
                SELECT 'geminigen', job_id, status, progress, NULL, created_at
                FROM geminigen_tasks
                WHERE status IN ('queued', 'processing')
                UNION ALL
                SELECT 'runway', job_id, status, progress, NULL, created_at
                FROM runway_tasks
                WHERE status NOT IN ('completed', 'failed', 'cancelled', 'canceled')
                LIMIT ?
                """,
                (safe_limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_api_key_account_ids(self, key_id: int, existing_only: bool = False) -> List[int]:
        async with self._connect() as db:
            if existing_only:
                cursor = await db.execute(
                    """
                    SELECT aka.account_id
                    FROM api_key_accounts aka
                    JOIN tokens t ON t.id = aka.account_id
                    WHERE aka.api_key_id = ?
                    ORDER BY aka.account_id ASC
                    """,
                    (key_id,),
                )
            else:
                cursor = await db.execute(
                    "SELECT account_id FROM api_key_accounts WHERE api_key_id = ? ORDER BY account_id ASC",
                    (key_id,),
                )
            rows = await cursor.fetchall()
            return [int(row[0]) for row in rows]

    async def prune_stale_api_key_accounts(self, key_id: int) -> int:
        """Remove api_key_accounts rows whose account_id no longer exists in tokens."""
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                DELETE FROM api_key_accounts
                WHERE api_key_id = ?
                  AND account_id NOT IN (SELECT id FROM tokens)
                """,
                (key_id,),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def get_api_key_rate_limits(self, key_id: int, endpoint: str) -> Dict[str, Any]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT rpm, rph, burst
                FROM api_key_rate_limits
                WHERE api_key_id = ? AND endpoint IN (?, '*')
                ORDER BY CASE WHEN endpoint = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (key_id, endpoint, endpoint),
            )
            row = await cursor.fetchone()
            return dict(row) if row else {}

    async def touch_api_key_usage(self, key_id: int):
        async with self._connect(write=True) as db:
            await db.execute(
                "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (key_id,),
            )
            await db.commit()

    async def touch_api_key_presence(self, key_id: int):
        async with self._connect(write=True) as db:
            await db.execute(
                "UPDATE api_keys SET last_presence_at = CURRENT_TIMESTAMP WHERE id = ?",
                (key_id,),
            )
            await db.commit()

    async def insert_api_key_audit_log(
        self,
        *,
        api_key_id: Optional[int],
        endpoint: str,
        account_id: Optional[int],
        status_code: int,
        detail: str,
        ip: str,
        user_agent: str,
    ):
        async with self._connect(write=True) as db:
            await db.execute(
                """
                INSERT INTO api_key_audit_logs (api_key_id, endpoint, account_id, status_code, detail, ip, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (api_key_id, endpoint, account_id, status_code, detail[:300], ip[:120], user_agent[:200]),
            )
            await db.commit()

    async def list_api_keys(self) -> List[Dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    k.id,
                    c.name AS client_name,
                    k.label,
                    k.key_prefix,
                    CASE WHEN k.key_plaintext IS NOT NULL AND length(trim(k.key_plaintext)) > 0 THEN 1 ELSE 0 END AS can_reveal_plaintext,
                    k.scopes,
                    k.is_active,
                    k.expires_at,
                    k.last_used_at,
                    k.last_presence_at,
                    CASE
                        WHEN k.is_active = 1
                         AND k.last_presence_at IS NOT NULL
                         AND datetime(k.last_presence_at) >= datetime('now', '-90 seconds')
                        THEN 1 ELSE 0
                    END AS is_online,
                    COALESCE(k.adobe_cloning_enabled, 1) AS adobe_cloning_enabled,
                    COALESCE(k.adobe_metadata_enabled, 1) AS adobe_metadata_enabled,
                    COALESCE(k.adobe_tracker_enabled, 1) AS adobe_tracker_enabled,
                    k.created_at
                FROM api_keys k
                JOIN api_clients c ON c.id = k.client_id
                ORDER BY k.created_at DESC
                """
            )
            rows = [dict(row) for row in await cursor.fetchall()]

            for row in rows:
                row["is_online"] = bool(row.get("is_online"))
                row["account_ids"] = await self.get_api_key_account_ids(
                    int(row["id"]),
                    existing_only=True,
                )
            return rows

    async def list_api_key_rate_limits(self, key_id: int) -> List[Dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT endpoint, rpm, rph, burst
                FROM api_key_rate_limits
                WHERE api_key_id = ?
                ORDER BY endpoint ASC
                """,
                (key_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def update_api_key(
        self,
        key_id: int,
        *,
        is_active: Optional[bool] = None,
        client_name: Optional[str] = None,
        label: Optional[str] = None,
        scopes: Optional[str] = None,
        expires_at: Optional[str] = None,
        account_ids: Optional[List[int]] = None,
        endpoint_limits: Optional[Dict[str, Dict[str, int]]] = None,
    ):
        # Resolve client outside the write transaction: _get_or_create_api_client also
        # takes _write_lock; nesting here deadlocks asyncio.Lock until Cloudflare 524.
        resolved_client_id: Optional[int] = None
        if client_name is not None:
            resolved_client_id = await self._get_or_create_api_client(client_name.strip())

        async with self._connect(write=True) as db:
            if resolved_client_id is not None:
                await db.execute(
                    "UPDATE api_keys SET client_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (resolved_client_id, key_id),
                )
            if label is not None:
                await db.execute(
                    "UPDATE api_keys SET label = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (label, key_id),
                )
            if is_active is not None:
                await db.execute(
                    "UPDATE api_keys SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (1 if is_active else 0, key_id),
                )
            if scopes is not None:
                ac, am, at_ = derive_adobe_ints_from_scopes_csv(scopes)
                await db.execute(
                    """
                    UPDATE api_keys SET scopes = ?, adobe_cloning_enabled = ?, adobe_metadata_enabled = ?,
                    adobe_tracker_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
                    """,
                    (scopes, ac, am, at_, key_id),
                )
            if expires_at is not None:
                await db.execute(
                    "UPDATE api_keys SET expires_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (expires_at, key_id),
                )
            if account_ids is not None:
                await db.execute("DELETE FROM api_key_accounts WHERE api_key_id = ?", (key_id,))
                for account_id in sorted({int(x) for x in account_ids if int(x) > 0}):
                    await db.execute(
                        "INSERT OR IGNORE INTO api_key_accounts (api_key_id, account_id) VALUES (?, ?)",
                        (key_id, account_id),
                    )
            if endpoint_limits is not None:
                await db.execute("DELETE FROM api_key_rate_limits WHERE api_key_id = ?", (key_id,))
                for endpoint, values in endpoint_limits.items():
                    await db.execute(
                        """
                        INSERT INTO api_key_rate_limits (api_key_id, endpoint, rpm, rph, burst)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            key_id,
                            endpoint,
                            int((values or {}).get("rpm") or 0),
                            int((values or {}).get("rph") or 0),
                            int((values or {}).get("burst") or 0),
                        ),
                    )
            await db.commit()

    async def get_api_key_detail(self, key_id: int, include_plaintext: bool = False) -> Optional[Dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            plaintext_col = "k.key_plaintext," if include_plaintext else "NULL AS key_plaintext,"
            cursor = await db.execute(
                f"""
                SELECT
                    k.id,
                    c.name AS client_name,
                    k.label,
                    k.key_prefix,
                    {plaintext_col}
                    k.scopes,
                    k.is_active,
                    k.expires_at,
                    k.last_used_at,
                    k.last_presence_at,
                    CASE
                        WHEN k.is_active = 1
                         AND k.last_presence_at IS NOT NULL
                         AND datetime(k.last_presence_at) >= datetime('now', '-90 seconds')
                        THEN 1 ELSE 0
                    END AS is_online,
                    COALESCE(k.adobe_cloning_enabled, 1) AS adobe_cloning_enabled,
                    COALESCE(k.adobe_metadata_enabled, 1) AS adobe_metadata_enabled,
                    COALESCE(k.adobe_tracker_enabled, 1) AS adobe_tracker_enabled,
                    k.created_at
                FROM api_keys k
                JOIN api_clients c ON c.id = k.client_id
                WHERE k.id = ?
                LIMIT 1
                """,
                (key_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            data = dict(row)
            data["is_online"] = bool(data.get("is_online"))
            data["account_ids"] = await self.get_api_key_account_ids(
                key_id,
                existing_only=True,
            )
            data["endpoint_limits"] = await self.list_api_key_rate_limits(key_id)
            return data

    async def delete_api_key(self, key_id: int) -> Dict[str, Any]:
        """Delete a managed key without deleting historical generation records."""
        key_id = int(key_id)
        async with self._connect(write=True) as db:
            cursor = await db.execute("SELECT 1 FROM api_keys WHERE id = ? LIMIT 1", (key_id,))
            if not await cursor.fetchone():
                return {
                    "deleted": False,
                    "historical_records_detached": 0,
                    "cache_metadata_deleted": 0,
                    "extension_bindings_deleted": 0,
                }

            historical_records_detached = 0
            for table_name in (
                "projects",
                "tasks",
                "request_logs",
                "runway_tasks",
                "geminigen_tasks",
                "api_key_audit_logs",
            ):
                if not await self._table_exists(db, table_name):
                    continue
                cursor = await db.execute(
                    f"UPDATE {table_name} SET api_key_id = NULL WHERE api_key_id = ?",
                    (key_id,),
                )
                historical_records_detached += max(0, int(cursor.rowcount or 0))

            cache_metadata_deleted = 0
            if await self._table_exists(db, "cache_files"):
                cursor = await db.execute("DELETE FROM cache_files WHERE api_key_id = ?", (key_id,))
                cache_metadata_deleted = max(0, int(cursor.rowcount or 0))

            extension_bindings_deleted = 0
            if await self._table_exists(db, "extension_worker_bindings"):
                cursor = await db.execute(
                    "DELETE FROM extension_worker_bindings WHERE api_key_id = ?",
                    (key_id,),
                )
                extension_bindings_deleted = max(0, int(cursor.rowcount or 0))

            await db.execute("DELETE FROM api_key_accounts WHERE api_key_id = ?", (key_id,))
            await db.execute("DELETE FROM api_key_rate_limits WHERE api_key_id = ?", (key_id,))
            await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            await db.commit()
            return {
                "deleted": True,
                "historical_records_detached": historical_records_detached,
                "cache_metadata_deleted": cache_metadata_deleted,
                "extension_bindings_deleted": extension_bindings_deleted,
            }

    async def count_api_key_audit_logs(self, key_id: Optional[int] = None) -> int:
        async with self._connect() as db:
            if key_id is not None:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM api_key_audit_logs WHERE api_key_id = ?",
                    (key_id,),
                )
            else:
                cursor = await db.execute("SELECT COUNT(*) FROM api_key_audit_logs")
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    async def clear_api_key_audit_logs(self) -> int:
        """Delete every managed API key audit log and return the number removed."""
        async with self._connect(write=True) as db:
            cursor = await db.execute("DELETE FROM api_key_audit_logs")
            await db.commit()
            return int(cursor.rowcount or 0)

    async def list_api_key_audit_logs(
        self,
        limit: int = 200,
        offset: int = 0,
        key_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 200), 500))
        safe_offset = max(0, int(offset or 0))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if key_id is not None:
                cursor = await db.execute(
                    """
                    SELECT
                        l.id,
                        l.api_key_id,
                        k.key_prefix,
                        k.label,
                        l.endpoint,
                        l.account_id,
                        l.status_code,
                        l.detail,
                        l.ip,
                        l.user_agent,
                        l.created_at
                    FROM api_key_audit_logs l
                    LEFT JOIN api_keys k ON k.id = l.api_key_id
                    WHERE l.api_key_id = ?
                    ORDER BY l.created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (key_id, safe_limit, safe_offset),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT
                        l.id,
                        l.api_key_id,
                        k.key_prefix,
                        k.label,
                        l.endpoint,
                        l.account_id,
                        l.status_code,
                        l.detail,
                        l.ip,
                        l.user_agent,
                        l.created_at
                    FROM api_key_audit_logs l
                    LEFT JOIN api_keys k ON k.id = l.api_key_id
                    ORDER BY l.created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (safe_limit, safe_offset),
                )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_extension_worker_binding_for_route_key(self, route_key: str) -> Optional[Dict[str, Any]]:
        normalized = (route_key or "").strip()
        if not normalized:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, route_key, api_key_id, created_at, updated_at
                FROM extension_worker_bindings
                WHERE route_key = ?
                """,
                (normalized,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def upsert_extension_worker_binding(self, route_key: str, api_key_id: int) -> None:
        normalized = (route_key or "").strip()
        if not normalized:
            raise ValueError("route_key is required")
        normalized_key_id = int(api_key_id)
        async with self._connect(write=True) as db:
            await db.execute(
                """
                INSERT INTO extension_worker_bindings (route_key, api_key_id, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(route_key) DO UPDATE SET
                    api_key_id = excluded.api_key_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (normalized, normalized_key_id),
            )
            await db.commit()

    async def delete_extension_worker_binding(self, route_key: str) -> None:
        normalized = (route_key or "").strip()
        if not normalized:
            return
        async with self._connect(write=True) as db:
            await db.execute(
                "DELETE FROM extension_worker_bindings WHERE route_key = ?",
                (normalized,),
            )
            await db.commit()

    async def list_extension_worker_bindings(self) -> List[Dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    b.id,
                    b.route_key,
                    b.api_key_id,
                    b.created_at,
                    b.updated_at,
                    k.label AS api_key_label
                FROM extension_worker_bindings b
                LEFT JOIN api_keys k ON k.id = b.api_key_id
                ORDER BY b.updated_at DESC, b.route_key ASC
                """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_captcha_worker_key_by_hash(self, key_hash: str) -> Optional[Dict[str, Any]]:
        normalized = (key_hash or "").strip()
        if not normalized:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM captcha_worker_keys
                WHERE key_hash = ?
                LIMIT 1
                """,
                (normalized,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_captcha_worker_key(self, key_id: int) -> Optional[Dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM captcha_worker_keys WHERE id = ?",
                (int(key_id),),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_captcha_worker_key(
        self,
        *,
        key_prefix: str,
        key_hash: str,
        label: str = "",
        key_plaintext: Optional[str] = None,
    ) -> int:
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO captcha_worker_keys (key_prefix, key_hash, label, key_plaintext)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (key_prefix or "").strip(),
                    (key_hash or "").strip(),
                    (label or "").strip(),
                    (key_plaintext or "").strip() or None,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def update_captcha_worker_key(
        self,
        key_id: int,
        *,
        label: Any = _DEDICATED_WORKER_UPDATE_OMIT,
        is_active: Any = _DEDICATED_WORKER_UPDATE_OMIT,
        last_instance_id: Optional[str] = None,
        last_error: Optional[str] = None,
        mark_seen: bool = False,
    ) -> None:
        updates: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
        values: list[Any] = []
        if label is not _DEDICATED_WORKER_UPDATE_OMIT:
            updates.append("label = ?")
            values.append((label or "").strip())
        if is_active is not _DEDICATED_WORKER_UPDATE_OMIT:
            updates.append("is_active = ?")
            values.append(1 if is_active else 0)
        if last_instance_id is not None:
            updates.append("last_instance_id = ?")
            values.append((last_instance_id or "").strip() or None)
        if last_error is not None:
            updates.append("last_error = ?")
            values.append((last_error or "").strip() or None)
        if mark_seen:
            updates.append("last_seen_at = CURRENT_TIMESTAMP")
        values.append(int(key_id))
        async with self._connect(write=True) as db:
            await db.execute(
                f"UPDATE captcha_worker_keys SET {', '.join(updates)} WHERE id = ?",
                tuple(values),
            )
            await db.commit()

    async def delete_captcha_worker_key(self, key_id: int) -> None:
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM captcha_worker_keys WHERE id = ?", (int(key_id),))
            await db.commit()

    async def list_captcha_worker_keys(self) -> List[Dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM captcha_worker_keys
                ORDER BY updated_at DESC, id DESC
                """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_token_refresh_config(self) -> TokenRefreshConfig:
        """Return global protocol ST refresh settings."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_refresh_config WHERE id = 1")
            row = await cursor.fetchone()
            return TokenRefreshConfig(**dict(row)) if row else TokenRefreshConfig()

    async def update_token_refresh_config(
        self,
        *,
        enabled: Optional[bool] = None,
        refresh_interval_minutes: Optional[int] = None,
    ) -> TokenRefreshConfig:
        """Update global protocol ST refresh settings with bounded values."""
        current = await self.get_token_refresh_config()
        new_enabled = current.enabled if enabled is None else bool(enabled)
        raw_interval = (
            current.refresh_interval_minutes if refresh_interval_minutes is None else refresh_interval_minutes
        )
        try:
            interval = max(1, min(10080, int(raw_interval)))
        except Exception:
            interval = 120

        async with self._connect(write=True) as db:
            await db.execute(
                """
                INSERT INTO token_refresh_config (id, enabled, refresh_interval_minutes, updated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    enabled = excluded.enabled,
                    refresh_interval_minutes = excluded.refresh_interval_minutes,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (1 if new_enabled else 0, interval),
            )
            await db.commit()
        return await self.get_token_refresh_config()

    async def apply_redis_event_batch(self, events: List[Dict[str, Any]]) -> int:
        """Persist idempotent Redis events in one SQLite transaction."""
        if not events:
            return 0
        applied = 0
        async with self._connect(write=True) as db:
            for event in events[:100]:
                cursor = str(event.get("cursor") or "").strip()
                event_type = str(event.get("type") or "").strip()
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                if not cursor or not event_type:
                    continue
                marker = await db.execute(
                    "INSERT OR IGNORE INTO redis_persisted_events (cursor, event_type) VALUES (?, ?)",
                    (cursor, event_type),
                )
                if int(marker.rowcount or 0) == 0:
                    continue
                if event_type == "usage_touch":
                    key_id = int(data.get("api_key_id") or 0)
                    if key_id > 0:
                        await db.execute(
                            "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (key_id,),
                        )
                elif event_type == "api_key_audit":
                    await db.execute(
                        """
                        INSERT INTO api_key_audit_logs
                            (api_key_id, endpoint, account_id, status_code, detail, ip, user_agent)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            data.get("api_key_id"),
                            str(data.get("endpoint") or "")[:300],
                            data.get("account_id"),
                            int(data.get("status_code") or 0),
                            str(data.get("detail") or "")[:300],
                            str(data.get("ip") or "")[:120],
                            str(data.get("user_agent") or "")[:200],
                        ),
                    )
                applied += 1
            await db.commit()
        return applied


def create_database() -> Database:
    """Create the configured database backend for the temporary bridge release.

    Direct ``Database(path)`` construction remains SQLite-compatible for the
    offline importer and existing tests. The application uses this factory so
    all service-facing method signatures remain unchanged.
    """
    from .database_runtime import DatabaseSettings

    settings = DatabaseSettings.from_env()
    if settings.backend == "postgres":
        from .postgres_database import PostgresDatabase

        return PostgresDatabase(settings=settings)
    return Database()
