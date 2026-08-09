"""Redis-backed hot state, event delivery, and batched persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterable, Optional


REDIS_STREAM_KEY = "sub2gen:events"
REDIS_CONSUMER_GROUP = "sub2gen-persist"
REDIS_STATE_KEY = "sub2gen:state:version"
REDIS_STATE_VERSION = "1"
REDIS_MAINTENANCE_KEY = "sub2gen:maintenance"
REDIS_STREAM_MAXLEN = 100_000
AUTH_CACHE_TTL_SECONDS = 60
PRESENCE_TTL_SECONDS = 120
TASK_PROGRESS_TTL_SECONDS = 24 * 60 * 60


class RedisUnavailableError(RuntimeError):
    """Raised when protected work cannot safely use Redis."""


@dataclass(frozen=True)
class RedisEvent:
    cursor: str
    event_type: str
    data: Dict[str, Any]
    created_at: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cursor": self.cursor,
            "type": self.event_type,
            "data": self.data,
            "created_at": self.created_at,
        }


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


class RedisRuntime:
    """Owns Redis connectivity and keeps outage behavior consistent."""

    _RATE_LIMIT_LUA = """
    local minute_count = 0
    local hour_count = 0
    if tonumber(ARGV[1]) > 0 then
      minute_count = redis.call('INCR', KEYS[1])
      if minute_count == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3])) end
    end
    if tonumber(ARGV[2]) > 0 then
      hour_count = redis.call('INCR', KEYS[2])
      if hour_count == 1 then redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4])) end
    end
    if tonumber(ARGV[1]) > 0 and minute_count > tonumber(ARGV[1]) then
      return {1, minute_count, hour_count}
    end
    if tonumber(ARGV[2]) > 0 and hour_count > tonumber(ARGV[2]) then
      return {2, minute_count, hour_count}
    end
    return {0, minute_count, hour_count}
    """
    _ACQUIRE_SLOT_LUA = """
    local current = tonumber(redis.call('GET', KEYS[1]) or '0')
    local limit = tonumber(ARGV[1])
    if limit > 0 and current >= limit then return {0, current} end
    current = redis.call('INCR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
    return {1, current}
    """
    _RELEASE_SLOT_LUA = """
    local current = tonumber(redis.call('GET', KEYS[1]) or '0')
    if current <= 0 then redis.call('SET', KEYS[1], 0, 'EX', tonumber(ARGV[1])); return 0 end
    current = redis.call('DECR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
    return current
    """

    def __init__(self, url: Optional[str] = None, mode: Optional[str] = None):
        self.url = str(url if url is not None else os.environ.get("SUB2GEN_REDIS_URL", "") or "").strip()
        requested_mode = str(mode if mode is not None else os.environ.get("SUB2GEN_REDIS_MODE", "") or "").strip().lower()
        self.mode = requested_mode or ("shadow" if self.url else "off")
        if self.mode not in {"off", "shadow", "required"}:
            raise ValueError("SUB2GEN_REDIS_MODE must be off, shadow, or required")

        self.client: Any = None
        self.db: Any = None
        self.ready = False
        self.state_marker_present = False
        self.event_consumer_ready = False
        self.last_error = ""
        self.last_latency_ms = 0.0
        self.last_ready_at: Optional[str] = None
        self._stopping = False
        self._consumer_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._consumer_name = f"{socket.gethostname()}-{os.getpid()}"
        self._rate_script: Any = None
        self._acquire_slot_script: Any = None
        self._release_slot_script: Any = None
        self._last_stream_length = 0
        self._consumer_backlog = 0
        self.websocket_clients = 0
        self._last_claim_at = 0.0
        self._state_warmed = False
        self._last_warm_counts = {"auth_records": 0, "active_tasks": 0}
        self.maintenance_active = False
        self.maintenance_reason = ""
        self.maintenance_started_at: Optional[str] = None
        self.maintenance_owner = ""

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and bool(self.url)

    @property
    def required(self) -> bool:
        return self.mode == "required"

    async def start(self, db: Any = None) -> Dict[str, int]:
        self.db = db
        self._stopping = False
        if not self.enabled:
            self.ready = False
            self.last_error = "redis_disabled" if self.mode == "off" else "redis_url_missing"
            return dict(self._last_warm_counts)
        await self._connect_and_probe()
        self._health_task = asyncio.create_task(self._health_loop(), name="sub2gen-redis-health")
        self._consumer_task = asyncio.create_task(self._consumer_loop(), name="sub2gen-redis-persist")
        return dict(self._last_warm_counts)

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._consumer_task, self._health_task):
            if task:
                task.cancel()
        for task in (self._consumer_task, self._health_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._consumer_task = None
        self._health_task = None
        self.event_consumer_ready = False
        self._state_warmed = False
        client, self.client = self.client, None
        if client is not None:
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        self.ready = False

    async def _connect_and_probe(self) -> None:
        try:
            if self.client is None:
                try:
                    import redis.asyncio as redis
                except ImportError as exc:
                    raise RuntimeError("redis package is not installed") from exc
                self.client = redis.from_url(
                    self.url,
                    decode_responses=False,
                    socket_connect_timeout=1.5,
                    socket_timeout=2.0,
                    health_check_interval=15,
                    retry_on_timeout=True,
                )
            started = time.perf_counter()
            await self.client.ping()
            self.last_latency_ms = (time.perf_counter() - started) * 1000.0
            marker = await self.client.get(REDIS_STATE_KEY)
            marker_text = _decode(marker) if marker is not None else ""
            self.state_marker_present = marker_text == REDIS_STATE_VERSION
            if not self.state_marker_present:
                self.ready = False
                self.last_error = "redis_state_marker_missing" if not marker_text else "redis_state_version_mismatch"
                return
            await self._refresh_maintenance_state()
            await self._ensure_consumer_group()
            self._rate_script = self.client.register_script(self._RATE_LIMIT_LUA)
            self._acquire_slot_script = self.client.register_script(self._ACQUIRE_SLOT_LUA)
            self._release_slot_script = self.client.register_script(self._RELEASE_SLOT_LUA)
            if self.db is not None and not self._state_warmed:
                self._last_warm_counts = await self._warm_from_sqlite(self.db)
                self._state_warmed = True
            self.ready = True
            self.last_error = ""
            self.last_ready_at = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            self._mark_unavailable(exc)

    async def initialize_state(self, *, force: bool = False) -> Dict[str, Any]:
        """Explicitly initialize an empty Redis deployment.

        This is deliberately never called by application startup. Running it resets the
        safety marker and must therefore be an operator action.
        """
        if not self.url:
            raise RedisUnavailableError("SUB2GEN_REDIS_URL is not configured")
        if self.client is None:
            try:
                import redis.asyncio as redis
            except ImportError as exc:
                raise RuntimeError("redis package is not installed") from exc
            self.client = redis.from_url(self.url, decode_responses=False, socket_connect_timeout=2.0)
        existing = await self.client.get(REDIS_STATE_KEY)
        if existing is not None and _decode(existing) != REDIS_STATE_VERSION and not force:
            raise RedisUnavailableError("Redis contains a different sub2gen state version; use --force only after review")
        await self.client.set(REDIS_STATE_KEY, REDIS_STATE_VERSION)
        await self.client.set(
            "sub2gen:state:initialized_at",
            datetime.now(timezone.utc).isoformat(),
        )
        await self._connect_and_probe()
        return self.status_snapshot()

    async def warm_from_sqlite(self, db: Any) -> Dict[str, int]:
        """Warm managed auth and active task progress before serving traffic."""
        if not self.ready:
            return {"auth_records": 0, "active_tasks": 0}
        try:
            counts = await self._warm_from_sqlite(db)
            self._last_warm_counts = counts
            self._state_warmed = True
            return counts
        except Exception as exc:
            self._mark_unavailable(exc)
            return {"auth_records": 0, "active_tasks": 0}

    async def _warm_from_sqlite(self, db: Any) -> Dict[str, int]:
        """Populate hot state without exposing readiness before warming finishes."""
        auth_records = await db.list_api_key_auth_cache_seed()
        for source_record in auth_records:
            record = dict(source_record)
            key_hash = str(record.pop("key_hash", "") or "")
            account_ids = list(record.pop("account_ids", []) or [])
            if key_hash:
                await self._set_auth_cache(
                    key_hash,
                    {"row": record, "account_ids": account_ids},
                )

        tasks = await db.list_active_task_progress_seed()
        if tasks:
            pipe = self.client.pipeline(transaction=False)
            for row in tasks:
                task_kind = str(row.get("task_kind") or "native")
                task_id = str(row.get("task_id") or "")
                if not task_id:
                    continue
                pipe.set(
                    f"sub2gen:progress:{task_kind}:{task_id}",
                    json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=_json_default),
                    ex=TASK_PROGRESS_TTL_SECONDS,
                )
            await pipe.execute()
        return {"auth_records": len(auth_records), "active_tasks": len(tasks)}

    def _mark_unavailable(self, exc: Any) -> None:
        self.ready = False
        self.event_consumer_ready = False
        self._state_warmed = False
        self.last_error = str(exc or "redis_unavailable")[:300]

    async def _ensure_consumer_group(self) -> None:
        try:
            await self.client.xgroup_create(
                REDIS_STREAM_KEY,
                REDIS_CONSUMER_GROUP,
                id="0-0",
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _health_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(2.0)
                await self._connect_and_probe()
                if self.ready:
                    try:
                        info = await self.client.xinfo_groups(REDIS_STREAM_KEY)
                        for row in info or []:
                            name = _decode(row.get(b"name", row.get("name", "")))
                            if name == REDIS_CONSUMER_GROUP:
                                self._consumer_backlog = int(row.get(b"lag", row.get("lag", 0)) or 0)
                                break
                        self._last_stream_length = int(await self.client.xlen(REDIS_STREAM_KEY))
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark_unavailable(exc)

    def ensure_ready(self) -> None:
        if not self.ready:
            raise RedisUnavailableError("redis_unavailable")

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "configured": bool(self.url),
            "redis_ready": bool(self.ready),
            "state_marker_present": bool(self.state_marker_present),
            "state_version": REDIS_STATE_VERSION,
            "state_warmed": bool(self._state_warmed),
            "event_consumer_ready": bool(self.event_consumer_ready),
            "latency_ms": round(float(self.last_latency_ms), 3),
            "stream_length": int(self._last_stream_length),
            "consumer_backlog": int(self._consumer_backlog),
            "websocket_clients": int(self.websocket_clients),
            "last_ready_at": self.last_ready_at,
            "error": self.last_error or None,
            "maintenance": {
                "active": bool(self.maintenance_active),
                "reason": self.maintenance_reason or None,
                "started_at": self.maintenance_started_at,
                "owner": self.maintenance_owner or None,
            },
        }

    async def _refresh_maintenance_state(self) -> Dict[str, Any]:
        if self.client is None:
            return self.maintenance_snapshot()
        raw = await self.client.get(REDIS_MAINTENANCE_KEY)
        payload: Dict[str, Any] = {}
        if raw:
            try:
                decoded = json.loads(_decode(raw))
                payload = decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError):
                payload = {"active": True, "reason": "invalid_maintenance_marker"}
        self.maintenance_active = bool(payload.get("active"))
        self.maintenance_reason = str(payload.get("reason") or "")[:300]
        self.maintenance_started_at = str(payload.get("started_at") or "") or None
        self.maintenance_owner = str(payload.get("owner") or "")[:120]
        return self.maintenance_snapshot()

    def maintenance_snapshot(self) -> Dict[str, Any]:
        return {
            "active": bool(self.maintenance_active),
            "reason": self.maintenance_reason or None,
            "started_at": self.maintenance_started_at,
            "owner": self.maintenance_owner or None,
        }

    async def set_maintenance(
        self,
        active: bool,
        *,
        reason: str = "operator_requested",
        owner: str = "admin",
    ) -> Dict[str, Any]:
        self.ensure_ready()
        if active:
            payload = {
                "active": True,
                "reason": str(reason or "operator_requested")[:300],
                "owner": str(owner or "admin")[:120],
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            await self.client.set(
                REDIS_MAINTENANCE_KEY,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        else:
            await self.client.delete(REDIS_MAINTENANCE_KEY)
        return await self._refresh_maintenance_state()

    @staticmethod
    def auth_cache_key(key_hash: str) -> str:
        return f"sub2gen:auth:{key_hash}"

    async def get_auth_cache(self, key_hash: str) -> Optional[Dict[str, Any]]:
        if not self.ready:
            return None
        try:
            raw = await self.client.get(self.auth_cache_key(key_hash))
            return json.loads(_decode(raw)) if raw else None
        except Exception as exc:
            self._mark_unavailable(exc)
            return None

    async def set_auth_cache(self, key_hash: str, payload: Dict[str, Any]) -> None:
        if not self.ready:
            return
        try:
            await self._set_auth_cache(key_hash, payload)
        except Exception as exc:
            self._mark_unavailable(exc)

    async def _set_auth_cache(self, key_hash: str, payload: Dict[str, Any]) -> None:
        cache_key = self.auth_cache_key(key_hash)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default)
        pipe = self.client.pipeline(transaction=False)
        pipe.set(cache_key, encoded, ex=AUTH_CACHE_TTL_SECONDS)
        key_id = int(payload.get("row", {}).get("id") or 0)
        if key_id:
            index_key = f"sub2gen:auth:index:{key_id}"
            pipe.sadd(index_key, cache_key)
            pipe.expire(index_key, AUTH_CACHE_TTL_SECONDS + 5)
        await pipe.execute()

    async def invalidate_api_key(self, key_id: int) -> None:
        if not self.ready:
            return
        index_key = f"sub2gen:auth:index:{int(key_id)}"
        try:
            members = await self.client.smembers(index_key)
            keys = [_decode(value) for value in members or []]
            if keys:
                await self.client.delete(*keys)
            await self.client.delete(index_key)
            async for rate_key in self.client.scan_iter(match=f"sub2gen:rate-config:{int(key_id)}:*"):
                await self.client.delete(rate_key)
            await self.publish("auth_invalidated", {"api_key_id": int(key_id)}, persist=False)
        except Exception as exc:
            self._mark_unavailable(exc)

    async def get_rate_config(self, key_id: int, endpoint: str) -> Optional[Dict[str, Any]]:
        if not self.ready:
            return None
        endpoint_hash = hashlib.sha1(endpoint.encode("utf-8")).hexdigest()[:16]
        try:
            raw = await self.client.get(f"sub2gen:rate-config:{int(key_id)}:{endpoint_hash}")
            return json.loads(_decode(raw)) if raw else None
        except Exception as exc:
            self._mark_unavailable(exc)
            return None

    async def set_rate_config(self, key_id: int, endpoint: str, limits: Dict[str, Any]) -> None:
        if not self.ready:
            return
        endpoint_hash = hashlib.sha1(endpoint.encode("utf-8")).hexdigest()[:16]
        try:
            await self.client.set(
                f"sub2gen:rate-config:{int(key_id)}:{endpoint_hash}",
                json.dumps(limits or {}, separators=(",", ":")),
                ex=AUTH_CACHE_TTL_SECONDS,
            )
        except Exception as exc:
            self._mark_unavailable(exc)

    async def enforce_rate_limits(
        self,
        *,
        key_id: int,
        endpoint: str,
        rpm: int,
        rph: int,
        now: Optional[int] = None,
    ) -> None:
        self.ensure_ready()
        current = int(now if now is not None else time.time())
        endpoint_hash = hashlib.sha1(endpoint.encode("utf-8")).hexdigest()[:16]
        minute_window = current // 60
        hour_window = current // 3600
        minute_key = f"sub2gen:rate:{key_id}:{endpoint_hash}:m:{minute_window}"
        hour_key = f"sub2gen:rate:{key_id}:{endpoint_hash}:h:{hour_window}"
        try:
            if self._rate_script is None:
                self._rate_script = self.client.register_script(self._RATE_LIMIT_LUA)
            result = await self._rate_script(
                keys=[minute_key, hour_key],
                args=[max(0, int(rpm)), max(0, int(rph)), 65, 3665],
            )
            violation = int(result[0] or 0)
            if violation == 1:
                raise RuntimeError(f"Rate limit exceeded: {int(rpm)} requests/min for {endpoint}")
            if violation == 2:
                raise RuntimeError(f"Rate limit exceeded: {int(rph)} requests/hour for {endpoint}")
        except RuntimeError:
            raise
        except Exception as exc:
            self._mark_unavailable(exc)
            raise RedisUnavailableError("redis_unavailable") from exc

    async def touch_presence(self, key_id: int) -> None:
        self.ensure_ready()
        try:
            await self.client.set(
                f"sub2gen:presence:{int(key_id)}",
                datetime.now(timezone.utc).isoformat(),
                ex=PRESENCE_TTL_SECONDS,
            )
            await self.publish("presence", {"api_key_id": int(key_id)}, persist=False)
        except RedisUnavailableError:
            raise
        except Exception as exc:
            self._mark_unavailable(exc)
            raise RedisUnavailableError("redis_unavailable") from exc

    @staticmethod
    def _inflight_key(kind: str, token_id: int) -> str:
        normalized = "video" if kind == "video" else "image"
        return f"sub2gen:inflight:{normalized}:{int(token_id)}"

    async def initialize_inflight(self, token_ids: Iterable[int]) -> None:
        if not self.ready:
            return
        try:
            pipe = self.client.pipeline(transaction=False)
            for token_id in token_ids:
                pipe.set(self._inflight_key("image", int(token_id)), 0, ex=6 * 3600)
                pipe.set(self._inflight_key("video", int(token_id)), 0, ex=6 * 3600)
            await pipe.execute()
        except Exception as exc:
            self._mark_unavailable(exc)

    async def acquire_inflight(self, kind: str, token_id: int, limit: Optional[int]) -> tuple[bool, int]:
        self.ensure_ready()
        try:
            if self._acquire_slot_script is None:
                self._acquire_slot_script = self.client.register_script(self._ACQUIRE_SLOT_LUA)
            result = await self._acquire_slot_script(
                keys=[self._inflight_key(kind, token_id)],
                args=[int(limit or -1), 6 * 3600],
            )
            return bool(int(result[0] or 0)), int(result[1] or 0)
        except Exception as exc:
            self._mark_unavailable(exc)
            raise RedisUnavailableError("redis_unavailable") from exc

    async def release_inflight(self, kind: str, token_id: int) -> int:
        if not self.ready:
            return 0
        try:
            if self._release_slot_script is None:
                self._release_slot_script = self.client.register_script(self._RELEASE_SLOT_LUA)
            return int(await self._release_slot_script(
                keys=[self._inflight_key(kind, token_id)],
                args=[6 * 3600],
            ))
        except Exception as exc:
            self._mark_unavailable(exc)
            return 0

    async def get_inflight(self, kind: str, token_id: int) -> Optional[int]:
        if not self.ready:
            return None
        try:
            raw = await self.client.get(self._inflight_key(kind, token_id))
            return int(raw or 0)
        except Exception as exc:
            self._mark_unavailable(exc)
            return None

    async def is_present(self, key_id: int) -> bool:
        if not self.ready:
            return False
        try:
            return bool(await self.client.exists(f"sub2gen:presence:{int(key_id)}"))
        except Exception as exc:
            self._mark_unavailable(exc)
            return False

    async def publish_task_progress(
        self,
        task_kind: str,
        task_id: str,
        payload: Dict[str, Any],
        *,
        terminal: bool = False,
    ) -> Optional[str]:
        if not self.ready:
            return None
        key = f"sub2gen:progress:{task_kind}:{task_id}"
        try:
            body = {**payload, "task_kind": task_kind, "task_id": task_id}
            await self.client.set(
                key,
                json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=_json_default),
                ex=TASK_PROGRESS_TTL_SECONDS,
            )
            return await self.publish("task_progress", body, persist=False)
        except Exception as exc:
            self._mark_unavailable(exc)
            return None

    async def queue_usage_touch(self, key_id: int) -> None:
        if not self.ready:
            if self.required:
                raise RedisUnavailableError("redis_unavailable")
            return
        debounce_key = f"sub2gen:usage:debounce:{int(key_id)}"
        try:
            acquired = await self.client.set(debounce_key, "1", ex=60, nx=True)
            if acquired:
                await self.publish("usage_touch", {"api_key_id": int(key_id)}, persist=self.required)
        except Exception as exc:
            self._mark_unavailable(exc)
            if self.required:
                raise RedisUnavailableError("redis_unavailable") from exc

    async def queue_audit(self, payload: Dict[str, Any]) -> None:
        if not self.ready:
            if self.required:
                raise RedisUnavailableError("redis_unavailable")
            return
        await self.publish("api_key_audit", payload, persist=self.required)

    async def publish(
        self,
        event_type: str,
        data: Dict[str, Any],
        *,
        persist: bool = False,
    ) -> Optional[str]:
        if not self.ready:
            if self.required and persist:
                raise RedisUnavailableError("redis_unavailable")
            return None
        created_at = datetime.now(timezone.utc).isoformat()
        fields = {
            "type": str(event_type),
            "data": json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=_json_default),
            "created_at": created_at,
            "persist": "1" if persist else "0",
        }
        try:
            cursor = await self.client.xadd(
                REDIS_STREAM_KEY,
                fields,
                maxlen=REDIS_STREAM_MAXLEN,
                approximate=True,
            )
            return _decode(cursor)
        except Exception as exc:
            self._mark_unavailable(exc)
            if self.required and persist:
                raise RedisUnavailableError("redis_unavailable") from exc
            return None

    @staticmethod
    def _parse_event(cursor: Any, raw_fields: Dict[Any, Any]) -> RedisEvent:
        fields = {_decode(key): _decode(value) for key, value in raw_fields.items()}
        try:
            data = json.loads(fields.get("data") or "{}")
        except Exception:
            data = {"raw": fields.get("data") or ""}
        if not isinstance(data, dict):
            data = {"value": data}
        return RedisEvent(
            cursor=_decode(cursor),
            event_type=fields.get("type") or "unknown",
            data=data,
            created_at=fields.get("created_at") or datetime.now(timezone.utc).isoformat(),
        )

    async def read_events(
        self,
        cursor: str = "$",
        *,
        block_ms: int = 15_000,
        count: int = 100,
    ) -> list[RedisEvent]:
        self.ensure_ready()
        try:
            response = await self.client.xread(
                {REDIS_STREAM_KEY: cursor or "$"},
                count=max(1, min(int(count), 500)),
                block=max(0, int(block_ms)),
            )
            events: list[RedisEvent] = []
            for _stream, entries in response or []:
                for event_id, fields in entries:
                    events.append(self._parse_event(event_id, fields))
            return events
        except Exception as exc:
            self._mark_unavailable(exc)
            raise RedisUnavailableError("redis_unavailable") from exc

    async def cursor_was_trimmed(self, cursor: str) -> bool:
        if not self.ready or not cursor or cursor == "$":
            return False
        try:
            rows = await self.client.xrange(REDIS_STREAM_KEY, min="-", max="+", count=1)
            if not rows:
                return False
            oldest = _decode(rows[0][0])
            requested_parts = tuple(int(part) for part in cursor.split("-", 1))
            oldest_parts = tuple(int(part) for part in oldest.split("-", 1))
            return requested_parts < oldest_parts
        except Exception:
            return True

    async def iter_events(self, cursor: str = "$") -> AsyncIterator[RedisEvent]:
        current = cursor or "$"
        while self.ready and not self._stopping:
            events = await self.read_events(current, block_ms=15_000)
            for event in events:
                current = event.cursor
                yield event

    async def _consumer_loop(self) -> None:
        while not self._stopping:
            if not self.ready or self.db is None:
                self.event_consumer_ready = False
                await asyncio.sleep(0.25)
                continue
            if self.maintenance_active:
                self.event_consumer_ready = True
                await asyncio.sleep(0.25)
                continue
            try:
                await self._ensure_consumer_group()
                self.event_consumer_ready = True
                entries: list[tuple[Any, Dict[Any, Any]]] = []
                now = time.monotonic()
                if now - self._last_claim_at >= 5.0:
                    self._last_claim_at = now
                    claimed = await self.client.xautoclaim(
                        REDIS_STREAM_KEY,
                        REDIS_CONSUMER_GROUP,
                        self._consumer_name,
                        min_idle_time=5_000,
                        start_id="0-0",
                        count=100,
                    )
                    if isinstance(claimed, (list, tuple)) and len(claimed) >= 2:
                        entries.extend(claimed[1] or [])
                if not entries:
                    response = await self.client.xreadgroup(
                        REDIS_CONSUMER_GROUP,
                        self._consumer_name,
                        {REDIS_STREAM_KEY: ">"},
                        count=100,
                        block=250,
                    )
                    for _stream, batch in response or []:
                        entries.extend(batch)
                if not entries:
                    continue
                persistable: list[Dict[str, Any]] = []
                ack_ids: list[Any] = []
                for event_id, fields in entries:
                    decoded = {_decode(key): _decode(value) for key, value in fields.items()}
                    if decoded.get("persist") == "1":
                        event = self._parse_event(event_id, fields)
                        persistable.append(event.as_dict())
                    ack_ids.append(event_id)
                if persistable:
                    await self.db.apply_redis_event_batch(persistable)
                if ack_ids:
                    await self.client.xack(REDIS_STREAM_KEY, REDIS_CONSUMER_GROUP, *ack_ids)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark_unavailable(exc)
                await asyncio.sleep(0.5)


redis_runtime = RedisRuntime()


def is_new_protected_work(method: str, path: str) -> bool:
    """Return whether an authenticated request creates new upstream work."""
    if str(method or "").upper() != "POST":
        return False
    normalized = str(path or "").rstrip("/") or "/"
    if normalized in {"/api/client/presence"} or normalized.endswith("/cancel"):
        return False
    if normalized in {
        "/v1/chat/completions",
        "/v1/async/chat/completions",
        "/v1/runway/tasks",
        "/v1/runway/uploads",
        "/api/generate-cloning-prompts",
        "/api/generate-cloning-video-prompt",
        "/api/generate-metadata",
        "/api/tracker/contributor",
        "/api/tracker/keyword",
        "/api/extension/generation-upload",
    }:
        return True
    return normalized.endswith(":generateContent") or normalized.endswith(":streamGenerateContent")
