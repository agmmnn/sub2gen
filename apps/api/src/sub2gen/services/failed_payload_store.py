"""Asynchronous failed-request payload storage for DigitalOcean Spaces."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..core.logger import redact_text_for_log, sanitize_data_for_log


SUMMARY_EXCERPT_CHARS = 1024
FAILED_PAYLOAD_RETENTION_DAYS = 7


def _text_size(value: Any) -> int:
    return len(str(value or "").encode("utf-8", errors="replace"))


def summarize_payload(value: Any, limit: int = SUMMARY_EXCERPT_CHARS) -> str:
    """Return a redacted, single-field bounded excerpt suitable for SQLite."""
    if value is None:
        return ""
    raw = str(value)
    try:
        parsed = json.loads(raw)
        safe = sanitize_data_for_log(parsed)
        text = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = redact_text_for_log(raw)
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 24)]}… [truncated {len(text)}]"


def _redact_payload_document(request_body: str, response_body: str) -> bytes:
    def safe_value(raw: str) -> Any:
        try:
            return sanitize_data_for_log(json.loads(raw))
        except Exception:
            return redact_text_for_log(raw)

    document = {
        "request_body": safe_value(request_body),
        "response_body": safe_value(response_body),
    }
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(encoded, compresslevel=6)


@dataclass
class _PendingPayload:
    request_body: str
    response_body: str
    size_bytes: int
    updated_at: float


@dataclass
class _PayloadJob:
    log_id: int
    request_body: str
    response_body: str


class FailedPayloadManager:
    """Keeps in-flight payloads bounded and uploads only terminal failures."""

    def __init__(self):
        self.db: Any = None
        self.enabled = False
        self.configured = False
        self.last_error = ""
        self._client: Any = None
        self._bucket = str(os.environ.get("SUB2GEN_DO_SPACES_BUCKET", "") or "").strip()
        self._prefix = str(os.environ.get("SUB2GEN_FAILED_LOG_PREFIX", "sub2gen/logs") or "sub2gen/logs").strip("/")
        self._pending: OrderedDict[int, _PendingPayload] = OrderedDict()
        self._pending_bytes = 0
        self._max_pending_bytes = max(
            1024 * 1024,
            int(os.environ.get("SUB2GEN_FAILED_LOG_PENDING_MAX_BYTES", str(64 * 1024 * 1024)) or 0),
        )
        self._queue: asyncio.Queue[_PayloadJob] = asyncio.Queue(
            maxsize=max(1, int(os.environ.get("SUB2GEN_FAILED_LOG_QUEUE_SIZE", "100") or 100))
        )
        self._worker: Optional[asyncio.Task] = None
        self.stored_total = 0
        self.failed_total = 0
        self.dropped_total = 0
        self.payload_bytes_total = 0

    def _build_client(self) -> Any:
        access_key = str(os.environ.get("SUB2GEN_DO_SPACES_ACCESS_KEY_ID", "") or "").strip()
        secret = str(os.environ.get("SUB2GEN_DO_SPACES_SECRET_ACCESS_KEY", "") or "").strip()
        region = str(os.environ.get("SUB2GEN_DO_SPACES_REGION", "") or "").strip()
        self.configured = bool(access_key and secret and region and self._bucket)
        if not self.configured:
            return None
        import boto3

        return boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://{region}.digitaloceanspaces.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret,
        )

    async def start(self, db: Any, *, enabled: bool) -> None:
        self.db = db
        self.enabled = bool(enabled)
        if self.enabled:
            try:
                self._client = self._build_client()
                if self._client is None:
                    self.last_error = "digitalocean_spaces_not_configured"
            except Exception as exc:
                self.last_error = str(exc)[:300]
                self._client = None
        self._worker = asyncio.create_task(self._worker_loop(), name="sub2gen-failed-payloads")

    async def stop(self) -> None:
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=10.0)
        except asyncio.TimeoutError:
            pass
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "queue_depth": self._queue.qsize(),
            "pending_payloads": len(self._pending),
            "pending_bytes": self._pending_bytes,
            "stored_total": self.stored_total,
            "failed_total": self.failed_total,
            "dropped_total": self.dropped_total,
            "payload_bytes_total": self.payload_bytes_total,
            "error": self.last_error or None,
        }

    def _remember(self, log_id: int, request_body: str, response_body: str) -> None:
        old = self._pending.pop(log_id, None)
        if old:
            self._pending_bytes -= old.size_bytes
        size = _text_size(request_body) + _text_size(response_body)
        self._pending[log_id] = _PendingPayload(request_body, response_body, size, time.monotonic())
        self._pending_bytes += size
        while self._pending and self._pending_bytes > self._max_pending_bytes:
            _evicted_id, evicted = self._pending.popitem(last=False)
            self._pending_bytes -= evicted.size_bytes
            self.dropped_total += 1

    def _forget(self, log_id: int) -> Optional[_PendingPayload]:
        item = self._pending.pop(log_id, None)
        if item:
            self._pending_bytes -= item.size_bytes
        return item

    def capture_initial(
        self,
        log_id: int,
        *,
        request_body: Any,
        response_body: Any,
        status_code: int,
        status_text: str,
    ) -> Dict[str, Any]:
        request_text = str(request_body or "")
        response_text = str(response_body or "")
        fields = self.summary_fields(request_text, response_text)
        terminal, failed = self._terminal_state(status_code, status_text)
        if terminal:
            if failed:
                fields.update(self._enqueue(log_id, request_text, response_text))
        else:
            self._remember(log_id, request_text, response_text)
        return fields

    def summary_fields(self, request_body: Any, response_body: Any) -> Dict[str, Any]:
        request_text = str(request_body or "")
        response_text = str(response_body or "")
        return {
            "request_body": summarize_payload(request_text),
            "response_body": summarize_payload(response_text),
            "request_excerpt": summarize_payload(request_text),
            "response_excerpt": summarize_payload(response_text),
            "request_size_bytes": _text_size(request_text),
            "response_size_bytes": _text_size(response_text),
        }

    def capture_update(self, log_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        pending = self._pending.get(log_id)
        request_text = str(fields.get("request_body") if "request_body" in fields else (pending.request_body if pending else "") or "")
        response_text = str(fields.get("response_body") if "response_body" in fields else (pending.response_body if pending else "") or "")
        if "request_body" in fields:
            fields["request_body"] = summarize_payload(request_text)
            fields["request_excerpt"] = fields["request_body"]
            fields["request_size_bytes"] = _text_size(request_text)
        if "response_body" in fields:
            fields["response_body"] = summarize_payload(response_text)
            fields["response_excerpt"] = fields["response_body"]
            fields["response_size_bytes"] = _text_size(response_text)

        terminal, failed = self._terminal_state(
            int(fields.get("status_code") or 0),
            str(fields.get("status_text") or ""),
        )
        if terminal:
            self._forget(log_id)
            if failed:
                fields.update(self._enqueue(log_id, request_text, response_text))
        elif pending is not None or request_text or response_text:
            self._remember(log_id, request_text, response_text)
        return fields

    @staticmethod
    def _terminal_state(status_code: int, status_text: str) -> tuple[bool, bool]:
        normalized = str(status_text or "").strip().lower()
        failed = int(status_code or 0) >= 400 or normalized in {
            "failed", "cancelled", "canceled", "error", "timeout"
        }
        completed = int(status_code or 0) >= 200 or normalized == "completed"
        return failed or completed, failed

    def _enqueue(self, log_id: int, request_body: str, response_body: str) -> Dict[str, Any]:
        if not self.enabled or self._client is None:
            self.failed_total += 1
            return {"payload_available": 0, "payload_storage_error": self.last_error or "payload_storage_unavailable"}
        try:
            self._queue.put_nowait(_PayloadJob(log_id, request_body, response_body))
            return {"payload_available": 0, "payload_storage_error": None}
        except asyncio.QueueFull:
            self.dropped_total += 1
            return {"payload_available": 0, "payload_storage_error": "payload_queue_full"}

    async def _worker_loop(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._store_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failed_total += 1
                self.last_error = str(exc)[:300]
                if self.db is not None:
                    await self.db.set_request_log_payload_metadata(
                        job.log_id,
                        payload_available=False,
                        payload_object_key=None,
                        payload_storage_error=self.last_error,
                    )
            finally:
                self._queue.task_done()

    async def _store_job(self, job: _PayloadJob) -> None:
        compressed = await asyncio.to_thread(
            _redact_payload_document,
            job.request_body,
            job.response_body,
        )
        now = datetime.now(timezone.utc)
        key = (
            f"{self._prefix}/{now:%Y/%m/%d}/"
            f"{int(job.log_id)}-{uuid.uuid4().hex}.json.gz"
        )
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=compressed,
            ContentType="application/json",
            ContentEncoding="gzip",
            ACL="private",
        )
        self.stored_total += 1
        self.payload_bytes_total += len(compressed)
        if self.db is not None:
            await self.db.set_request_log_payload_metadata(
                job.log_id,
                payload_available=True,
                payload_object_key=key,
                payload_storage_error=None,
            )

    async def load(self, object_key: str) -> Optional[Dict[str, Any]]:
        key = str(object_key or "").strip()
        if not key or self._client is None:
            return None
        response = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket,
            Key=key,
        )
        body = response["Body"]
        try:
            compressed = await asyncio.to_thread(body.read)
        finally:
            await asyncio.to_thread(body.close)
        decoded = await asyncio.to_thread(gzip.decompress, compressed)
        payload = json.loads(decoded.decode("utf-8"))
        return payload if isinstance(payload, dict) else None

    async def delete(self, object_key: str) -> None:
        if self._client is None or not object_key:
            return
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=str(object_key),
        )

    async def cleanup_expired(self, days: int = FAILED_PAYLOAD_RETENTION_DAYS) -> int:
        if self._client is None:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        continuation: Optional[str] = None
        expired: list[str] = []
        while True:
            kwargs: Dict[str, Any] = {"Bucket": self._bucket, "Prefix": f"{self._prefix}/"}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            page = await asyncio.to_thread(self._client.list_objects_v2, **kwargs)
            for row in page.get("Contents", []):
                modified = row.get("LastModified")
                if modified and modified < cutoff:
                    expired.append(str(row.get("Key") or ""))
            if not page.get("IsTruncated"):
                break
            continuation = page.get("NextContinuationToken")
        for offset in range(0, len(expired), 500):
            batch = [key for key in expired[offset:offset + 500] if key]
            if batch:
                await asyncio.to_thread(
                    self._client.delete_objects,
                    Bucket=self._bucket,
                    Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
                )
        return len(expired)


failed_payload_manager = FailedPayloadManager()
