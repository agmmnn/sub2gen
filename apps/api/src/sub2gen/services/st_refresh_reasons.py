"""Shared human-readable hints for ST refresh reason codes.

The reason codes are produced by ``TokenManager._try_refresh_st`` (and its helpers)
and stored in ``TokenManager._last_st_refresh_reason``. They are consumed by the
manual ``/api/tokens/{id}/refresh-at`` endpoint (to surface a friendly toast) and
by the ST-only refresh scheduler (to surface a friendly warning log line).

Keeping a single source of truth here ensures both surfaces use identical wording.
"""
from typing import Final

ST_REFRESH_REASON_HINTS: Final[dict[str, str]] = {
    "not_attempted": "ST refresh was not attempted",
    "policy_skipped": "ST refresh skipped by policy",
    "disabled": "ST auto refresh is disabled",
    "extension_disabled": "extension ST refresh feature is disabled",
    "project_id_missing": "token has no project_id for ST refresh",
    "extension_no_worker_or_empty": "extension worker not connected or session unavailable",
    "extension_worker_offline": "extension refresh worker for this token ID is not connected",
    "extension_session_refresh_disabled": "ST refresh is disabled for the extension refresh worker bound to this token ID",
    "extension_timeout": "extension refresh timed out",
    "extension_error": "extension refresh encountered an internal error",
    "local_timeout": "local browser ST refresh timed out",
    "local_error": "local browser ST refresh failed",
    "local_timeout_after_extension": "local browser refresh timed out after extension attempt",
    "local_error_after_extension": "local browser refresh failed after extension attempt",
    "browser_profile_resource_exhausted": "browser profile runtime capacity is exhausted",
    "extension_and_local_failed": "both extension and local browser ST refresh failed",
    "extension_enabled_but_no_success": "extension refresh enabled but did not return a usable session",
    "same_st": "session token did not rotate (possibly expired login)",
    "st_refresh_exception": "unexpected ST refresh exception",
    "failed_without_reason": "ST refresh failed without detailed reason",
    "token_not_found": "token not found during refresh",
}


def describe_st_refresh_reason(code: str | None) -> str:
    """Return a human-readable hint for an ST refresh reason code.

    Falls back to the raw code when unknown so we never lose information,
    and returns an empty string for empty/None input.
    """
    normalized = (code or "").strip()
    if not normalized:
        return ""
    return ST_REFRESH_REASON_HINTS.get(normalized, normalized)
