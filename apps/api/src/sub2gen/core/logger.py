"""Debug logger module for detailed API request/response logging"""
import json
import logging
import logging.handlers
import os
import queue
import re
import sys
import atexit
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from .config import config


_SENSITIVE_KEYS = {
    "authorization",
    "proxy_authorization",
    "cookie",
    "cookies",
    "set_cookie",
    "api_key",
    "apikey",
    "key_plaintext",
    "client_key",
    "worker_key",
    "captcha_worker_key",
    "private_key",
    "x_api_key",
    "x_goog_api_key",
    "password",
    "client_secret",
    "secret",
    "secret_key",
    "access_token",
    "refresh_token",
    "session_token",
    "id_token",
    "connection_token",
    "google_cookies",
    "st",
    "at",
    "token",
}


def _normalize_sensitive_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key or "").strip().lower()).strip("_")


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalize_sensitive_key(key)
    if normalized in _SENSITIVE_KEYS:
        return True
    if normalized in {"token_id", "tokens", "token_count", "password_required"}:
        return False
    return normalized.endswith(
        ("_token", "_secret", "_password", "_api_key", "_auth_key", "_worker_key", "_cookies")
    )


def redact_url_for_log(value: Any) -> str:
    text = str(value or "")
    try:
        parts = urlsplit(text)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if not query:
            return text
        safe_query = [
            (key, "<redacted>" if _is_sensitive_key(key) or key.strip().lower() == "key" else item)
            for key, item in query
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), parts.fragment))
    except Exception:
        return re.sub(
            r"(?i)([?&](?:key|token|access_token|session_token|api_key)=)[^&#\s]+",
            r"\1<redacted>",
            text,
        )


