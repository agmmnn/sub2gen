"""
Direct HTTP client for tastracker.com contributor-search (no browser).

Mirrors upstream secureHeaders / csr-token flow (webpack 7548).
Used by TaskTrackerService and optionally scripts/fetch_contributor_tas.py.

Keywords enrichment (HAR / ContributorDataLoader, module ~3038): contributor
GET /api/contributor-search and keyword GET /api/search may return images[] with
empty keywords plus encodedEnrichment (Base64). XOR with UTF-8(enrichmentKey),
JSON-parse to a map keyed by asset id; each entry {d,k,c} merges into downloads,
keywords, creatorId. Key matches the logged-in session user id (NextAuth).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"(?i)\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)

HOST = "tastracker.com"
BASE = f"https://{HOST}"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
CSR_REFRESH_REMAINING_MS = 120_000  # 12e4 in upstream bundle (module 9329)
DEFAULT_TLS_PROFILE = "chrome124"


def impersonation_candidates(preferred: str) -> List[Optional[str]]:
    out: List[Optional[str]] = []
    seen: set[Optional[str]] = set()
    for candidate in (preferred, "chrome124", "chrome120", "chrome116", None):
        c = candidate.strip() if isinstance(candidate, str) else None
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def request_with_fallback(method: str, url: str, **kwargs: Any):
    preferred = str(kwargs.pop("impersonate", "") or "").strip()
    errors: List[str] = []
    for imp in impersonation_candidates(preferred):
        call_kwargs = dict(kwargs)
        if imp:
            call_kwargs["impersonate"] = imp
        try:
            return getattr(curl_requests, method)(url, **call_kwargs)
        except Exception as exc:
            msg = str(exc)
            if "Impersonating " in msg and " is not supported" in msg:
                errors.append(msg)
                continue
            raise
    raise RuntimeError("; ".join(errors) if errors else "no working impersonation profile")


def normalize_generative_ai(value: Optional[str]) -> str:
    raw = str(value or "all").strip().lower()
    if raw in {"all", "any"}:
        return "all"
    if raw in {"ai_only", "only_ai", "ai", "gen_ai"}:
        return "only"
    if raw in {"exclude_ai", "non_ai", "no_ai", "human_only"}:
        return "exclude"
    return "all"


def normalize_content_type(value: Optional[str]) -> str:
    raw = str(value or "all").strip().lower()
    allowed = {"all", "photo", "illustration", "vector", "video", "template", "3d", "audio"}
    return raw if raw in allowed else "all"


def build_referer(
    search_id: str,
    order: str,
    page: int,
    generative_ai: str = "all",
    content_type: str = "all",
) -> str:
    ga = normalize_generative_ai(generative_ai)
    ct = normalize_content_type(content_type)
    qs: Dict[str, str] = {
        "search": search_id,
        "order": order,
        "content_type": ct,
    }
    if ga != "all":
        qs["generative_ai"] = ga
    if page > 1:
        qs["page"] = str(page)
    return f"{BASE}/contributor?{urlencode(qs)}"


def map_image(img: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(img, dict):
        return None
    img_id = str(img.get("id") or img.get("assetId") or "").strip()
    if not img_id:
        return None
    return {
        "id": img_id,
        "title": str(img.get("title") or ""),
        "downloads": int(img.get("downloads") or 0),
        "keywords": str(img.get("keywords") or ""),
        "imageUrl": str(img.get("thumbnailUrl") or img.get("imageUrl") or ""),
        "dimensions": str(img.get("dimensions") or ""),
        "mediaType": str(img.get("mediaType") or ""),
        "contentType": str(img.get("contentType") or ""),
        "category": str(img.get("category") or ""),
        "premium": str(img.get("premium") or ""),
        "updatedAt": str(img.get("creationDate") or img.get("updatedAt") or ""),
        "isAI": bool(img.get("isAI")),
        "creator": str(img.get("creator") or ""),
    }


def looks_unauthorized(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    err = str(body.get("error") or body.get("message") or "").lower()
    return bool(
        "unauthor" in err
        or "forbidden" in err
        or ("not" in err and "signed" in err and "in" in err)
        or ("please" in err and "log" in err)
        or "session expired" in err
    )


def decode_tas_enrichment(encoded_b64: str, enrichment_key: str) -> Optional[Dict[str, Any]]:
    """Base64 → XOR with UTF-8 key (repeating) → UTF-8 JSON object keyed by asset id."""
    try:
        raw = base64.b64decode(encoded_b64, validate=False)
        key_bytes = enrichment_key.encode("utf-8")
        if not key_bytes:
            return None
        kl = len(key_bytes)
        out = bytes(raw[i] ^ key_bytes[i % kl] for i in range(len(raw)))
        parsed = json.loads(out.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _apply_enrichment_patch(img: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(img)
    if "d" in patch:
        out["downloads"] = patch["d"]
    if "k" in patch:
        out["keywords"] = patch["k"]
    if "c" in patch:
        out["creatorId"] = patch["c"]
    return out


def merge_contributor_images(body: Dict[str, Any], enrichment_key: Optional[str]) -> List[Dict[str, Any]]:
    """Merge encodedEnrichment into images; unchanged if blob or key missing or decode fails."""
    imgs = body.get("images")
    if not isinstance(imgs, list) or not imgs:
        return list(imgs) if isinstance(imgs, list) else []

    enc = str(body.get("encodedEnrichment") or "").strip()
    key = (enrichment_key or "").strip()

    if not enc:
        return list(imgs)

    if not key:
        logger.warning(
            "TAS response has encodedEnrichment but no enrichment key; keywords left unchanged"
        )
        return [dict(row) if isinstance(row, dict) else row for row in imgs]

    payload = decode_tas_enrichment(enc, key)
    if payload is None:
        logger.warning(
            "TAS encodedEnrichment could not be decoded (wrong key or corrupt blob); keywords left unchanged"
        )
        return [dict(row) if isinstance(row, dict) else row for row in imgs]

    out: List[Dict[str, Any]] = []
    for row in imgs:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or row.get("assetId") or "").strip()
        patch: Any = None
        if rid:
            patch = payload.get(rid)
        if patch is None and row.get("id") is not None:
            patch = payload.get(str(row.get("id")))
        if patch is None and isinstance(row.get("id"), int):
            patch = payload.get(row["id"])
        if isinstance(patch, dict) and patch:
            out.append(_apply_enrichment_patch(row, patch))
        else:
            out.append(dict(row))
    return out


def resolve_tas_enrichment_merge_key(
    body: Dict[str, Any],
    resolved_key: Optional[str],
    session_fetched: bool,
    cookie: str,
    device_id: str,
    impersonate: str,
) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Per-page merge key for XOR decode. Order: cached resolved_key → body
    enrichmentKey → (if blob present) one session fetch. Updates cache when a
    new key is discovered.
    """
    merge_key = resolved_key
    new_resolved = resolved_key
    if not merge_key:
        ek = body.get("enrichmentKey")
        if isinstance(ek, str) and ek.strip():
            merge_key = ek.strip()
            new_resolved = merge_key
    enc_raw = body.get("encodedEnrichment")
    enc_str = str(enc_raw).strip() if enc_raw else ""
    new_sess = session_fetched
    if enc_str and not merge_key and not session_fetched:
        new_sess = True
        sk, _ = fetch_enrichment_key_from_session(cookie, device_id, impersonate)
        if sk:
            merge_key = sk
            new_resolved = sk
    return merge_key, new_resolved, new_sess


