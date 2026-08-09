"""File caching service"""
import os
import asyncio
import hashlib
import time
import mimetypes
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse
import httpx
from curl_cffi.requests import AsyncSession
from ..core.config import config
from ..core.logger import debug_logger
from ..persistence.repositories import CacheRepository
from .cache_backends import (
    DigitalOceanSpacesBackend,
    DigitalOceanSpacesSettings,
    LocalCacheBackend,
)

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".jpe"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv", ".m4v"})
MIN_VALID_VIDEO_BYTES = 1024
MIN_FREE_SPACE_BYTES = 256 * 1024 * 1024
MAX_FREE_SPACE_BYTES = 1024 * 1024 * 1024
FREE_SPACE_RATIO = 0.10
STALE_PART_SECONDS = 300


class FileCache:
    """File caching service for videos"""

    def __init__(
        self,
        cache_dir: str = "tmp",
        default_timeout: int = 7200,
        proxy_manager=None,
        flow_client=None,
        db=None,
        cache_repository: CacheRepository | None = None,
    ):
        """
        Initialize file cache

        Args:
            cache_dir: Cache directory path
            default_timeout: Default cache timeout in seconds (default: 2 hours)
            proxy_manager: ProxyManager instance for downloading files
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.default_timeout = max(0, int(default_timeout))
        self.proxy_manager = proxy_manager
        self.flow_client = flow_client
        self.db = db
        self.cache_repository = cache_repository or (
            CacheRepository(db) if db is not None else None
        )
        self._cleanup_task = None
        self._cleanup_lock = asyncio.Lock()
        self._download_locks: Dict[str, asyncio.Lock] = {}
        self.provider = "local"
        self.delivery_mode = "proxy"
        self.backend = LocalCacheBackend(self.cache_dir)

    @staticmethod
    def digitalocean_environment(delivery_mode: str = "proxy") -> Dict[str, Any]:
        values = {
            "access_key_id": (os.environ.get("SUB2GEN_DO_SPACES_ACCESS_KEY_ID") or "").strip(),
            "secret_access_key": (os.environ.get("SUB2GEN_DO_SPACES_SECRET_ACCESS_KEY") or "").strip(),
            "region": (os.environ.get("SUB2GEN_DO_SPACES_REGION") or "").strip(),
            "bucket": (os.environ.get("SUB2GEN_DO_SPACES_BUCKET") or "").strip(),
            "prefix": (os.environ.get("SUB2GEN_DO_SPACES_PREFIX") or "sub2gen/cache").strip("/"),
            "cdn_base_url": (os.environ.get("SUB2GEN_DO_SPACES_CDN_BASE_URL") or "").strip().rstrip("/"),
            "api_token": (os.environ.get("SUB2GEN_DO_API_TOKEN") or "").strip(),
            "cdn_endpoint_id": (os.environ.get("SUB2GEN_DO_CDN_ENDPOINT_ID") or "").strip(),
        }
        settings = DigitalOceanSpacesSettings(delivery_mode=delivery_mode, **values)
        return {
            "settings": settings,
            "configured": not settings.missing(),
            "missing": settings.missing(),
            "region": settings.region,
            "bucket": settings.bucket,
            "prefix": settings.prefix,
            "cdn_base_url": settings.cdn_base_url,
            "access_key_configured": bool(settings.access_key_id and settings.secret_access_key),
            "api_token_configured": bool(settings.api_token),
            "cdn_endpoint_configured": bool(settings.cdn_endpoint_id),
        }

    async def configure_backend(
        self,
        provider: str,
        delivery_mode: str,
        *,
        validate: bool = False,
    ) -> Dict[str, Any]:
        provider = str(provider or "local").lower()
        delivery_mode = str(delivery_mode or "proxy").lower()
        if provider not in {"local", "digitalocean"}:
            raise ValueError("cache provider must be local or digitalocean")
        if delivery_mode not in {"proxy", "cdn"}:
            raise ValueError("cache delivery mode must be proxy or cdn")
        candidate, delivery_mode = self._candidate_backend(provider, delivery_mode)
        status = await candidate.validate() if validate else {"ok": True, **candidate.location()}
        self.backend = candidate
        self.provider = provider
        self.delivery_mode = delivery_mode
        debug_logger.log_info(f"Cache backend configured: provider={provider}, delivery={delivery_mode}")
        return status

    def _candidate_backend(self, provider: str, delivery_mode: str):
        provider = str(provider or "local").lower()
        delivery_mode = str(delivery_mode or "proxy").lower()
        if provider == "local":
            return LocalCacheBackend(self.cache_dir), "proxy"
        if provider == "digitalocean":
            env = self.digitalocean_environment(delivery_mode)
            return DigitalOceanSpacesBackend(env["settings"]), delivery_mode
        raise ValueError("cache provider must be local or digitalocean")

    async def validate_backend(self, provider: str, delivery_mode: str) -> Dict[str, Any]:
        candidate, normalized_mode = self._candidate_backend(provider, delivery_mode)
        result = await candidate.validate()
        return {**result, "delivery_mode": normalized_mode}

    def _is_cleanup_disabled(self) -> bool:
        return self.default_timeout <= 0

    def _disk_usage(self):
        return shutil.disk_usage(self.cache_dir)

    def _free_space_target(self, total_bytes: int, required_bytes: int = 0) -> int:
        reserve = max(
            MIN_FREE_SPACE_BYTES,
            min(MAX_FREE_SPACE_BYTES, int(total_bytes * FREE_SPACE_RATIO)),
        )
        return reserve + max(0, int(required_bytes))

    @staticmethod
    def _is_generated_media(path: Path) -> bool:
        return path.suffix.lower() in (_IMAGE_SUFFIXES | _VIDEO_SUFFIXES)

    @staticmethod
    def _safe_unlink(path: Path) -> int:
        """Delete one regular file without following symlinks."""
        try:
            if path.is_symlink() or not path.is_file():
                return 0
            size = path.stat().st_size
            path.unlink()
            return size
        except OSError:
            return 0

    def _iter_cache_files(self):
        """Yield regular cache files recursively without following symlinked directories."""
        if not self.cache_dir.exists():
            return
        for root, dirnames, filenames in os.walk(self.cache_dir, followlinks=False):
            root_path = Path(root)
            safe_dirs = []
            for dirname in dirnames:
                path = root_path / dirname
                try:
                    if not path.is_symlink():
                        safe_dirs.append(dirname)
                except OSError:
                    continue
            dirnames[:] = safe_dirs
            for filename in filenames:
                yield root_path / filename

    async def reclaim_cache_space(
        self,
        required_bytes: int = 0,
        *,
        target_free_bytes: Optional[int] = None,
    ) -> Dict[str, int]:
        """Restore the cache-volume reserve by evicting only generated media."""
        async with self._cleanup_lock:
            return await asyncio.to_thread(
                self._reclaim_cache_space_sync,
                required_bytes,
                target_free_bytes,
            )

    def _reclaim_cache_space_sync(
        self,
        required_bytes: int = 0,
        target_free_bytes: Optional[int] = None,
    ) -> Dict[str, int]:
        if self.provider != "local":
            usage = self._disk_usage()
            return {
                "free_before": int(usage.free),
                "free_after": int(usage.free),
                "target_free": 0,
                "reclaimed_bytes": 0,
                "removed_count": 0,
            }
        usage = self._disk_usage()
        target = (
            self._free_space_target(usage.total, required_bytes)
            if target_free_bytes is None
            else max(0, int(target_free_bytes)) + max(0, int(required_bytes))
        )
        free_before = usage.free
        reclaimed = 0
        removed = 0
        now = time.time()
        timeout = self.get_timeout()
        candidates = []

        for path in self._iter_cache_files() or ():
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            age = now - stat.st_mtime
            if path.name.endswith(".part"):
                if age < STALE_PART_SECONDS:
                    continue
                priority = 0
            elif self._is_generated_media(path):
                priority = 1 if timeout > 0 and age > timeout else 2
            else:
                continue
            candidates.append((priority, stat.st_mtime, path))

        for _priority, _mtime, path in sorted(candidates, key=lambda item: (item[0], item[1])):
            if free_before + reclaimed >= target:
                break
            size = self._safe_unlink(path)
            if size or not path.exists():
                reclaimed += size
                removed += 1

        try:
            free_after = self._disk_usage().free
        except OSError:
            free_after = free_before + reclaimed
        result = {
            "free_before": int(free_before),
            "free_after": int(free_after),
            "target_free": int(target),
            "reclaimed_bytes": int(reclaimed),
            "removed_count": int(removed),
        }
        if removed:
            debug_logger.log_warning(
                "Cache space recovery removed "
                f"{removed} file(s) ({reclaimed} bytes); free={free_after}, target={target}"
            )
        return result

    async def ensure_cache_capacity(self, required_bytes: int = 0) -> Dict[str, int]:
        result = await self.reclaim_cache_space(required_bytes)
        if result["free_after"] < result["target_free"]:
            raise OSError(
                28,
                "Insufficient storage for generated media "
                f"(free={result['free_after']}, required={result['target_free']})",
            )
        return result

    def _get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """读取当前请求链路里绑定的浏览器指纹。"""
        if not self.flow_client or not hasattr(self.flow_client, "get_request_fingerprint"):
            return None

        try:
            fingerprint = self.flow_client.get_request_fingerprint()
            if isinstance(fingerprint, dict) and fingerprint:
                return fingerprint
        except Exception as e:
            debug_logger.log_warning(f"Get request fingerprint failed: {str(e)}")

        return None

    async def _resolve_download_proxy(
        self,
        media_type: str,
        fingerprint: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """根据媒体类型解析下载代理地址。"""
        if isinstance(fingerprint, dict):
            fingerprint_proxy = str(fingerprint.get("proxy_url") or "").strip()
            if fingerprint_proxy:
                return fingerprint_proxy

        if not self.proxy_manager:
            return None

        try:
            # 媒体下载（图片/视频）优先使用独立的上传/下载代理
            if media_type in ("image", "video") and hasattr(self.proxy_manager, "get_media_proxy_url"):
                return await self.proxy_manager.get_media_proxy_url()

            # 其他下载走请求代理
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                return await self.proxy_manager.get_request_proxy_url()

            # 向后兼容旧实现
            if hasattr(self.proxy_manager, "get_proxy_url"):
                return await self.proxy_manager.get_proxy_url()
        except Exception as e:
            debug_logger.log_warning(f"Resolve download proxy failed: {str(e)}")

        return None

    def _guess_extension(self, url: str, media_type: str) -> str:
        """尽量保留原始扩展名，未知时回退到默认值。"""
        path = urlparse(url).path or ""
        guessed, _ = mimetypes.guess_type(path)
        suffix = Path(path).suffix.lower()

        if media_type == "video":
            if suffix in {".mp4", ".mov", ".webm", ".mkv", ".m4v"}:
                return suffix
            if guessed == "video/webm":
                return ".webm"
            if guessed == "video/quicktime":
                return ".mov"
            return ".mp4"

        if media_type == "image":
            if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp"}:
                return suffix
            if guessed == "image/png":
                return ".png"
            if guessed == "image/webp":
                return ".webp"
            if guessed == "image/gif":
                return ".gif"
            if guessed == "image/avif":
                return ".avif"
            if guessed == "image/bmp":
                return ".bmp"
            return ".jpg"

        return suffix

    @staticmethod
    def _is_signed_flow_cdn_url(url: str) -> bool:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        if host != "flow-content.google" and not host.endswith(".googleusercontent.com"):
            return False
        query = (parsed.query or "").lower()
        return "signature=" in query or "expires=" in query

    @staticmethod
    def _download_phase_for_url(url: str) -> str:
        if FileCache._is_signed_flow_cdn_url(url):
            return "cdn"
        if "getMediaUrlRedirect" in (url or ""):
            return "labs"
        host = (urlparse(url or "").hostname or "").lower()
        if host.endswith("labs.google"):
            return "labs"
        return "other"

    @staticmethod
    def _sec_fetch_site_for_url(url: str) -> str:
        host = (urlparse(url or "").hostname or "").lower()
        if host.endswith("labs.google"):
            return "same-origin"
        if host.endswith("google.com") or host.endswith("googleusercontent.com"):
            return "cross-site"
        return "cross-site"

    def _apply_fingerprint_headers(
        self,
        headers: Dict[str, str],
        fingerprint: Optional[Dict[str, Any]],
    ) -> None:
        if isinstance(fingerprint, dict):
            if fingerprint.get("user_agent"):
                headers["User-Agent"] = str(fingerprint["user_agent"])
            if fingerprint.get("accept_language"):
                headers["Accept-Language"] = str(fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = str(fingerprint["sec_ch_ua"])
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = str(fingerprint["sec_ch_ua_mobile"])
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = str(fingerprint["sec_ch_ua_platform"])
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

    def _build_cdn_download_headers(
        self,
        media_type: str,
        fingerprint: Optional[Dict[str, Any]] = None,
        url: Optional[str] = None,
    ) -> Dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": "https://labs.google/fx/tools/flow",
            "Sec-Fetch-Site": self._sec_fetch_site_for_url(url or ""),
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "video" if media_type == "video" else "image",
        }
        self._apply_fingerprint_headers(headers, fingerprint)
        return headers

    def _build_download_headers(
        self,
        media_type: str,
        fingerprint: Optional[Dict[str, Any]] = None,
        auth_token: Optional[str] = None,
        session_token: Optional[str] = None,
        url: Optional[str] = None,
    ) -> Dict[str, str]:
        """构建媒体下载请求头，优先复用当前打码浏览器指纹。"""
        if self._is_signed_flow_cdn_url(url or ""):
            return self._build_cdn_download_headers(media_type, fingerprint=fingerprint, url=url)

        headers = {
            "Accept": (
                "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                if media_type == "image"
                else "*/*"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": "https://labs.google/fx/tools/flow",
            "Origin": "https://labs.google",
            "Sec-Fetch-Site": self._sec_fetch_site_for_url(url or ""),
            "Sec-Fetch-Mode": "cors",
        }

        if media_type == "image":
            headers["Sec-Fetch-Dest"] = "image"
        else:
            headers["Sec-Fetch-Dest"] = "video"

        self._apply_fingerprint_headers(headers, fingerprint)

        clean_auth_token = (auth_token or "").strip()
        if clean_auth_token:
            headers["Authorization"] = f"Bearer {clean_auth_token}"
        clean_session_token = (session_token or "").strip()
        if clean_session_token and self._download_phase_for_url(url or "") == "labs":
            headers["Cookie"] = f"__Secure-next-auth.session-token={clean_session_token}"
        return headers

    @staticmethod
    def _video_download_rejection_error(url: str) -> str:
        phase = FileCache._download_phase_for_url(url)
        if phase == "cdn":
            return "Video CDN download was rejected by flow-content.google"
        if phase == "labs":
            return "Video cache download was rejected by Flow media endpoint"
        return "Video cache download was rejected by upstream media endpoint"

    def _log_download_rejection(
        self,
        *,
        url: str,
        status_code: int,
        headers: Dict[str, str],
        client_name: str,
        response_body: Optional[bytes] = None,
    ) -> None:
        host = urlparse(url or "").hostname or "<unknown>"
        phase = self._download_phase_for_url(url)
        body_preview = ""
        if response_body:
            body_preview = response_body[:120].decode("utf-8", errors="replace").replace("\n", " ")
        debug_logger.log_warning(
            f"{client_name} video cache download rejected: "
            f"phase={phase}, status={status_code}, host={host}, "
            f"auth={'yes' if headers.get('Authorization') else 'no'}, "
            f"cookie={'yes' if headers.get('Cookie') else 'no'}"
            + (f", body_preview={body_preview!r}" if body_preview else "")
        )

    async def _cdn_download_proxy_attempts(
        self,
        fingerprint: Optional[Dict[str, Any]],
    ) -> List[Optional[str]]:
        attempts: List[Optional[str]] = []
        fingerprint_proxy = None
        if isinstance(fingerprint, dict):
            fingerprint_proxy = str(fingerprint.get("proxy_url") or "").strip() or None
        attempts.append(fingerprint_proxy)

        media_proxy = None
        if self.proxy_manager and hasattr(self.proxy_manager, "get_media_proxy_url"):
            try:
                media_proxy = await self.proxy_manager.get_media_proxy_url()
            except Exception as exc:
                debug_logger.log_warning(f"Resolve CDN media proxy failed: {exc}")
        if media_proxy and media_proxy not in attempts:
            attempts.append(media_proxy)
        return attempts or [None]

    def _is_valid_download_response(self, content: bytes, content_type: str, media_type: str) -> bool:
        if not content:
            return False
        normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
        if media_type == "video":
            if len(content) < MIN_VALID_VIDEO_BYTES:
                return False
            if normalized_type and not (
                normalized_type.startswith("video/")
                or normalized_type == "application/octet-stream"
            ):
                return False
            prefix = content[:512].lstrip().lower()
            if (
                prefix.startswith(b"<!doctype html")
                or prefix.startswith(b"<html")
                or prefix.startswith(b"{")
                or prefix.startswith(b"[")
                or prefix.startswith(b"<?xml")
            ):
                return False
            media_prefix = content[:512]
            has_mp4_signature = b"ftyp" in media_prefix[:64]
            has_webm_signature = media_prefix.startswith(b"\x1a\x45\xdf\xa3")
            if not (has_mp4_signature or has_webm_signature):
                return False
        return True

    def _validate_cached_file(self, file_path: Path, media_type: str) -> int:
        if not file_path.exists():
            raise Exception("Downloaded file is missing")
        file_size = file_path.stat().st_size
        if file_size <= 0:
            raise Exception("Downloaded file is empty")
        with open(file_path, "rb") as f:
            prefix = f.read(max(512, MIN_VALID_VIDEO_BYTES if media_type == "video" else 512))
        guessed_type = mimetypes.guess_type(file_path.name)[0] or ""
        if not self._is_valid_download_response(prefix, guessed_type, media_type):
            raise Exception("Downloaded file is not valid media")
        return file_size

    def _write_cached_content(self, file_path: Path, content: bytes):
        """先写临时文件，再原子替换，避免并发读到半截文件。"""
        temp_path = file_path.with_suffix(f"{file_path.suffix}.part")
        try:
            with open(temp_path, "wb") as f:
                f.write(content)
            temp_path.replace(file_path)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    async def _store_download_response(
        self,
        *,
        filename: str,
        file_path: Path,
        content: bytes,
        content_type: str,
        media_type: str,
        api_key_id: Optional[int],
        token_id: Optional[int],
        flow_project_id: Optional[str],
        source_url: str,
        method_name: str,
    ) -> str:
        if not self._is_valid_download_response(content, content_type, media_type):
            debug_logger.log_warning(
                f"Invalid cached {media_type} response from {method_name}: "
                f"content_type={content_type or '<empty>'}, bytes={len(content or b'')}"
            )
            raise Exception("Downloaded video response is not valid media")

        await self.ensure_cache_capacity(len(content))
        self._write_cached_content(file_path, content)
        await self._record_cache_metadata(
            filename=filename,
            api_key_id=api_key_id,
            token_id=token_id,
            flow_project_id=flow_project_id,
            media_type=media_type,
            source_url=source_url,
        )
        debug_logger.log_info(
            f"File cached ({method_name}): {filename} ({len(content)} bytes)"
        )
        return filename

    async def start_cleanup_task(self):
        """Start background cleanup task"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            return True
        return True

    async def stop_cleanup_task(self):
        """Stop background cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def refresh_cleanup_task(self) -> bool:
        """Apply the latest timeout setting to the cleanup background task."""
        return await self.start_cleanup_task()

    async def _cleanup_loop(self):
        """Background task to clean up expired files"""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                from .redis_runtime import redis_runtime

                if redis_runtime.maintenance_active:
                    continue
                await self._cleanup_expired_files()
                await self.reclaim_cache_space()
            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_logger.log_error(
                    error_message=f"Cleanup task error: {str(e)}",
                    status_code=0,
                    response_text=""
                )

    async def _cleanup_expired_files(self):
        """Remove expired cache files"""
        if self.provider != "local":
            try:
                removed_count, removed_bytes = await self.backend.cleanup_expired(self.get_timeout())
                return {"removed_count": removed_count, "reclaimed_bytes": removed_bytes}
            except Exception as e:
                debug_logger.log_error(
                    error_message=f"Failed to cleanup {self.provider} cache: {str(e)}",
                    status_code=0,
                    response_text="",
                )
                return {"removed_count": 0, "reclaimed_bytes": 0}
        try:
            return await asyncio.to_thread(self._cleanup_local_expired_sync)
        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to cleanup expired files: {str(e)}",
                status_code=0,
                response_text=""
            )
            return {"removed_count": 0, "reclaimed_bytes": 0}

    def _cleanup_local_expired_sync(self) -> Dict[str, int]:
        timeout = self.get_timeout()
        current_time = time.time()
        removed_count = 0
        removed_bytes = 0
        for file_path in self.cache_dir.iterdir():
            try:
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                file_age = current_time - file_path.stat().st_mtime
            except OSError:
                continue
            stale_part = file_path.name.endswith(".part") and file_age >= STALE_PART_SECONDS
            expired_media = timeout > 0 and self._is_generated_media(file_path) and file_age > timeout
            if stale_part or expired_media:
                size = self._safe_unlink(file_path)
                if size or not file_path.exists():
                    removed_bytes += size
                    removed_count += 1
        if removed_count > 0:
            debug_logger.log_info(f"Cleanup: removed {removed_count} expired cache files")
        return {"removed_count": removed_count, "reclaimed_bytes": removed_bytes}

    def _generate_cache_filename(
        self,
        url: str,
        media_type: str,
        api_key_id: Optional[int] = None,
        flow_project_id: Optional[str] = None,
    ) -> str:
        """Generate unique filename for cached file"""
        # Bind cache entries to API key and Flow project to avoid cross-key / cross-project sharing.
        pid = (flow_project_id or "").strip()
        digest_source = f"{api_key_id or 0}:{pid}:{url}"
        url_hash = hashlib.md5(digest_source.encode()).hexdigest()
        ext = self._guess_extension(url, media_type)

        return f"{url_hash}{ext}"

    def _normalize_cache_error(self, error: Exception) -> str:
        """整理缓存错误，避免将底层命令异常直接暴露给用户。"""
        if isinstance(error, FileNotFoundError):
            missing_name = Path(getattr(error, "filename", "") or "curl").name or "curl"
            return f"本机未安装 {missing_name}"

        message = str(error or "").strip()
        if not message:
            return "未知错误"

        if message.startswith("Failed to cache file:"):
            message = message.split(":", 1)[1].strip() or "未知错误"

        return message

    def _safe_python_download_error(self, error: Exception, media_type: str) -> Exception:
        message = str(error or "").strip()
        if (
            message.startswith("Downloaded ")
            or "Flow media endpoint" in message
            or "flow-content.google" in message
            or "upstream media endpoint" in message
        ):
            return Exception(message)
        if media_type == "video":
            return Exception("Python video cache download failed before receiving media")
        return error

    async def download_and_cache(
        self,
        url: str,
        media_type: str,
        api_key_id: Optional[int] = None,
        token_id: Optional[int] = None,
        flow_project_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        session_token: Optional[str] = None,
    ) -> str:
        """
        Download file from URL and cache it locally

        Args:
            url: File URL to download
            media_type: 'image' or 'video'

        Returns:
            Local cache filename
        """
        filename = self._generate_cache_filename(
            url, media_type, api_key_id=api_key_id, flow_project_id=flow_project_id
        )
        file_path = self.cache_dir / filename
        download_lock = self._download_locks.setdefault(filename, asyncio.Lock())

        async with download_lock:
            if self.provider != "local":
                existing = await self.backend.stat(filename)
                if existing is not None:
                    age = time.time() - existing.modified_at.timestamp()
                    if self._is_cleanup_disabled() or age < self.default_timeout:
                        debug_logger.log_info(f"Cache hit ({self.provider}): {filename}")
                        await self._record_cache_metadata(
                            filename=filename,
                            api_key_id=api_key_id,
                            token_id=token_id,
                            flow_project_id=flow_project_id,
                            media_type=media_type,
                            source_url=url,
                            stored_object=existing,
                        )
                        return filename
                    await self.backend.delete(filename)
            # Check if already cached and not expired
            if file_path.exists():
                try:
                    self._validate_cached_file(file_path, media_type)
                except Exception as e:
                    debug_logger.log_warning(
                        f"Invalid cache hit removed: {filename} ({str(e)})"
                    )
                    try:
                        file_path.unlink()
                    except Exception:
                        pass
                else:
                    if self._is_cleanup_disabled():
                        await self._record_cache_metadata(
                            filename=filename,
                            api_key_id=api_key_id,
                            token_id=token_id,
                            flow_project_id=flow_project_id,
                            media_type=media_type,
                            source_url=url,
                        )
                        return filename
                    file_age = time.time() - file_path.stat().st_mtime
                    if file_age < self.default_timeout:
                        debug_logger.log_info(f"Cache hit: {filename}")
                        await self._record_cache_metadata(
                            filename=filename,
                            api_key_id=api_key_id,
                            token_id=token_id,
                            flow_project_id=flow_project_id,
                            media_type=media_type,
                            source_url=url,
                        )
                        return filename
                    try:
                        file_path.unlink()
                    except Exception:
                        pass

            # Download file
            debug_logger.log_info(f"Downloading file from: {url}")

            fingerprint = self._get_request_fingerprint()
            is_cdn = self._is_signed_flow_cdn_url(url)
            if is_cdn:
                proxy_attempts = await self._cdn_download_proxy_attempts(fingerprint)
            else:
                proxy_attempts = [await self._resolve_download_proxy(media_type, fingerprint=fingerprint)]

            headers = self._build_download_headers(
                media_type,
                fingerprint=fingerprint,
                auth_token=auth_token,
                session_token=session_token,
                url=url,
            )
            python_download_error: Optional[Exception] = None

            for attempt_index, proxy_url in enumerate(proxy_attempts):
                if is_cdn and attempt_index > 0:
                    debug_logger.log_warning(
                        f"CDN download retry via media proxy after direct rejection "
                        f"(proxy={'set' if proxy_url else 'direct'})"
                    )

                skip_to_next_proxy = False

                # Try method 1: curl_cffi with browser impersonation
                try:
                    async with AsyncSession() as session:
                        response = await session.get(
                            url,
                            timeout=60,
                            proxy=proxy_url,
                            headers=headers,
                            impersonate="chrome120",
                            verify=False,
                            allow_redirects=True,
                        )

                        if response.status_code == 200:
                            return await self._store_download_response(
                                filename=filename,
                                file_path=file_path,
                                content=response.content,
                                content_type=response.headers.get("content-type", ""),
                                media_type=media_type,
                                api_key_id=api_key_id,
                                token_id=token_id,
                                flow_project_id=flow_project_id,
                                source_url=url,
                                method_name="curl_cffi",
                            )
                        if media_type == "video" and response.status_code in (401, 403):
                            self._log_download_rejection(
                                url=url,
                                status_code=response.status_code,
                                headers=headers,
                                client_name="curl_cffi",
                                response_body=response.content,
                            )
                            python_download_error = Exception(self._video_download_rejection_error(url))
                            if is_cdn and attempt_index < len(proxy_attempts) - 1:
                                skip_to_next_proxy = True
                        else:
                            python_download_error = Exception(
                                f"Python video cache download failed with HTTP {response.status_code}"
                            )
                        if not skip_to_next_proxy:
                            debug_logger.log_warning(
                                f"curl_cffi failed with HTTP {response.status_code}, trying httpx..."
                            )

                except Exception as e:
                    python_download_error = self._safe_python_download_error(e, media_type)
                    debug_logger.log_warning(f"curl_cffi failed: {str(e)}, trying httpx...")

                if skip_to_next_proxy:
                    continue

                # Try method 2: httpx pure-Python fallback for environments without wget/curl.
                try:
                    timeout = httpx.Timeout(60.0, connect=30.0)
                    async with httpx.AsyncClient(
                        follow_redirects=True,
                        timeout=timeout,
                        verify=False,
                        proxy=proxy_url,
                    ) as client:
                        response = await client.get(url, headers=headers)

                    if response.status_code == 200:
                        return await self._store_download_response(
                            filename=filename,
                            file_path=file_path,
                            content=response.content,
                            content_type=response.headers.get("content-type", ""),
                            media_type=media_type,
                            api_key_id=api_key_id,
                            token_id=token_id,
                            flow_project_id=flow_project_id,
                            source_url=url,
                            method_name="httpx",
                        )
                    if media_type == "video" and response.status_code in (401, 403):
                        self._log_download_rejection(
                            url=url,
                            status_code=response.status_code,
                            headers=headers,
                            client_name="httpx",
                            response_body=response.content,
                        )
                        python_download_error = Exception(self._video_download_rejection_error(url))
                        if is_cdn and attempt_index < len(proxy_attempts) - 1:
                            skip_to_next_proxy = True
                    else:
                        python_download_error = Exception(
                            f"Python video cache download failed with HTTP {response.status_code}"
                        )
                    if not skip_to_next_proxy:
                        debug_logger.log_warning(
                            f"httpx failed with HTTP {response.status_code}, trying wget..."
                        )
                except Exception as e:
                    python_download_error = self._safe_python_download_error(e, media_type)
                    debug_logger.log_warning(f"httpx failed: {str(e)}, trying wget...")

                if skip_to_next_proxy:
                    continue

                # Try method 3: wget command
                try:
                    import subprocess

                    await self.ensure_cache_capacity()

                    wget_cmd = [
                        "wget",
                        "-q",
                        "-O", str(file_path),
                        "--timeout=60",
                        "--tries=3",
                        f"--user-agent={headers.get('User-Agent', '')}",
                        f"--header=Accept: {headers.get('Accept', '*/*')}",
                        f"--header=Accept-Language: {headers.get('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8')}",
                        f"--header=Connection: {headers.get('Connection', 'keep-alive')}",
                        f"--header=Referer: {headers.get('Referer', 'https://labs.google/fx/tools/flow')}",
                    ]
                    if "Origin" in headers:
                        wget_cmd.append(f"--header=Origin: {headers['Origin']}")
                    if "Authorization" in headers:
                        wget_cmd.append(f"--header=Authorization: {headers['Authorization']}")
                    if "Cookie" in headers:
                        wget_cmd.append(f"--header=Cookie: {headers['Cookie']}")

                    if "sec-ch-ua" in headers:
                        wget_cmd.append(f"--header=sec-ch-ua: {headers['sec-ch-ua']}")
                    if "sec-ch-ua-mobile" in headers:
                        wget_cmd.append(f"--header=sec-ch-ua-mobile: {headers['sec-ch-ua-mobile']}")
                    if "sec-ch-ua-platform" in headers:
                        wget_cmd.append(f"--header=sec-ch-ua-platform: {headers['sec-ch-ua-platform']}")

                    if proxy_url:
                        env = os.environ.copy()
                        env["http_proxy"] = proxy_url
                        env["https_proxy"] = proxy_url
                    else:
                        env = None

                    wget_cmd.append(url)
                    result = await asyncio.to_thread(
                        subprocess.run,
                        wget_cmd,
                        capture_output=True,
                        timeout=90,
                        env=env,
                    )

                    if result.returncode == 0 and file_path.exists():
                        file_size = self._validate_cached_file(file_path, media_type)
                        debug_logger.log_info(f"File cached (wget): {filename} ({file_size} bytes)")
                        await self._record_cache_metadata(
                            filename=filename,
                            api_key_id=api_key_id,
                            token_id=token_id,
                            flow_project_id=flow_project_id,
                            media_type=media_type,
                            source_url=url,
                        )
                        return filename

                    error_msg = result.stderr.decode("utf-8", errors="ignore") if result.stderr else "Unknown error"
                    debug_logger.log_warning(f"wget failed: {error_msg}, trying curl...")
                    self._safe_unlink(file_path)

                except FileNotFoundError:
                    self._safe_unlink(file_path)
                    debug_logger.log_warning("wget not found, trying curl...")
                except Exception as e:
                    self._safe_unlink(file_path)
                    if "not valid media" in str(e):
                        python_download_error = Exception("Downloaded video response is not valid media")
                    debug_logger.log_warning(f"wget failed: {str(e)}, trying curl...")

                # Try method 4: system curl command
                try:
                    import subprocess

                    await self.ensure_cache_capacity()

                    curl_cmd = [
                        "curl",
                        "-L",
                        "-s",
                        "-o", str(file_path),
                        "--max-time", "60",
                        "-H", f"Accept: {headers.get('Accept', '*/*')}",
                        "-H", f"Accept-Language: {headers.get('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8')}",
                        "-H", f"Connection: {headers.get('Connection', 'keep-alive')}",
                        "-H", f"Referer: {headers.get('Referer', 'https://labs.google/fx/tools/flow')}",
                        "-A", headers.get("User-Agent", ""),
                    ]

                    if "Origin" in headers:
                        curl_cmd.extend(["-H", f"Origin: {headers['Origin']}"])
                    if "sec-ch-ua" in headers:
                        curl_cmd.extend(["-H", f"sec-ch-ua: {headers['sec-ch-ua']}"])
                    if "sec-ch-ua-mobile" in headers:
                        curl_cmd.extend(["-H", f"sec-ch-ua-mobile: {headers['sec-ch-ua-mobile']}"])
                    if "sec-ch-ua-platform" in headers:
                        curl_cmd.extend(["-H", f"sec-ch-ua-platform: {headers['sec-ch-ua-platform']}"])
                    if "Authorization" in headers:
                        curl_cmd.extend(["-H", f"Authorization: {headers['Authorization']}"])
                    if "Cookie" in headers:
                        curl_cmd.extend(["-H", f"Cookie: {headers['Cookie']}"])
                    if proxy_url:
                        curl_cmd.extend(["-x", proxy_url])

                    curl_cmd.append(url)
                    result = await asyncio.to_thread(
                        subprocess.run,
                        curl_cmd,
                        capture_output=True,
                        timeout=90,
                    )

                    if result.returncode == 0 and file_path.exists():
                        file_size = self._validate_cached_file(file_path, media_type)
                        debug_logger.log_info(f"File cached (curl): {filename} ({file_size} bytes)")
                        await self._record_cache_metadata(
                            filename=filename,
                            api_key_id=api_key_id,
                            token_id=token_id,
                            flow_project_id=flow_project_id,
                            media_type=media_type,
                            source_url=url,
                        )
                        return filename

                    error_msg = result.stderr.decode("utf-8", errors="ignore") if result.stderr else "Unknown error"
                    raise Exception(f"curl command failed: {error_msg}")

                except FileNotFoundError as e:
                    self._safe_unlink(file_path)
                    if is_cdn and attempt_index < len(proxy_attempts) - 1:
                        continue
                    normalized_error = (
                        self._normalize_cache_error(python_download_error)
                        if python_download_error is not None
                        else self._normalize_cache_error(e)
                    )
                    debug_logger.log_error(
                        error_message=f"Failed to download file: {str(e)}",
                        status_code=0,
                        response_text=str(e),
                    )
                    raise Exception(normalized_error) from e
                except Exception as e:
                    self._safe_unlink(file_path)
                    if "not valid media" in str(e):
                        python_download_error = Exception("Downloaded video response is not valid media")
                    if is_cdn and attempt_index < len(proxy_attempts) - 1:
                        continue
                    normalized_error = self._normalize_cache_error(
                        python_download_error if python_download_error is not None else e
                    )
                    debug_logger.log_error(
                        error_message=f"Failed to download file: {str(e)}",
                        status_code=0,
                        response_text=str(e),
                    )
                    raise Exception(normalized_error) from e

            if python_download_error is not None:
                raise python_download_error
            raise Exception("Video cache download failed before receiving media")

    async def cache_base64_video(
        self,
        base64_data: str,
        *,
        api_key_id: Optional[int] = None,
        token_id: Optional[int] = None,
        flow_project_id: Optional[str] = None,
        source_media_name: Optional[str] = None,
    ) -> str:
        """Decode, validate, and atomically cache a base64-encoded video."""
        import base64
        import uuid

        raw_value = str(base64_data or "").strip()
        if raw_value.startswith("data:"):
            if "," not in raw_value:
                raise ValueError("Base64 video data URL is malformed")
            raw_value = raw_value.split(",", 1)[1]
        compact_value = "".join(raw_value.split())
        if not compact_value:
            raise ValueError("Base64 video payload is empty")

        project_id = str(flow_project_id or "").strip()
        unique_id = hashlib.md5(
            f"{api_key_id or 0}:{project_id}:{uuid.uuid4()}:{time.time()}".encode()
        ).hexdigest()
        filename = f"{unique_id}.mp4"
        file_path = self.cache_dir / filename
        try:
            video_data = base64.b64decode(compact_value, validate=True)
            return await self._store_download_response(
                filename=filename,
                file_path=file_path,
                content=video_data,
                content_type="video/mp4",
                media_type="video",
                api_key_id=api_key_id,
                token_id=token_id,
                flow_project_id=project_id,
                source_url=(
                    f"flow-media:{str(source_media_name).strip()}"
                    if str(source_media_name or "").strip()
                    else "flow-media:encoded-video"
                ),
                method_name="Flow get_media base64",
            )
        except Exception as exc:
            self._safe_unlink(file_path)
            raise Exception(f"Failed to cache base64 video: {self._normalize_cache_error(exc)}") from exc

    async def cache_base64_image(
        self,
        base64_data: str,
        resolution: str = "",
        api_key_id: Optional[int] = None,
        token_id: Optional[int] = None,
        flow_project_id: Optional[str] = None,
    ) -> str:
        """
        Cache base64 encoded image data to local file

        Args:
            base64_data: Base64 encoded image data (without data:image/... prefix)
            resolution: Resolution info for filename (e.g., "4K", "2K")

        Returns:
            Local cache filename
        """
        import base64
        import uuid

        pid = (flow_project_id or "").strip()
        unique_id = hashlib.md5(
            f"{api_key_id or 0}:{pid}:{uuid.uuid4()}:{time.time()}".encode()
        ).hexdigest()
        suffix = f"_{resolution}" if resolution else ""
        filename = f"{unique_id}{suffix}.jpg"
        file_path = self.cache_dir / filename

        try:
            # Decode base64 and save to file
            image_data = base64.b64decode(base64_data)
            await self.ensure_cache_capacity(len(image_data))
            self._write_cached_content(file_path, image_data)
            await self._record_cache_metadata(
                filename=filename,
                api_key_id=api_key_id,
                token_id=token_id,
                flow_project_id=flow_project_id,
                media_type="image",
                source_url=None,
            )
            debug_logger.log_info(f"Base64 image cached: {filename} ({len(image_data)} bytes)")
            return filename
        except Exception as e:
            self._safe_unlink(file_path)
            self._safe_unlink(file_path.with_suffix(f"{file_path.suffix}.part"))
            debug_logger.log_error(
                error_message=f"Failed to cache base64 image: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise Exception(f"Failed to cache base64 image: {str(e)}")

    def get_cache_path(self, filename: str) -> Path:
        """Get full path to cached file"""
        return self.cache_dir / filename

    def build_url(
        self,
        filename: str,
        proxy_base_url: str,
        flow_project_id: Optional[str] = None,
    ) -> str:
        direct = self.backend.public_url(filename)
        if direct:
            return direct
        base = f"{proxy_base_url.rstrip('/')}/api/cache/blob/{quote(filename, safe='')}"
        pid = (flow_project_id or "").strip()
        return f"{base}?project_id={quote(pid, safe='')}" if pid else base

    async def store_bytes(
        self,
        filename: str,
        content: bytes,
        content_type: str,
        *,
        api_key_id: Optional[int] = None,
        token_id: Optional[int] = None,
        flow_project_id: Optional[str] = None,
        media_type: str = "other",
        source_url: Optional[str] = None,
    ) -> str:
        stored = await self.backend.store_bytes(filename, content, content_type)
        await self._record_cache_metadata(
            filename=filename,
            api_key_id=api_key_id,
            token_id=token_id,
            flow_project_id=flow_project_id,
            media_type=media_type,
            source_url=source_url,
            stored_object=stored,
        )
        return filename

    async def read_bytes(self, filename: str) -> bytes:
        return await self.backend.read_bytes(filename)

    async def open_cached(self, filename: str, range_header: Optional[str] = None):
        return await self.backend.open(filename, range_header)

    async def get_stats(self) -> Dict[str, Any]:
        objects = await self.backend.list()
        return {
            **self.backend.location(),
            "delivery_mode": self.delivery_mode,
            "file_count": len(objects),
            "total_bytes": sum(item.size_bytes for item in objects),
        }

    async def list_files(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for item in await self.backend.list():
            suffix = Path(item.name).suffix.lower()
            kind = "image" if suffix in _IMAGE_SUFFIXES else "video" if suffix in _VIDEO_SUFFIXES else "other"
            rows.append({
                "name": item.name,
                "size_bytes": item.size_bytes,
                "kind": kind,
                "modified_at": item.modified_at.isoformat(),
                "provider": self.provider,
                "delivery_mode": self.delivery_mode,
                "public_url": self.backend.public_url(item.name),
            })
        return rows

    def set_timeout(self, timeout: int):
        """Set cache timeout in seconds"""
        self.default_timeout = max(0, int(timeout))
        debug_logger.log_info(f"Cache timeout updated to {timeout} seconds")

    def get_timeout(self) -> int:
        """Get current cache timeout"""
        return self.default_timeout

    def get_dir_stats(self) -> Dict[str, Any]:
        """Return file count, total size, and resolved cache directory path."""
        total_bytes = 0
        file_count = 0
        try:
            if not self.cache_dir.exists():
                return {
                    "cache_dir": str(self.cache_dir.resolve()),
                    "file_count": 0,
                    "total_bytes": 0,
                }
            for file_path in self.cache_dir.iterdir():
                if file_path.is_file():
                    try:
                        total_bytes += file_path.stat().st_size
                        file_count += 1
                    except OSError:
                        pass
        except OSError as e:
            debug_logger.log_warning(f"get_dir_stats: {e}")
        return {
            "cache_dir": str(self.cache_dir.resolve()),
            "file_count": file_count,
            "total_bytes": total_bytes,
        }

    def list_gallery_files(self) -> List[Dict[str, Any]]:
        """List cache files with media kind for admin gallery (newest first)."""
        rows: List[Dict[str, Any]] = []
        try:
            if not self.cache_dir.exists():
                return rows
            for path in self.cache_dir.iterdir():
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix in _IMAGE_SUFFIXES:
                    kind = "image"
                elif suffix in _VIDEO_SUFFIXES:
                    kind = "video"
                else:
                    kind = "other"
                try:
                    st = path.stat()
                except OSError:
                    continue
                mtime = st.st_mtime
                rows.append({
                    "name": path.name,
                    "size_bytes": st.st_size,
                    "kind": kind,
                    "modified_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                    "_mtime": mtime,
                })
        except OSError as e:
            debug_logger.log_warning(f"list_gallery_files: {e}")
        rows.sort(key=lambda r: r.get("_mtime", 0), reverse=True)
        for r in rows:
            r.pop("_mtime", None)
        return rows

    def clear_all_files(self) -> Tuple[int, int]:
        """Delete all files in cache_dir. Returns (removed_count, removed_bytes)."""
        removed_count = 0
        removed_bytes = 0
        try:
            if not self.cache_dir.exists():
                return 0, 0
            for file_path in self.cache_dir.iterdir():
                if file_path.is_file():
                    try:
                        removed_bytes += file_path.stat().st_size
                        file_path.unlink()
                        removed_count += 1
                    except OSError:
                        pass
            debug_logger.log_info(f"Cache cleared: removed {removed_count} files ({removed_bytes} bytes)")
            return removed_count, removed_bytes
        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to clear cache: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise

    async def clear_all(self) -> int:
        """Clear all cached files (async wrapper). Returns removed file count."""
        count, _ = await self.backend.clear()
        if self.cache_repository is not None:
            await self.cache_repository.delete_all_cache_file_metadata()
        return count

    async def _record_cache_metadata(
        self,
        *,
        filename: str,
        api_key_id: Optional[int],
        token_id: Optional[int],
        flow_project_id: Optional[str] = None,
        media_type: str,
        source_url: Optional[str],
        stored_object=None,
    ):
        if stored_object is None and self.provider != "local":
            staging_path = self.cache_dir / Path(filename).name
            if staging_path.is_file():
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                try:
                    stored_object = await self.backend.store_file(filename, staging_path, content_type)
                finally:
                    self._safe_unlink(staging_path)
            else:
                stored_object = await self.backend.stat(filename)
        if self.cache_repository is None or api_key_id is None:
            return
        try:
            fpid = (flow_project_id or "").strip() or None
            await self.cache_repository.upsert_cache_file(
                filename=filename,
                api_key_id=int(api_key_id),
                token_id=token_id,
                media_type=media_type,
                source_url=source_url,
                flow_project_id=fpid,
                storage_provider=self.provider,
                object_key=getattr(stored_object, "key", filename),
                delivery_mode=self.delivery_mode,
                size_bytes=getattr(stored_object, "size_bytes", None),
            )
        except Exception as exc:
            debug_logger.log_warning(f"Failed to record cache metadata: {exc}")
