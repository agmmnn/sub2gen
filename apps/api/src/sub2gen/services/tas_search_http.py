"""
Direct HTTP client for tastracker.com keyword search (GET /api/search).

Mirrors browser Referer on /search?... and reuses CSR / device / Turnstile
headers from tas_contributor_http. Keyword responses may include
encodedEnrichment; merge into images the same way as contributor-search
(see tas_contributor_http).
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from .tas_contributor_http import (
    BASE,
    CsrTokenCache,
    DEFAULT_TLS_PROFILE,
    DEFAULT_UA,
    ensure_csr_token,
    looks_unauthorized,
    merge_contributor_images,
    normalize_generative_ai,
    request_with_fallback,
    resolve_tas_enrichment_merge_key,
)


_ALLOWED_CT = {"all", "photo", "illustration", "vector", "video", "template", "3d", "audio"}


def normalize_content_type_csv(value: Optional[str]) -> str:
    """Split comma-separated content types; validate each token; return CSV or 'all'."""
    raw = str(value or "all").strip()
    if not raw or raw.lower() == "all":
        return "all"
    parts: List[str] = []
    for seg in raw.split(","):
        t = seg.strip().lower()
        if t in _ALLOWED_CT and t != "all":
            if t not in parts:
                parts.append(t)
    if not parts:
        return "all"
    return ",".join(parts)


def build_search_referer(
    q: str,
    order: str,
    page: int,
    content_type_csv: str,
    generative_ai: str,
) -> str:
    ga = normalize_generative_ai(generative_ai)
    ct = normalize_content_type_csv(content_type_csv)
    qs: Dict[str, str] = {"q": q, "order": order, "content_type": ct}
    if ga != "all":
        qs["generative_ai"] = ga
    if page > 1:
        qs["page"] = str(page)
    return f"{BASE}/search?{urlencode(qs)}"


def _search_api_params(
    q: str,
    order: str,
    page: int,
    generative_ai: str,
    content_type_csv: str,
) -> Dict[str, str]:
    ga = normalize_generative_ai(generative_ai)
    ct = normalize_content_type_csv(content_type_csv)
    params: Dict[str, str] = {"q": q, "order": order, "content_type": ct}
    if ga != "all":
        params["generative_ai"] = ga
    if page > 1:
        params["page"] = str(page)
    return params


def fetch_tas_search_page(
    q: str,
    order: str,
    page: int,
    generative_ai: str,
    content_type_csv: str,
    cookie: str,
    csr_token: str,
    turnstile_token: Optional[str],
    device_id: str,
    impersonate: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    params = _search_api_params(q, order, page, generative_ai, content_type_csv)
    qs = urlencode(params)
    url = f"{BASE}/api/search?{qs}"
    headers: Dict[str, str] = {
        "Accept": "*/*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Cookie": cookie,
        "Referer": build_search_referer(q, order, page, content_type_csv, generative_ai),
        "User-Agent": DEFAULT_UA,
        "X-Requested-With": "XMLHttpRequest",
        "X-CSR-Token": csr_token,
        "x-device-id": device_id,
        "X-Device-Id": device_id,
    }
    if turnstile_token:
        headers["X-Turnstile-Token"] = turnstile_token
    try:
        r = request_with_fallback(
            "get",
            url,
            headers=headers,
            impersonate=impersonate,
            timeout=60,
        )
    except Exception as exc:
        return None, str(exc)
    try:
        body = r.json()
    except Exception:
        return None, f"non-JSON body HTTP {r.status_code}: {r.text[:400]}"
    if not r.ok:
        return body, f"HTTP {r.status_code}"
    if looks_unauthorized(body):
        return body, "unauthorized body"
    return body, ""


def fetch_tas_search_raw(
    q: str,
    order: str,
    pages: List[int],
    generative_ai: str,
    content_type_csv: str,
    cookie: str,
    device_id: str,
    device_token: Optional[str],
    turnstile_token: Optional[str],
    tls_profile: str,
    csr_cache: CsrTokenCache,
    csr_token_override: Optional[str] = None,
    enrichment_key: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """
    Fetch one or more search pages; merge ``images`` (including encodedEnrichment
    XOR merge per page); return upstream-shaped dict.

    enrichment_key: explicit XOR key; else TAS_ENRICHMENT_KEY env; else response
    enrichmentKey; else one GET /api/auth/session (cached for the run).
    """
    if not pages:
        return {}, "no pages"
    impersonate = (tls_profile or "").strip() or DEFAULT_TLS_PROFILE
    csr_referer = build_search_referer(q, order, pages[0], content_type_csv, generative_ai)
    resolved_key = (enrichment_key or "").strip() or None
    if not resolved_key:
        resolved_key = (os.environ.get("TAS_ENRICHMENT_KEY") or "").strip() or None
    session_fetched = False
    merged: Optional[Dict[str, Any]] = None
    all_images: List[Dict[str, Any]] = []

    for page in pages:
        csr, err = ensure_csr_token(
            csr_cache, cookie, impersonate, device_token, csr_token_override, csr_referer
        )
        if err:
            return {}, err
        body, err = fetch_tas_search_page(
            q,
            order,
            page,
            generative_ai,
            content_type_csv,
            cookie,
            csr,
            turnstile_token,
            device_id,
            impersonate,
        )
        if err:
            return {}, f"page={page}: {err}"
        assert body is not None
        if not isinstance(body, dict):
            return {}, f"page={page}: response body is not an object"

        merge_key, resolved_key, session_fetched = resolve_tas_enrichment_merge_key(
            body, resolved_key, session_fetched, cookie, device_id, impersonate
        )
        page_images = merge_contributor_images(body, merge_key)

        if merged is None:
            merged = copy.deepcopy(body)
        all_images.extend(page_images)

    if merged is None:
        return {}, "empty response"
    merged["images"] = all_images
    merged.pop("encodedEnrichment", None)
    merged.pop("enrichmentKey", None)
    return merged, ""