def _uuid_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s if _UUID_RE.match(s) else None


def extract_enrichment_key_from_session_body(data: Any) -> Optional[str]:
    """
    NextAuth /api/auth/session: prefer user.id, user.sub, then top-level id/sub,
    then depth-first search for a UUID-shaped string.
    """
    if not isinstance(data, dict):
        return None
    user = data.get("user")
    if isinstance(user, dict):
        for k in ("id", "sub", "userId"):
            u = _uuid_string(user.get(k))
            if u:
                return u
    for k in ("sub", "userId", "id"):
        u = _uuid_string(data.get(k))
        if u:
            return u
    stack: List[Any] = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for v in cur.values():
                if isinstance(v, str):
                    u = _uuid_string(v)
                    if u:
                        return u
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def fetch_enrichment_key_from_session(
    cookie: str,
    device_id: str,
    impersonate: str,
) -> Tuple[Optional[str], str]:
    """GET /api/auth/session with contributor-style device headers."""
    headers: Dict[str, str] = {
        "Accept": "*/*",
        "Cookie": cookie,
        "Referer": f"{BASE}/",
        "User-Agent": DEFAULT_UA,
        "x-device-id": device_id,
        "X-Device-Id": device_id,
    }
    try:
        r = request_with_fallback(
            "get",
            f"{BASE}/api/auth/session",
            headers=headers,
            impersonate=impersonate,
            timeout=60,
        )
    except Exception as exc:
        return None, str(exc)
    if not r.ok:
        return None, f"session HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception:
        return None, "session response is not JSON"
    key = extract_enrichment_key_from_session_body(data)
    if not key:
        return None, "session JSON had no UUID-shaped user id"
    return key, ""


