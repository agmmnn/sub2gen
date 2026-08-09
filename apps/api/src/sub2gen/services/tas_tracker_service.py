import asyncio
from typing import List, Dict, Any, Optional

from ..core.config import config
from ..core.logger import debug_logger
from .tas_contributor_http import (
    CsrTokenCache,
    DEFAULT_TLS_PROFILE,
    fetch_contributor_raw_images,
    map_image,
)
from .tas_search_http import fetch_tas_search_raw, normalize_content_type_csv


class TaskTrackerService:
    def __init__(self):
        self._csr_cache = CsrTokenCache()
        self._csr_cache_search = CsrTokenCache()

    def _map_image(self, img: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return map_image(img)

    async def fetch_contributor_assets(
        self,
        search_id: str,
        order: str = "creation",
        pages: Optional[List[int]] = None,
        title_filter: str = "",
        generative_ai: str = "all",
        content_type: str = "all",
    ) -> List[Dict[str, Any]]:
        search_id = search_id.strip()
        order = order.strip()
        title_filter = title_filter.strip().lower()
        if not pages:
            pages = [1]

        pages = sorted(list(set(p for p in pages if p >= 1)))
        if not pages:
            pages = [1]

        auth_cookie, device_id, device_token, turnstile_token, tls_profile = (
            self._validate_tracker_credentials()
        )

        debug_logger.log_info(
            f"[TaskTracker] direct HTTP search={search_id} pages={pages} tls={tls_profile!r}"
        )

        all_images, err = await asyncio.to_thread(
            fetch_contributor_raw_images,
            search_id,
            order,
            pages,
            generative_ai,
            content_type,
            auth_cookie,
            device_id,
            device_token or None,
            turnstile_token,
            tls_profile,
            self._csr_cache,
            None,
        )

        if err:
            if "401" in err or "Unauthorized" in err or "unauthorized" in err.lower():
                raise ValueError(
                    "Auth session looks expired or invalid (CSR or search rejected). "
                    "Refresh cookies and device token in Task Tracker settings."
                )
            raise ValueError(f"Task Tracker fetch failed: {err}")

        mapped = [self._map_image(img) for img in all_images if self._map_image(img)]

        if title_filter:
            filtered = [r for r in mapped if title_filter in r["title"].lower()]
        else:
            filtered = mapped

        debug_logger.log_info(f"[TaskTracker] complete. mapped={len(mapped)} filtered={len(filtered)}")
        return filtered

    def _validate_tracker_credentials(self) -> tuple[str, str, Optional[str], Optional[str], str]:
        """Shared cookie/device/TLS validation for contributor and keyword fetches."""
        auth_cookie = config.task_tracker_cookies.strip()
        device_id = config.task_tracker_device_id.strip()
        device_token = config.task_tracker_device_token.strip()
        turnstile_token = (config.task_tracker_turnstile_token or "").strip() or None
        tls_profile = (config.task_tracker_tls_profile or "").strip() or DEFAULT_TLS_PROFILE

        if not auth_cookie or "__Secure-next-auth.session-token=" not in auth_cookie:
            raise ValueError(
                "Invalid TAS Tracker cookies. Must contain __Secure-next-auth.session-token"
            )

        if not device_id:
            device_id = "dev_d6u2k6_wabygqst2z9_mocsd0nz"

        if not device_token:
            raise ValueError(
                "TAS Tracker device token is required. Set it in Admin → TAS Tracker Settings "
                "(x-device-token from DevTools on POST /api/auth/csr-token)."
            )

        return auth_cookie, device_id, device_token or None, turnstile_token, tls_profile

    async def fetch_keyword_search(
        self,
        q: str,
        order: str = "relevance",
        pages: Optional[List[int]] = None,
        generative_ai: str = "all",
        content_type: str = "all",
    ) -> Dict[str, Any]:
        q = q.strip()
        order = order.strip()
        if not q:
            raise ValueError("Query q must not be empty")

        if not pages:
            pages = [1]
        pages = sorted(list(set(p for p in pages if p >= 1)))
        if not pages:
            pages = [1]

        content_csv = normalize_content_type_csv(content_type)

        auth_cookie, device_id, device_token, turnstile_token, tls_profile = (
            self._validate_tracker_credentials()
        )

        debug_logger.log_info(
            f"[TaskTracker] keyword search q={q!r} pages={pages} tls={tls_profile!r}"
        )

        merged, err = await asyncio.to_thread(
            fetch_tas_search_raw,
            q,
            order,
            pages,
            generative_ai,
            content_csv,
            auth_cookie,
            device_id,
            device_token,
            turnstile_token,
            tls_profile,
            self._csr_cache_search,
            None,
        )

        if err:
            if "401" in err or "Unauthorized" in err or "unauthorized" in err.lower():
                raise ValueError(
                    "Auth session looks expired or invalid (CSR or search rejected). "
                    "Refresh cookies and device token in Task Tracker settings."
                )
            raise ValueError(f"Task Tracker keyword search failed: {err}")

        debug_logger.log_info(
            f"[TaskTracker] keyword complete. images={len((merged or {}).get('images') or [])}"
        )
        return merged
