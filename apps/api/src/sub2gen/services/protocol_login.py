"""NextAuth protocol login using exported Google cookies.

The redirect chain is deliberately restricted to Google Accounts and the
Labs callback host. Cookie values, session tokens, and proxy credentials are
never included in logs or returned error messages.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlparse, urlunparse

from curl_cffi.requests import AsyncSession

from ..core.logger import debug_logger


LABS_BASE = "https://labs.google/fx"
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"
GOOGLE_COOKIE_NAMES = frozenset({"SID", "HSID", "SSID", "APISID", "SAPISID"})
_ALLOWED_REDIRECT_HOSTS = frozenset({"accounts.google.com", "labs.google"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


def parse_google_cookies(raw: str) -> Dict[str, str]:
    """Parse common browser-export JSON shapes or a Cookie header string."""
    text = str(raw or "").strip()
    if not text:
        return {}

    try:
        data = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        data = None

    def from_items(items: Iterable[Any]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if name and value:
                result[name] = value
        return result

    if isinstance(data, list):
        parsed = from_items(data)
        if parsed:
            return parsed
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            parsed = from_items(data["cookies"])
            if parsed:
                return parsed
        parsed = {
            str(name).strip(): str(value).strip()
            for name, value in data.items()
            if isinstance(value, str) and str(name).strip() and value.strip()
        }
        if parsed:
            return parsed

    result: Dict[str, str] = {}
    for part in text.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name.strip() and value.strip():
            result[name.strip()] = value.strip()
    return result


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """Normalize supported proxy notations without logging credentials."""
    raw = str(proxy_url or "").strip()
    if not raw:
        return None

    st5_match = re.match(r"^st5\s+(.+)$", raw, re.IGNORECASE)
    if st5_match:
        raw = st5_match.group(1).strip()
        scheme = "socks5"
    else:
        scheme = "http"

    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"}:
            return None
        if not parsed.hostname or not parsed.port:
            return None
        return raw

    if "@" in raw:
        return f"{scheme}://{raw}"

    parts = raw.split(":")
    if len(parts) == 2 and parts[1].isdigit():
        return f"{scheme}://{raw}"
    if len(parts) >= 4 and parts[1].isdigit():
        host, port, username = parts[:3]
        password = ":".join(parts[3:])
        return f"{scheme}://{username}:{password}@{host}:{port}"
    return None


def _cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if name and value)


def _set_cookie_lines(headers: Any) -> List[str]:
    for method_name in ("get_list", "getlist"):
        method = getattr(headers, method_name, None)
        if callable(method):
            values = method("set-cookie") or method("Set-Cookie") or []
            if values:
                return [str(value) for value in values]
    value = headers.get("set-cookie") or headers.get("Set-Cookie")
    return [str(value)] if value else []


def _merge_response_cookies(cookies: Dict[str, str], headers: Any) -> None:
    for line in _set_cookie_lines(headers):
        name, separator, value = line.split(";", 1)[0].partition("=")
        if separator and name.strip():
            cookies[name.strip()] = value.strip()


def _extract_session_token(headers: Any) -> Optional[str]:
    prefix = f"{SESSION_COOKIE_NAME}="
    for line in _set_cookie_lines(headers):
        first = line.split(";", 1)[0].strip()
        if first.startswith(prefix):
            return first[len(prefix):].strip() or None
    return None


def _append_login_hint(url: str, email: Optional[str]) -> str:
    hint = str(email or "").strip()
    if not hint:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["login_hint"] = hint
    return urlunparse(parsed._replace(query=urlencode(query)))


def _extract_html_redirect(body: str) -> Optional[str]:
    text = str(body or "")
    patterns = (
        r'content\s*=\s*["\']?\d+\s*;\s*url\s*=\s*([^"\'>\s]+)',
        r'location(?:\.(?:href|replace))?\s*(?:\(|=)\s*["\']([^"\']+)',
        r'<form[^>]*action\s*=\s*["\']([^"\']+)',
        r'(https://labs\.google/fx/api/auth/callback/google[^"\'<>\s]*)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return unquote(match.group(1))
    return None


def _validate_redirect(url: str, *, callback_only: bool = False) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_REDIRECT_HOSTS:
        raise RuntimeError("OAuth returned a redirect outside the allowed Google hosts")
    if callback_only and not (
        parsed.hostname == "labs.google"
        and parsed.path.startswith("/fx/api/auth/callback/google")
    ):
        raise RuntimeError("OAuth callback URL was invalid")
    return parsed.geturl()


def _safe_error(exc: Exception) -> str:
    text = str(exc or type(exc).__name__)
    text = re.sub(r"(?i)(https?://|socks5h?://)([^/@\s]+)@", r"\1***@", text)
    text = re.sub(r"(?i)(cookie|token|authorization)=([^&\s]+)", r"\1=***", text)
    return text[:240]


class ProtocolLogin:
    """Exchange exported Google session cookies for a Labs NextAuth ST."""

    async def login(
        self,
        google_cookies_raw: str,
        proxy: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        google_cookies = parse_google_cookies(google_cookies_raw)
        if not GOOGLE_COOKIE_NAMES.intersection(google_cookies):
            return {
                "success": False,
                "error": "No supported Google session cookie was provided",
            }

        kwargs: Dict[str, Any] = {
            "impersonate": "chrome124",
            "trust_env": False,
        }
        normalized_proxy = normalize_proxy_url(proxy)
        if proxy and not normalized_proxy:
            return {"success": False, "error": "The protocol refresh proxy URL is invalid"}
        if normalized_proxy:
            kwargs["proxy"] = normalized_proxy

        labs_cookies: Dict[str, str] = {}
        try:
            async with AsyncSession(**kwargs) as session:
                csrf_response = await session.get(
                    f"{LABS_BASE}/api/auth/csrf",
                    allow_redirects=False,
                    timeout=30,
                )
                if csrf_response.status_code != 200:
                    return {"success": False, "error": f"CSRF request failed with HTTP {csrf_response.status_code}"}
                csrf_token = (csrf_response.json() or {}).get("csrfToken")
                if not csrf_token:
                    return {"success": False, "error": "CSRF response did not include a token"}
                _merge_response_cookies(labs_cookies, csrf_response.headers)

                signin_response = await session.post(
                    f"{LABS_BASE}/api/auth/signin/google",
                    data={"csrfToken": csrf_token, "callbackUrl": LABS_BASE, "json": "true"},
                    headers={
                        "Origin": "https://labs.google",
                        "Referer": LABS_BASE,
                        "Cookie": _cookie_header(labs_cookies),
                    },
                    allow_redirects=False,
                    timeout=30,
                )
                if signin_response.status_code != 200:
                    return {"success": False, "error": f"Signin request failed with HTTP {signin_response.status_code}"}
                _merge_response_cookies(labs_cookies, signin_response.headers)
                signin_data = signin_response.json() or {}
                current_url = _validate_redirect(
                    _append_login_hint(signin_data.get("redirect") or signin_data.get("url") or "", email)
                )

                callback_url: Optional[str] = None
                google_cookie_header = _cookie_header(google_cookies)
                for attempt in range(10):
                    oauth_response = await session.get(
                        current_url,
                        headers={
                            "Cookie": google_cookie_header,
                            "Referer": "https://labs.google/" if attempt == 0 else "https://accounts.google.com/",
                        },
                        allow_redirects=False,
                        timeout=30,
                    )
                    location = str(oauth_response.headers.get("location") or "").strip()
                    if not location and oauth_response.status_code == 200:
                        location = _extract_html_redirect(oauth_response.text or "") or ""
                    if not location:
                        return {
                            "success": False,
                            "error": f"OAuth did not return a redirect (HTTP {oauth_response.status_code})",
                        }
                    next_url = _validate_redirect(urljoin(current_url, location))
                    parsed_next = urlparse(next_url)
                    if parsed_next.hostname == "labs.google":
                        callback_url = _validate_redirect(next_url, callback_only=True)
                        break
                    current_url = next_url

                if not callback_url:
                    return {"success": False, "error": "OAuth did not reach the Labs callback"}

                callback_response = await session.get(
                    callback_url,
                    headers={
                        "Cookie": _cookie_header(labs_cookies),
                        "Referer": "https://accounts.google.com/",
                    },
                    allow_redirects=False,
                    timeout=30,
                )
                _merge_response_cookies(labs_cookies, callback_response.headers)
                session_token = _extract_session_token(callback_response.headers)

                for _ in range(5):
                    if session_token:
                        break
                    location = str(callback_response.headers.get("location") or "").strip()
                    if callback_response.status_code not in _REDIRECT_CODES or not location:
                        break
                    callback_url = _validate_redirect(urljoin(callback_url, location))
                    callback_response = await session.get(
                        callback_url,
                        headers={"Cookie": _cookie_header(labs_cookies)},
                        allow_redirects=False,
                        timeout=30,
                    )
                    _merge_response_cookies(labs_cookies, callback_response.headers)
                    session_token = _extract_session_token(callback_response.headers)

                if not session_token:
                    return {"success": False, "error": "Labs did not issue a session token"}
                return {"success": True, "session_token": session_token}
        except Exception as exc:
            safe_error = _safe_error(exc)
            debug_logger.log_error(f"[PROTOCOL_LOGIN] Login failed: {safe_error}")
            return {"success": False, "error": safe_error}


protocol_loginer = ProtocolLogin()