def redact_text_for_log(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[^\s,;\"']+", r"\1<redacted>", text)
    text = re.sub(
        r"(?i)((?:__Secure-next-auth\.session-token|session_token|access_token|refresh_token|api_key|password)=)[^;\s,]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r'''(?ix)(["'](?:access_token|refresh_token|session_token|api_key|password|client_secret|google_cookies)["']\s*:\s*["'])[^"']+''',
        r"\1<redacted>",
        text,
    )
    return redact_url_for_log(text)


def sanitize_headers_for_log(headers: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    sanitized: Dict[str, Any] = {}
    for key, value in dict(headers or {}).items():
        sanitized[str(key)] = "<redacted>" if _is_sensitive_key(key) else redact_text_for_log(value)
    return sanitized


def sanitize_data_for_log(data: Any, *, field_name: Any = None) -> Any:
    if field_name is not None and _is_sensitive_key(field_name):
        return "<redacted>"
    if isinstance(data, dict):
        return {str(key): sanitize_data_for_log(value, field_name=key) for key, value in data.items()}
    if isinstance(data, list):
        return [sanitize_data_for_log(item) for item in data]
    if isinstance(data, tuple):
        return tuple(sanitize_data_for_log(item) for item in data)
    if isinstance(data, str):
        return redact_text_for_log(data)
    return data


class SensitiveAccessLogFilter(logging.Filter):
    """Redact sensitive values before Uvicorn formats HTTP or WebSocket logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(
                redact_text_for_log(value) if isinstance(value, str) else value
                for value in args
            )
        elif isinstance(args, dict):
            record.args = sanitize_data_for_log(args)
        return True


class _DroppingQueueHandler(logging.handlers.QueueHandler):
    """Bounded non-blocking QueueHandler with an observable drop counter."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__(log_queue)
        self.dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

class DebugLogger:
    """Debug logger for API requests and responses"""

    def __init__(self):
        self.log_file = Path("logs.txt")
        default_payload_logging = not bool(os.environ.get("RAILWAY_ENVIRONMENT"))
        raw_payload_logging = str(os.environ.get("SUB2GEN_DEBUG_PAYLOAD_LOGGING", "") or "").strip().lower()
        self.payload_logging_enabled = (
            raw_payload_logging in {"1", "true", "yes", "on"}
            if raw_payload_logging
            else default_payload_logging
        )
        self._queue_handler: Optional[_DroppingQueueHandler] = None
        self._queue_listener: Optional[logging.handlers.QueueListener] = None
        self._setup_logger()

    def _setup_logger(self):
        """Setup file logger"""
        # Create logger
        self.logger = logging.getLogger("debug_logger")
        self.logger.setLevel(logging.DEBUG)

        # Remove existing handlers
        self.logger.handlers.clear()

        # Create file handler
        file_handler = logging.handlers.RotatingFileHandler(
            self.log_file,
            mode='a',
            encoding='utf-8',
            maxBytes=25 * 1024 * 1024,
            backupCount=2,
        )
        file_handler.setLevel(logging.DEBUG)

        # Create stdout handler (for docker logs / console)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.DEBUG)

        # Create formatter
        formatter = logging.Formatter(
            '%(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        stream_handler.setFormatter(formatter)

        synchronous = str(os.environ.get("SUB2GEN_SYNC_DEBUG_LOGGING", "0") or "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if synchronous:
            self.logger.addHandler(file_handler)
            self.logger.addHandler(stream_handler)
        else:
            log_queue: queue.Queue = queue.Queue(
                maxsize=max(100, int(os.environ.get("SUB2GEN_LOG_QUEUE_SIZE", "10000") or 10000))
            )
            self._queue_handler = _DroppingQueueHandler(log_queue)
            self.logger.addHandler(self._queue_handler)
            self._queue_listener = logging.handlers.QueueListener(
                log_queue,
                file_handler,
                stream_handler,
                respect_handler_level=True,
            )
            self._queue_listener.start()
            atexit.register(self.close)

        # Prevent propagation to root logger
        self.logger.propagate = False

    def close(self) -> None:
        listener, self._queue_listener = self._queue_listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    def queue_status(self) -> Dict[str, int]:
        if self._queue_handler is None:
            return {"depth": 0, "capacity": 0, "dropped": 0}
        return {
            "depth": int(self._queue_handler.queue.qsize()),
            "capacity": int(self._queue_handler.queue.maxsize),
            "dropped": int(self._queue_handler.dropped),
        }

    def _mask_token(self, token: str) -> str:
        """Mask token for logging (show first 6 and last 6 characters)"""
        if not config.debug_mask_token or len(token) <= 12:
            return token
        return f"{token[:6]}...{token[-6:]}"

    def _format_timestamp(self) -> str:
        """Format current timestamp"""
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    def _write_separator(self, char: str = "=", length: int = 100):
        """Write separator line"""
        self.logger.info(char * length)

    def _truncate_large_fields(self, data: Any, max_length: int = 200) -> Any:
        """对大字段进行截断处理，特别是 base64 编码的图片数据
        
        Args:
            data: 要处理的数据
            max_length: 字符串字段的最大长度
        
        Returns:
            截断后的数据副本
        """
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                if _is_sensitive_key(key):
                    result[key] = "<redacted>"
                    continue
                # 对特定的大字段进行截断
                if key in ("encodedImage", "base64", "imageData", "data") and isinstance(value, str) and len(value) > max_length:
                    result[key] = f"{value[:100]}... (truncated, total {len(value)} chars)"
                else:
                    result[key] = self._truncate_large_fields(value, max_length)
            return result
        elif isinstance(data, list):
            return [self._truncate_large_fields(item, max_length) for item in data]
        elif isinstance(data, str) and len(data) > 10000:
            # 对超长字符串进行截断（可能是未知的 base64 字段）
            return f"{data[:100]}... (truncated, total {len(data)} chars)"
        return data

    def log_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[Any] = None,
        files: Optional[Dict] = None,
        proxy: Optional[str] = None
    ):
        """Log API request details to log.txt"""

        if not self.payload_logging_enabled or not config.debug_enabled or not config.debug_log_requests:
            return

        try:
            self._write_separator()
            self.logger.info(f"[REQUEST] {self._format_timestamp()}")
            self._write_separator("-")

            # Basic info
            self.logger.info(f"Method: {method}")
            self.logger.info(f"URL: {redact_url_for_log(url)}")

            # Headers
            self.logger.info("\nHeaders:")
            masked_headers = sanitize_headers_for_log(headers)

            for key, value in masked_headers.items():
                self.logger.info(f"  {key}: {value}")

            # Body
            if body is not None:
                self.logger.info("\nRequest Body:")
                safe_body = sanitize_data_for_log(body)
                if isinstance(safe_body, (dict, list)):
                    body_str = json.dumps(safe_body, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                else:
                    self.logger.info(str(safe_body))

            # Files
            if files:
                self.logger.info("\nFiles:")
                try:
                    if hasattr(files, 'keys') and callable(getattr(files, 'keys', None)):
                        for key in files.keys():
                            self.logger.info(f"  {key}: <file data>")
                    else:
                        self.logger.info("  <multipart form data>")
                except (AttributeError, TypeError):
                    self.logger.info("  <binary file data>")

            # Proxy
            if proxy:
                self.logger.info(f"\nProxy: {redact_url_for_log(proxy)}")

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging request: {e}")

    def log_response(
        self,
        status_code: int,
        headers: Dict[str, str],
        body: Any,
        duration_ms: Optional[float] = None
    ):
        """Log API response details to log.txt"""

        if not self.payload_logging_enabled or not config.debug_enabled or not config.debug_log_responses:
            return

        try:
            self._write_separator()
            self.logger.info(f"[RESPONSE] {self._format_timestamp()}")
            self._write_separator("-")

            # Status
            status_label = "OK" if 200 <= status_code < 300 else "ERROR"
            self.logger.info(f"Status: {status_code} {status_label}")

            # Duration
            if duration_ms is not None:
                self.logger.info(f"Duration: {duration_ms:.2f}ms")

            # Headers
            self.logger.info("\nResponse Headers:")
            for key, value in sanitize_headers_for_log(headers).items():
                self.logger.info(f"  {key}: {value}")

            # Body
            self.logger.info("\nResponse Body:")
            safe_body = sanitize_data_for_log(body)
            if isinstance(safe_body, (dict, list)):
                # 对大字段进行截断处理
                body_to_log = self._truncate_large_fields(safe_body)
                body_str = json.dumps(body_to_log, indent=2, ensure_ascii=False)
                self.logger.info(body_str)
            elif isinstance(safe_body, str):
                # Try to parse as JSON
                try:
                    parsed = sanitize_data_for_log(json.loads(safe_body))
                    # 对大字段进行截断处理
                    parsed = self._truncate_large_fields(parsed)
                    body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                except:
                    # Not JSON, log as text (limit length)
                    if len(safe_body) > 2000:
                        self.logger.info(f"{safe_body[:2000]}... (truncated)")
                    else:
                        self.logger.info(safe_body)
            else:
                self.logger.info(str(safe_body))

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging response: {e}")

    def log_error(
        self,
        error_message: str,
        status_code: Optional[int] = None,
        response_text: Optional[str] = None
    ):
        """Log API error details to log.txt"""

        if not config.debug_enabled:
            return

        try:
            self._write_separator()
            self.logger.info(f"[ERROR] {self._format_timestamp()}")
            self._write_separator("-")

            if status_code:
                self.logger.info(f"Status Code: {status_code}")

            self.logger.info(f"Error Message: {redact_text_for_log(error_message)}")

            if response_text:
                self.logger.info("\nError Response:")
                # Try to parse as JSON
                try:
                    parsed = sanitize_data_for_log(json.loads(response_text))
                    body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                except:
                    # Not JSON, log as text
                    safe_response_text = redact_text_for_log(response_text)
                    if len(safe_response_text) > 2000:
                        self.logger.info(f"{safe_response_text[:2000]}... (truncated)")
                    else:
                        self.logger.info(safe_response_text)

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging error: {e}")

    def log_info(self, message: str):
        """Log general info message to log.txt"""
        if not config.debug_enabled:
            return
        try:
            self.logger.info(f"INFO [{self._format_timestamp()}] {redact_text_for_log(message)}")
        except Exception as e:
            self.logger.error(f"Error logging info: {e}")

    def log_warning(self, message: str):
        """Log warning message to log.txt"""
        if not config.debug_enabled:
            return
        try:
            self.logger.warning(f"WARN [{self._format_timestamp()}] {redact_text_for_log(message)}")
        except Exception as e:
            self.logger.error(f"Error logging warning: {e}")

    def _should_log_recaptcha(self) -> bool:
        return bool(config.debug_enabled or config.debug_recaptcha_trace)

    def should_log_recaptcha(self) -> bool:
        """True when reCAPTCHA narrative or trace logging is active."""
        return self._should_log_recaptcha()

    def _recaptcha_narrative_line(self, line: str, level: str = "info") -> None:
        """Single-line reCAPTCHA log (no banner). Writes to logs.txt and optional console."""
        if not self._should_log_recaptcha():
            return
        try:
            line = redact_text_for_log(line)
            if level == "warning":
                self.logger.warning(line)
            elif level == "error":
                self.logger.error(line)
            else:
                self.logger.info(line)
            if config.debug_recaptcha_console:
                print(line, flush=True)
        except Exception as e:
            self.logger.error(f"Error logging reCAPTCHA narrative: {e}")

    def log_recaptcha_state_reset(self) -> None:
        self._recaptcha_narrative_line("[DEBUG] reCAPTCHA state reset (lastAction cleared)")

    def log_recaptcha_action_switch(self, old_action: str, new_action: str) -> None:
        self._recaptcha_narrative_line(
            f"[DEBUG] reCAPTCHA action switch: {old_action} -> {new_action}, resetting"
        )

    def log_recaptcha_proxy_check(self, line: str) -> None:
        """Headed-browser proxy resolution; intended to run before action/generating narrative lines."""
        self._recaptcha_narrative_line(line)

    def log_recaptcha_request_action(self, action: str) -> None:
        self._recaptcha_narrative_line(f"[DEBUG] reCAPTCHA request action: {action}")

    def log_recaptcha_generating(self, action: str) -> None:
        """Plain line (no [DEBUG] prefix), matching browser-style UX logs."""
        self._recaptcha_narrative_line(f"Generating reCAPTCHA token with action: {action}")

    def log_recaptcha_token_success(self, token: Optional[str]) -> None:
        if not token:
            return
        self._recaptcha_narrative_line(f"Token obtained: <redacted> (length={len(token)})")
        self._recaptcha_narrative_line(f"[DEBUG] reCAPTCHA token length: {len(token)}")
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        self._recaptcha_narrative_line(f"[DEBUG] reCAPTCHA token obtained at: {iso}")

    def log_recaptcha_browser_error(self, message: str, raw_result: Optional[Any] = None) -> None:
        self._recaptcha_narrative_line(f"reCAPTCHA error: {message}", level="error")
        if raw_result is not None:
            if isinstance(raw_result, str):
                raw = redact_text_for_log(raw_result)
            else:
                try:
                    raw = json.dumps(sanitize_data_for_log(raw_result), ensure_ascii=False)
                except Exception:
                    raw = redact_text_for_log(raw_result)
            if len(raw) > 2000:
                raw = raw[:2000] + "... (truncated)"
            self._recaptcha_narrative_line(f"[DEBUG] reCAPTCHA raw result: {raw}")

    def log_recaptcha_execution_error(self, message: str) -> None:
        self._recaptcha_narrative_line(f"reCAPTCHA execution error: {message}", level="error")

    @staticmethod
    def format_recaptcha_token_meta(token: Optional[str]) -> str:
        """Safe one-line description of a reCAPTCHA token (never log full JWT)."""
        if not token:
            return "none"
        n = len(token)
        if config.debug_mask_token and n > 12:
            return f"len={n} preview={token[:6]}...{token[-6:]}"
        return f"len={n}"

# Global debug logger instance
debug_logger = DebugLogger()