class CsrTokenCache:
    """In-process cache mirroring tastracker client module 9329."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at_ms: float = 0.0

    def get_valid_token(self) -> Optional[str]:
        now = time.time() * 1000.0
        if not self._token or self._expires_at_ms <= now:
            return None
        remaining = self._expires_at_ms - now
        if remaining > CSR_REFRESH_REMAINING_MS:
            return self._token
        return None

    def set_from_response(self, token: str, expires_in: float) -> None:
        self._token = token
        self._expires_at_ms = time.time() * 1000.0 + float(expires_in)


def mint_csr_token(
    cookie: str,
    impersonate: str,
    device_token: Optional[str],
    csr_referer: str,
) -> Tuple[Optional[str], float, str]:
    headers: Dict[str, str] = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Cookie": cookie,
        "Origin": BASE,
        "Referer": csr_referer,
        "User-Agent": DEFAULT_UA,
    }
    if device_token:
        headers["X-Device-Token"] = device_token
    try:
        r = request_with_fallback(
            "post",
            f"{BASE}/api/auth/csr-token",
            headers=headers,
            json={},
            impersonate=impersonate,
            timeout=60,
        )
    except Exception as exc:
        return None, 0.0, f"csr-token request failed: {exc}"
    if not r.ok:
        return None, 0.0, f"csr-token HTTP {r.status_code}: {r.text[:500]}"
    try:
        data = r.json()
    except Exception:
        return None, 0.0, "csr-token response is not JSON"
    token = data.get("token")
    if not token:
        return None, 0.0, f"csr-token missing in JSON: {json.dumps(data)[:300]}"
    expires_in = data.get("expiresIn")
    if expires_in is None:
        return None, 0.0, "csr-token response missing expiresIn"
    return str(token), float(expires_in), ""


def ensure_csr_token(
    cache: CsrTokenCache,
    cookie: str,
    impersonate: str,
    device_token: Optional[str],
    csr_override: Optional[str],
    csr_referer: str,
) -> Tuple[str, str]:
    if csr_override:
        return csr_override, ""
    cached = cache.get_valid_token()
    if cached:
        return cached, ""
    token, expires_in, err = mint_csr_token(cookie, impersonate, device_token, csr_referer)
    if err or not token:
        return "", err or "empty csr token"
    cache.set_from_response(token, expires_in)
    return token, ""


def fetch_contributor_search_page(
    search_id: str,
    order: str,
    page: int,
    generative_ai: str,
    content_type: str,
    cookie: str,
    csr_token: str,
    turnstile_token: Optional[str],
    device_id: str,
    impersonate: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    ga = normalize_generative_ai(generative_ai)
    ct = normalize_content_type(content_type)
    params: Dict[str, str] = {
        "search": search_id,
        "order": order,
        "content_type": ct,
    }
    if ga != "all":
        params["generative_ai"] = ga
    if page > 1:
        params["page"] = str(page)
    qs = urlencode(params)
    url = f"{BASE}/api/contributor-search?{qs}"
    headers: Dict[str, str] = {
        "Accept": "*/*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Cookie": cookie,
        "Referer": build_referer(search_id, order, page, generative_ai, content_type),
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


def fetch_contributor_raw_images(
    search_id: str,
    order: str,
    pages: List[int],
    generative_ai: str,
    content_type: str,
    cookie: str,
    device_id: str,
    device_token: Optional[str],
    turnstile_token: Optional[str],
    tls_profile: str,
    csr_cache: CsrTokenCache,
    csr_token_override: Optional[str] = None,
    enrichment_key: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Fetch all pages; return flattened list of raw image dicts from API, or ([], error).

    enrichment_key: explicit XOR key; else TAS_ENRICHMENT_KEY env; else response
    enrichmentKey; else one GET /api/auth/session (cached for the run).
    """
    if not pages:
        return [], "no pages"
    impersonate = (tls_profile or "").strip() or DEFAULT_TLS_PROFILE
    csr_referer = build_referer(search_id, order, pages[0], generative_ai, content_type)
    resolved_key = (enrichment_key or "").strip() or None
    if not resolved_key:
        resolved_key = (os.environ.get("TAS_ENRICHMENT_KEY") or "").strip() or None
    session_fetched = False
    all_images: List[Dict[str, Any]] = []
    for page in pages:
        csr, err = ensure_csr_token(
            csr_cache, cookie, impersonate, device_token, csr_token_override, csr_referer
        )
        if err:
            return [], err
        body, err = fetch_contributor_search_page(
            search_id,
            order,
            page,
            generative_ai,
            content_type,
            cookie,
            csr,
            turnstile_token,
            device_id,
            impersonate,
        )
        if err:
            return [], f"page={page}: {err}"
        assert body is not None
        if not isinstance(body, dict):
            return [], f"page={page}: response body is not an object"

        merge_key, resolved_key, session_fetched = resolve_tas_enrichment_merge_key(
            body, resolved_key, session_fetched, cookie, device_id, impersonate
        )
        merged = merge_contributor_images(body, merge_key)
        all_images.extend(merged)
    return all_images, ""
