"""Runway web-task integration used by sub2gen."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

from curl_cffi.requests import AsyncSession

from ..core.config import config as global_config
from ..core.database import Database
from ..core.logger import debug_logger
from ..core.models import RunwayAccount, RunwayModel, RunwayTask
from ..core.runway_manifest import runway_manifest_entry
from .file_cache import FileCache


RUNWAY_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
RUNWAY_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"}
RUNWAY_UPLOAD_PART_SIZE = 64 * 1024 * 1024

ASPECT_RATIO_DIMS = {
    "21:9": (1344, 576),
    "16:9": (1280, 720),
    "4:3": (1024, 768),
    "3:2": (1152, 768),
    "5:4": (1024, 819),
    "1:1": (960, 960),
    "4:5": (819, 1024),
    "3:4": (768, 1024),
    "2:3": (768, 1152),
    "9:16": (720, 1280),
}

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".m4v"}


@dataclass
class RunwayCredential:
    bearer_token: str
    workspace_id: Optional[str] = None
    team_id: Optional[str] = None


class RunwayService:
    """Create, poll, upload, and normalize Runway web-app tasks."""

    def __init__(self, db: Database, file_cache: FileCache, proxy_manager=None):
        self.db = db
        self.file_cache = file_cache
        self.proxy_manager = proxy_manager

    @staticmethod
    def is_runway_model(model: str) -> bool:
        return bool((model or "").strip().startswith("runway-"))

    @staticmethod
    def _json_loads(raw: Any, fallback: Any) -> Any:
        if isinstance(raw, (dict, list)):
            return raw
        text = str(raw or "").strip()
        if not text:
            return fallback
        try:
            return json.loads(text)
        except Exception:
            return fallback

    @staticmethod
    def _json_dumps(value: Any, fallback: Any) -> str:
        if value is None:
            value = fallback
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _decode_jwt_payload(token: str) -> Dict[str, Any]:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
            data = json.loads(decoded.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @classmethod
    def normalize_credential(cls, raw_credential: str) -> RunwayCredential:
        raw = (raw_credential or "").strip()
        if not raw:
            raise ValueError("Runway credential is empty")

        token = raw if raw.count(".") == 2 and raw.startswith("eyJ") else ""
        if not token:
            match = RUNWAY_JWT_RE.search(raw)
            token = match.group(0) if match else ""
        if not token:
            raise ValueError("Could not find a Runway JWT in the credential")

        payload = cls._decode_jwt_payload(token)
        workspace_id = payload.get("workspaceId") or payload.get("workspace_id") or payload.get("id") or payload.get("sub")
        team_id = payload.get("teamId") or payload.get("team_id") or workspace_id
        return RunwayCredential(
            bearer_token=token,
            workspace_id=str(workspace_id) if workspace_id is not None else None,
            team_id=str(team_id) if team_id is not None else None,
        )

    @classmethod
    def describe_credential(cls, raw_credential: str) -> Dict[str, Any]:
        try:
            cred = cls.normalize_credential(raw_credential)
            return {
                "workspace_id": cred.workspace_id,
                "team_id": cred.team_id,
                "status": "configured" if cred.workspace_id else "missing_workspace",
                "error": "" if cred.workspace_id else "JWT decoded but no workspace id was found",
            }
        except Exception as exc:
            return {
                "workspace_id": "",
                "team_id": "",
                "status": "invalid_credential",
                "error": str(exc),
            }

    @staticmethod
    def build_headers(
        credential: RunwayCredential,
        workspace_id: Optional[str] = None,
        *,
        require_workspace: bool = True,
    ) -> Dict[str, str]:
        wid = (workspace_id or credential.workspace_id or "").strip()
        if not wid and require_workspace:
            raise ValueError("Runway workspace id is required")
        headers = {
            "Authorization": f"Bearer {credential.bearer_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://app.runwayml.com",
            "Referer": "https://app.runwayml.com/",
        }
        if wid:
            headers["x-runway-workspace"] = wid
        return headers

    async def _request_proxy(self) -> Optional[str]:
        if not self.proxy_manager:
            return None
        try:
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                return await self.proxy_manager.get_request_proxy_url()
            if hasattr(self.proxy_manager, "get_proxy_url"):
                return await self.proxy_manager.get_proxy_url()
        except Exception as exc:
            debug_logger.log_warning(f"Runway proxy lookup failed: {exc}")
        return None

    @staticmethod
    def _team_id(account: RunwayAccount, credential: Optional[RunwayCredential] = None) -> Optional[str]:
        credential = credential or RunwayService.normalize_credential(account.raw_credential)
        return account.team_id or credential.team_id or account.workspace_id or credential.workspace_id

    @staticmethod
    def _team_id_value(account: RunwayAccount, credential: Optional[RunwayCredential] = None) -> Any:
        team_id = RunwayService._team_id(account, credential)
        return int(team_id) if str(team_id or "").isdigit() else team_id

    @staticmethod
    def _workspace_id(account: RunwayAccount, credential: Optional[RunwayCredential] = None) -> Optional[str]:
        credential = credential or RunwayService.normalize_credential(account.raw_credential)
        return account.workspace_id or credential.workspace_id

    async def _api_request(
        self,
        *,
        account: RunwayAccount,
        base_url: str,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        credential = self.normalize_credential(account.raw_credential)
        workspace_id = self._workspace_id(account, credential)
        headers = self.build_headers(credential, workspace_id=workspace_id)
        proxy = await self._request_proxy()
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        async with AsyncSession() as session:
            response = await session.request(
                method.upper(),
                url,
                headers=headers,
                json=json_body,
                params=params,
                timeout=timeout,
                proxy=proxy,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Runway {method.upper()} {path} failed HTTP {response.status_code}: {response.text[:500]}")
        if not response.text:
            return {}
        return response.json()

    async def _first_active_account(self) -> Optional[RunwayAccount]:
        accounts = await self.db.list_runway_accounts()
        for account in accounts:
            if account.is_active:
                return account
        return None

    @staticmethod
    def _normalize_teams_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        roles = {
            str(item.get("teamId")): str(item.get("role") or "")
            for item in payload.get("roles", [])
            if isinstance(item, dict) and item.get("teamId") is not None
        }
        organizations = {
            str(item.get("id")): item
            for item in payload.get("organizations", [])
            if isinstance(item, dict) and item.get("id") is not None
        }

        teams: List[Dict[str, Any]] = []
        for item in payload.get("teams", []):
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            team_id = str(item.get("id"))
            organization_id = str(item.get("organizationId") or "")
            organization = organizations.get(organization_id, {})
            teams.append(
                {
                    "id": team_id,
                    "username": str(item.get("username") or ""),
                    "team_name": str(item.get("teamName") or item.get("username") or f"Team {team_id}"),
                    "first_name": str(item.get("firstName") or ""),
                    "last_name": str(item.get("lastName") or ""),
                    "email": str(item.get("email") or ""),
                    "role": roles.get(team_id, ""),
                    "current_plan": str(item.get("currentPlan") or ""),
                    "plan_expiration": item.get("planExpiration"),
                    "gpu_credits": item.get("gpuCredits") or 0,
                    "organization_id": organization_id,
                    "organization_name": str(organization.get("name") or ""),
                }
            )

        return {
            "teams": teams,
            "organizations": [
                {"id": str(item.get("id")), "name": str(item.get("name") or "")}
                for item in payload.get("organizations", [])
                if isinstance(item, dict) and item.get("id") is not None
            ],
        }

    async def get_teams_for_credential(self, raw_credential: str, *, base_url: Optional[str] = None) -> Dict[str, Any]:
        credential = self.normalize_credential(raw_credential)
        config = await self.db.get_runway_config()
        headers = self.build_headers(credential, workspace_id=credential.workspace_id, require_workspace=False)
        proxy = await self._request_proxy()
        url = f"{(base_url or config.base_url).rstrip('/')}/teams"
        async with AsyncSession() as session:
            response = await session.request(
                "GET",
                url,
                headers=headers,
                timeout=30,
                proxy=proxy,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Runway GET /teams failed HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json() if response.text else {}
        normalized = self._normalize_teams_payload(payload if isinstance(payload, dict) else {})
        normalized["workspace_id"] = credential.workspace_id or ""
        normalized["team_id"] = credential.team_id or ""
        return normalized

    async def get_account_teams(self, account_id: int) -> Dict[str, Any]:
        account = await self.db.get_runway_account(account_id)
        if not account:
            raise ValueError("Runway account not found")
        return await self.get_teams_for_credential(account.raw_credential)

    @staticmethod
    def _extract_upstream_task_id(payload: Dict[str, Any]) -> str:
        task = payload.get("task") if isinstance(payload, dict) else None
        if isinstance(task, dict):
            for key in ("id", "taskId", "uuid"):
                value = task.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("id", "taskId"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise RuntimeError("Runway create task response did not include a task id")

    @staticmethod
    def normalize_status(raw_status: Any) -> str:
        status = str(raw_status or "").strip().upper()
        if status == "SUCCEEDED":
            return "completed"
        if status in {"FAILED", "CANCELLED", "REJECTED"}:
            return "failed"
        return "processing"

    @staticmethod
    def normalize_progress(task: Dict[str, Any]) -> int:
        raw = task.get("progressRatio", task.get("progress", 0))
        try:
            value = float(raw)
        except Exception:
            return 0
        if value <= 1:
            value *= 100
        return max(0, min(100, int(round(value))))

    @staticmethod
    def _walk_artifact_urls(value: Any, found: List[str]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {"url", "fifeurl", "fileurl", "downloadurl", "audiourl", "videourl"} and isinstance(item, str):
                    if item.startswith(("http://", "https://")) and item not in found:
                        found.append(item)
                else:
                    RunwayService._walk_artifact_urls(item, found)
        elif isinstance(value, list):
            for item in value:
                RunwayService._walk_artifact_urls(item, found)

    @classmethod
    def extract_artifact_urls(cls, payload: Dict[str, Any]) -> List[str]:
        task = payload.get("task") if isinstance(payload, dict) else None
        found: List[str] = []
        if isinstance(task, dict):
            cls._walk_artifact_urls(task.get("artifacts"), found)
            cls._walk_artifact_urls(task.get("outputs"), found)
            cls._walk_artifact_urls(task.get("output"), found)
            cls._walk_artifact_urls(task.get("generation"), found)
        cls._walk_artifact_urls(payload.get("artifacts"), found)
        cls._walk_artifact_urls(payload.get("output"), found)
        cls._walk_artifact_urls(payload.get("generation"), found)
        return found

    @staticmethod
    def _media_type_for_url(url: str, model_kind: str) -> str:
        suffix = Path(urlparse(url).path).suffix.lower()
        guessed, _ = mimetypes.guess_type(urlparse(url).path)
        if model_kind == "audio" or suffix in AUDIO_SUFFIXES or (guessed or "").startswith("audio/"):
            return "audio"
        if model_kind == "video" or suffix in VIDEO_SUFFIXES or (guessed or "").startswith("video/"):
            return "video"
        return "image"

    def _cache_url(self, filename: str, base_url: Optional[str]) -> str:
        base = (base_url or "").strip().rstrip("/")
        builder = getattr(self.file_cache, "build_url", None)
        if callable(builder):
            result = builder(filename, base)
            if isinstance(result, str):
                return result
        return f"{base}/api/cache/blob/{quote(filename, safe='')}" if base else f"/api/cache/blob/{quote(filename, safe='')}"

    async def _cache_artifacts(
        self,
        raw_urls: List[str],
        *,
        model_kind: str,
        api_key_id: Optional[int],
        base_url: Optional[str],
        enabled: bool,
    ) -> List[str]:
        if not enabled:
            return []
        cached: List[str] = []
        for raw_url in raw_urls:
            try:
                media_type = self._media_type_for_url(raw_url, model_kind)
                filename = await self.file_cache.download_and_cache(
                    raw_url,
                    media_type=media_type,
                    api_key_id=api_key_id,
                    token_id=None,
                    flow_project_id=None,
                )
                cached.append(self._cache_url(filename, base_url))
            except Exception as exc:
                debug_logger.log_warning(f"Runway artifact cache failed: {exc}")
        return cached

    async def create_upstream_task(self, *, account: RunwayAccount, payload: Dict[str, Any], base_url: str) -> Dict[str, Any]:
        return await self._api_request(account=account, base_url=base_url, method="POST", path="/tasks", json_body=payload, timeout=120)

    async def get_upstream_task(self, *, account: RunwayAccount, upstream_task_id: str, base_url: str) -> Dict[str, Any]:
        return await self._api_request(
            account=account,
            base_url=base_url,
            method="GET",
            path=f"/tasks/{upstream_task_id}",
            params={"asTeamId": self._team_id(account)},
            timeout=60,
        )

    async def get_upstream_task_generation(self, *, account: RunwayAccount, upstream_task_id: str, base_url: str) -> Dict[str, Any]:
        return await self._api_request(
            account=account,
            base_url=base_url,
            method="GET",
            path=f"/tasks/{upstream_task_id}/generation",
            params={"asTeamId": self._team_id(account)},
            timeout=60,
        )

    async def cancel_upstream_task(self, *, account: RunwayAccount, upstream_task_id: str, base_url: str) -> Dict[str, Any]:
        return await self._api_request(
            account=account,
            base_url=base_url,
            method="POST",
            path=f"/tasks/{upstream_task_id}/cancel",
            json_body={"asTeamId": self._team_id_value(account)},
            timeout=60,
        )

    async def get_profile_features(self, account: RunwayAccount, *, base_url: Optional[str] = None) -> Dict[str, Any]:
        config = await self.db.get_runway_config()
        return await self._api_request(
            account=account,
            base_url=base_url or config.base_url,
            method="GET",
            path="/profile/features",
            params={"asTeamId": self._team_id(account)},
            timeout=30,
        )

    async def get_voices(self) -> Dict[str, Any]:
        account = await self._first_active_account()
        if not account:
            raise RuntimeError("No active Runway account is available")
        config = await self.db.get_runway_config()
        return await self._api_request(
            account=account,
            base_url=config.base_url,
            method="GET",
            path="/generated_audio/voices",
            params={"asTeamId": self._team_id(account)},
            timeout=30,
        )

    async def sync_models(self) -> Dict[str, int]:
        account = await self._first_active_account()
        features: Dict[str, Any] = {}
        if account:
            try:
                features = await self.get_profile_features(account)
            except Exception as exc:
                debug_logger.log_warning(f"Runway feature sync failed; using manifest only: {exc}")
        return await self.db.sync_default_runway_models(features)

    @staticmethod
    def _is_task_type_disabled_error(error: Exception) -> bool:
        text = str(error or "").lower()
        return "task type is disabled for this user" in text

    async def test_account(self, account_id: int) -> Dict[str, Any]:
        account = await self.db.get_runway_account(account_id)
        if not account:
            raise ValueError("Runway account not found")
        config = await self.db.get_runway_config()
        try:
            await self.get_profile_features(account, base_url=config.base_url)
            status = "healthy"
            error = ""
        except Exception as exc:
            status = "failed"
            error = str(exc)
        await self.db.update_runway_account(account_id, last_status=status, last_error=error)
        return {"success": status == "healthy", "status": status, "error": error}

    @staticmethod
    def _truncate_name(prompt: str, prefix: str = "") -> str:
        text = " ".join((prompt or "").split())
        if prefix:
            text = f"{prefix} - {text}" if text else prefix
        return text[:96] or prefix or "Runway task"

    @staticmethod
    def _dimensions(aspect_ratio: Optional[str]) -> Tuple[int, int]:
        return ASPECT_RATIO_DIMS.get(str(aspect_ratio or "16:9"), ASPECT_RATIO_DIMS["16:9"])

    @staticmethod
    def _media_url(item: Dict[str, Any]) -> str:
        return str(item.get("url") or item.get("data_url") or item.get("uri") or "").strip()

    @staticmethod
    def _media_asset_id(item: Dict[str, Any]) -> str:
        return str(item.get("asset_id") or item.get("assetId") or item.get("id") or "").strip()

    @classmethod
    def _asset_ref(cls, item: Dict[str, Any], *, default_tag: Optional[str] = None) -> Dict[str, Any]:
        ref: Dict[str, Any] = {}
        asset_id = cls._media_asset_id(item)
        url = cls._media_url(item)
        if asset_id:
            ref["assetId"] = asset_id
        if url:
            ref["url"] = url
        name = item.get("name") or item.get("filename")
        if name:
            ref["name"] = str(name)
        tag = item.get("tag") or default_tag
        if tag:
            ref["tag"] = str(tag)
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            if "size" in metadata:
                ref["size"] = metadata.get("size")
            if "duration" in metadata:
                ref["duration"] = metadata.get("duration")
            if "instructions" in metadata:
                ref["instructions"] = metadata.get("instructions")
        return ref

    @staticmethod
    def _group_media(media: Optional[List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in media or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("type") or "").strip() or "reference_image"
            grouped.setdefault(role, []).append(item)
        return grouped

    @staticmethod
    def _first_media(grouped: Dict[str, List[Dict[str, Any]]], *roles: str) -> Optional[Dict[str, Any]]:
        for role in roles:
            items = grouped.get(role) or []
            if items:
                return items[0]
        return None

    @staticmethod
    def _model_defaults(model: RunwayModel) -> Dict[str, Any]:
        defaults = RunwayService._json_loads(model.default_options, {})
        return defaults if isinstance(defaults, dict) else {}

    @staticmethod
    def _merged_options(model: RunwayModel, options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        merged = dict(RunwayService._model_defaults(model))
        if isinstance(options, dict):
            merged.update(options)
        return merged

    @staticmethod
    def _apply_common_asset_group(options: Dict[str, Any]) -> None:
        asset_group_id = options.pop("asset_group_id", None) or options.get("assetGroupId")
        if asset_group_id:
            options["assetGroupId"] = asset_group_id

    def _build_gemini_image(
        self,
        model: RunwayModel,
        *,
        prompt: str,
        media: Optional[List[Dict[str, Any]]],
        aspect_ratio: Optional[str],
        image_size: Optional[str],
        num_outputs: Optional[int],
        seed: Optional[int],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        grouped = self._group_media(media)
        if prompt:
            opts["text_prompt"] = prompt
            opts.setdefault("name", self._truncate_name(prompt, "Nano Banana"))
        if aspect_ratio:
            opts["aspect_ratio"] = aspect_ratio
        if image_size:
            opts["image_size"] = image_size
        if num_outputs is not None:
            opts["num_images"] = max(1, int(num_outputs))
        if seed is not None:
            opts["seed"] = int(seed)
        refs = [self._asset_ref(item) for item in grouped.get("reference_image", [])]
        refs = [ref for ref in refs if ref]
        if refs:
            opts["reference_images"] = refs
        self._apply_common_asset_group(opts)
        return model.task_type, opts

    def _build_gen4_image(
        self,
        model: RunwayModel,
        *,
        prompt: str,
        media: Optional[List[Dict[str, Any]]],
        aspect_ratio: Optional[str],
        resolution: Optional[str],
        num_outputs: Optional[int],
        seed: Optional[int],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        grouped = self._group_media(media)
        refs = [self._asset_ref(item) for item in grouped.get("reference_image", [])]
        refs = [ref for ref in refs if ref]
        if prompt:
            opts["text_prompt"] = prompt
            opts.setdefault("name", self._truncate_name(prompt, "Gen-4 Image"))
        ratio = aspect_ratio or opts.get("aspect_ratio")
        if ratio:
            opts["aspect_ratio"] = ratio
            opts.setdefault("width", self._dimensions(str(ratio))[0])
            opts.setdefault("height", self._dimensions(str(ratio))[1])
        if resolution:
            opts["resolution"] = resolution
        if num_outputs is not None:
            opts["num_images"] = max(1, int(num_outputs))
        if seed is not None:
            opts["seed"] = int(seed)
        if refs:
            opts["reference_images"] = refs
        self._apply_common_asset_group(opts)
        return ("ref_image_to_image" if refs else "text_to_image"), opts

    def _build_gen45_video(
        self,
        model: RunwayModel,
        *,
        prompt: str,
        media: Optional[List[Dict[str, Any]]],
        aspect_ratio: Optional[str],
        duration: Optional[int],
        seed: Optional[int],
        sound: Optional[bool],
        fps: Optional[int],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        grouped = self._group_media(media)
        first = self._first_media(grouped, "first_frame", "reference_image")
        ratio = aspect_ratio or opts.pop("aspect_ratio", None) or "16:9"
        width, height = self._dimensions(str(ratio))
        opts.update({"width": width, "height": height})
        if prompt:
            opts["text_prompt"] = prompt
            opts.setdefault("name", self._truncate_name(prompt, "Gen-4.5"))
        if duration is not None:
            opts["seconds"] = int(duration)
        elif "duration" in opts and "seconds" not in opts:
            opts["seconds"] = int(opts.pop("duration"))
        if seed is not None:
            opts["seed"] = int(seed)
        if fps is not None:
            opts["fps"] = int(fps)
        if sound is not None:
            opts["route"] = "t2va" if sound else opts.get("route", "t2v")
        else:
            opts.setdefault("route", "t2v")
        if first:
            image_url = self._media_url(first)
            if image_url:
                opts["route"] = "k2v"
                opts["keyframes"] = [{"image": image_url, "timestamp": 0}]
        self._apply_common_asset_group(opts)
        return model.task_type, opts

    def _build_kling_video(
        self,
        model: RunwayModel,
        *,
        prompt: str,
        media: Optional[List[Dict[str, Any]]],
        aspect_ratio: Optional[str],
        duration: Optional[int],
        resolution: Optional[str],
        sound: Optional[bool],
        multi_shot: Optional[List[Dict[str, Any]]],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        grouped = self._group_media(media)
        if prompt:
            opts["textPrompt"] = prompt
            opts.setdefault("name", self._truncate_name(prompt, "Kling"))
        if duration is not None:
            opts["duration"] = int(duration)
        if aspect_ratio:
            opts["aspectRatio"] = aspect_ratio
        if resolution:
            opts["resolution"] = resolution
        provider = dict(opts.get("providerSettings") or {})
        if sound is not None:
            provider["sound"] = bool(sound)
        if multi_shot:
            provider["multiPrompt"] = multi_shot
        if provider:
            opts["providerSettings"] = provider
        refs: List[Dict[str, Any]] = []
        for role in ("first_frame", "last_frame", "reference_image"):
            for item in grouped.get(role, []):
                ref = self._asset_ref(item, default_tag=role)
                if ref:
                    refs.append(ref)
        if refs:
            opts["referenceImages"] = refs
        self._apply_common_asset_group(opts)
        return model.task_type, opts

    def _build_seedance_video(
        self,
        model: RunwayModel,
        *,
        prompt: str,
        media: Optional[List[Dict[str, Any]]],
        aspect_ratio: Optional[str],
        duration: Optional[int],
        resolution: Optional[str],
        sound: Optional[bool],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        grouped = self._group_media(media)
        if prompt:
            opts["textPrompt"] = prompt
            opts.setdefault("name", self._truncate_name(prompt, "Seedance"))
        if duration is not None:
            opts["duration"] = int(duration)
        if aspect_ratio:
            opts["aspectRatio"] = aspect_ratio
        if resolution:
            opts["resolution"] = resolution
        if sound is not None:
            opts["generateAudio"] = bool(sound)
        image_refs: List[Dict[str, Any]] = []
        role_type = {"first_frame": "first_frame", "last_frame": "end_frame", "reference_image": "reference"}
        for role, runway_type in role_type.items():
            for item in grouped.get(role, []):
                ref = self._asset_ref(item, default_tag=role)
                if ref:
                    ref["type"] = runway_type
                    image_refs.append(ref)
        if image_refs:
            opts["referenceImages"] = image_refs
        video_refs = [self._asset_ref(item, default_tag="reference_video") for item in grouped.get("reference_video", [])]
        video_refs = [ref for ref in video_refs if ref]
        if video_refs:
            opts["referenceVideos"] = video_refs
        audio_refs = [self._asset_ref(item, default_tag="reference_audio") for item in grouped.get("reference_audio", [])]
        audio_refs = [ref for ref in audio_refs if ref]
        if audio_refs:
            opts["referenceAudio"] = audio_refs
        self._apply_common_asset_group(opts)
        return model.task_type, opts

    def _build_veo_video(
        self,
        model: RunwayModel,
        *,
        prompt: str,
        media: Optional[List[Dict[str, Any]]],
        aspect_ratio: Optional[str],
        duration: Optional[int],
        sound: Optional[bool],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        grouped = self._group_media(media)
        if prompt:
            opts["textPrompt"] = prompt
            opts.setdefault("name", self._truncate_name(prompt, "Veo"))
        if aspect_ratio:
            opts["aspectRatio"] = aspect_ratio
        if duration is not None:
            opts["duration"] = int(duration)
        if sound is not None:
            opts["generateAudio"] = bool(sound)
        first = self._first_media(grouped, "first_frame", "reference_image")
        last = self._first_media(grouped, "last_frame")
        if first:
            ref = self._asset_ref(first, default_tag="first_frame")
            if ref:
                opts["firstFrame"] = ref
        if last:
            ref = self._asset_ref(last, default_tag="last_frame")
            if ref:
                opts["lastFrame"] = ref
        self._apply_common_asset_group(opts)
        return model.task_type, opts

    def _build_text_to_speech(
        self,
        model: RunwayModel,
        *,
        prompt: str,
        voice_id: Optional[str],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        if prompt:
            opts["text"] = prompt
            opts.setdefault("name", self._truncate_name(prompt, "Voiceover"))
        voice = voice_id or opts.get("voice_id") or opts.get("voiceId") or opts.get("runwayVoiceId")
        if voice:
            opts["voiceId"] = voice
            opts["runwayVoiceId"] = voice
        opts.pop("voice_id", None)
        self._apply_common_asset_group(opts)
        return model.task_type, opts

    def _build_speech_to_speech(
        self,
        model: RunwayModel,
        *,
        media: Optional[List[Dict[str, Any]]],
        duration: Optional[int],
        voice_id: Optional[str],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        grouped = self._group_media(media)
        audio = self._first_media(grouped, "input_audio", "reference_audio")
        if audio:
            opts["audio"] = self._media_url(audio)
            asset_id = self._media_asset_id(audio)
            if asset_id:
                opts["audio_asset_id"] = asset_id
            opts.setdefault("name", str(audio.get("name") or audio.get("filename") or "Speech to speech"))
        if voice_id:
            opts["voice_id"] = voice_id
        if duration is not None:
            opts["seconds"] = int(duration)
        asset_group_id = opts.pop("assetGroupId", None) or opts.pop("asset_group_id", None)
        if asset_group_id:
            opts["asset_group_id"] = asset_group_id
        return model.task_type, opts

    def _build_sound_effect(
        self,
        model: RunwayModel,
        *,
        prompt: str,
        duration: Optional[int],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        if prompt:
            opts["promptText"] = prompt
            opts.setdefault("name", self._truncate_name(prompt, "Sound"))
        if duration is not None:
            opts["duration"] = int(duration)
        self._apply_common_asset_group(opts)
        return model.task_type, opts

    def _build_image_upscale(
        self,
        model: RunwayModel,
        *,
        media: Optional[List[Dict[str, Any]]],
        upscale: Optional[Dict[str, Any]],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        if isinstance(upscale, dict):
            opts.update(upscale)
        grouped = self._group_media(media)
        image = self._first_media(grouped, "image_to_upscale", "reference_image")
        if image:
            opts["image"] = self._asset_ref(image, default_tag="image_to_upscale")
        if "scale" in opts and "scaleFactor" not in opts:
            opts["scaleFactor"] = opts.pop("scale")
        self._apply_common_asset_group(opts)
        return model.task_type, opts

    def _build_video_upscale(
        self,
        model: RunwayModel,
        *,
        media: Optional[List[Dict[str, Any]]],
        upscale: Optional[Dict[str, Any]],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        if isinstance(upscale, dict):
            opts.update(upscale)
        grouped = self._group_media(media)
        video = self._first_media(grouped, "video_to_upscale", "reference_video")
        if video:
            opts["video"] = self._asset_ref(video, default_tag="video_to_upscale")
        if "scale" in opts and "scaleFactor" not in opts:
            opts["scaleFactor"] = opts.pop("scale")
        self._apply_common_asset_group(opts)
        return model.task_type, opts

    def _build_talking_avatar(
        self,
        model: RunwayModel,
        *,
        prompt: str,
        media: Optional[List[Dict[str, Any]]],
        voice_id: Optional[str],
        options: Optional[Dict[str, Any]],
        **_: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        opts = self._merged_options(model, options)
        grouped = self._group_media(media)
        character = self._first_media(grouped, "character_image", "reference_image")
        audio = self._first_media(grouped, "input_audio", "reference_audio")
        if character:
            opts["character_image"] = self._asset_ref(character, default_tag="character_image")
        if audio:
            opts["input_audio"] = self._asset_ref(audio, default_tag="input_audio")
        elif prompt:
            opts["input_text"] = prompt
        voice = voice_id or opts.get("voice_id") or opts.get("voiceId") or opts.get("runwayVoiceId")
        if voice:
            opts["voice_config"] = {"type": "elevenlabs", "id": voice, "voice_id": voice}
        opts.pop("voice_id", None)
        self._apply_common_asset_group(opts)
        return model.task_type, opts

    def _builder_for(self, builder_key: str):
        builders = {
            "gemini_image": self._build_gemini_image,
            "gen4_image": self._build_gen4_image,
            "gen45_video": self._build_gen45_video,
            "kling_video": self._build_kling_video,
            "seedance_video": self._build_seedance_video,
            "veo_video": self._build_veo_video,
            "text_to_speech": self._build_text_to_speech,
            "speech_to_speech": self._build_speech_to_speech,
            "sound_effect": self._build_sound_effect,
            "image_upscale": self._build_image_upscale,
            "video_upscale": self._build_video_upscale,
            "talking_avatar": self._build_talking_avatar,
        }
        return builders.get((builder_key or "").strip())

    def build_task_payload(
        self,
        *,
        model: RunwayModel,
        account: RunwayAccount,
        prompt: str,
        media: Optional[List[Dict[str, Any]]] = None,
        mode: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        orientation: Optional[str] = None,
        duration: Optional[int] = None,
        resolution: Optional[str] = None,
        image_size: Optional[str] = None,
        num_outputs: Optional[int] = None,
        seed: Optional[int] = None,
        sound: Optional[bool] = None,
        fps: Optional[int] = None,
        voice_id: Optional[str] = None,
        multi_shot: Optional[List[Dict[str, Any]]] = None,
        upscale: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        builder_key = model.builder_key or (runway_manifest_entry(model.public_model_id) or {}).get("builder_key", "")
        builder = self._builder_for(builder_key)
        if not builder:
            raise RuntimeError(f"Runway model has no typed builder: {model.public_model_id}")

        task_type, opts = builder(
            model,
            prompt=prompt,
            media=media,
            mode=mode,
            aspect_ratio=aspect_ratio,
            orientation=orientation,
            duration=duration,
            resolution=resolution,
            image_size=image_size,
            num_outputs=num_outputs,
            seed=seed,
            sound=sound,
            fps=fps,
            voice_id=voice_id,
            multi_shot=multi_shot,
            upscale=upscale,
            options=options,
        )
        credential = self.normalize_credential(account.raw_credential)
        return {
            "taskType": task_type,
            "options": opts,
            "asTeamId": self._team_id_value(account, credential),
            "sessionId": str(uuid.uuid4()),
        }

    async def start_task(
        self,
        *,
        public_model_id: str,
        prompt: str,
        media: Optional[List[Dict[str, Any]]] = None,
        mode: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        orientation: Optional[str] = None,
        duration: Optional[int] = None,
        resolution: Optional[str] = None,
        image_size: Optional[str] = None,
        num_outputs: Optional[int] = None,
        seed: Optional[int] = None,
        sound: Optional[bool] = None,
        fps: Optional[int] = None,
        voice_id: Optional[str] = None,
        multi_shot: Optional[List[Dict[str, Any]]] = None,
        upscale: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
        api_key_id: Optional[int] = None,
    ) -> RunwayTask:
        config = await self.db.get_runway_config()
        if not config.enabled:
            raise RuntimeError("Runway integration is disabled")

        model = await self.db.get_runway_model(public_model_id)
        if not model or not model.is_enabled:
            raise RuntimeError(f"Runway model is not enabled or does not exist: {public_model_id}")
        if not model.live_available:
            raise RuntimeError(model.disabled_reason or f"Runway model is not available: {public_model_id}")

        account = await self.db.acquire_runway_account()
        if not account:
            raise RuntimeError("No active Runway account is available")

        job_id = f"runway-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        release_now = False
        try:
            payload = self.build_task_payload(
                model=model,
                account=account,
                prompt=prompt,
                media=media,
                mode=mode,
                aspect_ratio=aspect_ratio,
                orientation=orientation,
                duration=duration,
                resolution=resolution,
                image_size=image_size,
                num_outputs=num_outputs,
                seed=seed,
                sound=sound,
                fps=fps,
                voice_id=voice_id,
                multi_shot=multi_shot,
                upscale=upscale,
                options=options,
            )
            created = await self.create_upstream_task(account=account, payload=payload, base_url=config.base_url)
            upstream_task_id = self._extract_upstream_task_id(created)
            task = RunwayTask(
                job_id=job_id,
                upstream_task_id=upstream_task_id,
                account_id=account.id,
                api_key_id=api_key_id,
                public_model_id=public_model_id,
                prompt=prompt,
                status="processing",
                progress=0,
                request_payload=json.dumps(payload, ensure_ascii=False),
                response_payload=json.dumps(created, ensure_ascii=False),
            )
            await self.db.create_runway_task(task)
            await self.db.update_runway_account(account.id or 0, last_status="task_created", last_error="")
            return task
        except Exception as exc:
            release_now = True
            error_text = str(exc)
            if self._is_task_type_disabled_error(exc):
                error_text = "Runway task type is disabled for this user"
                await self.db.mark_runway_model_unavailable(public_model_id, error_text)
                await self.db.update_runway_account(account.id or 0, last_status="failed", last_error=error_text)
                raise RuntimeError(error_text) from exc
            await self.db.update_runway_account(account.id or 0, last_status="failed", last_error=error_text)
            raise
        finally:
            if release_now:
                await self.db.release_runway_account(account.id)

    async def poll_task(
        self,
        job_id: str,
        *,
        api_key_id: Optional[int] = None,
        base_url: Optional[str] = None,
    ) -> RunwayTask:
        task = await self.db.get_runway_task(job_id)
        if not task:
            raise KeyError("Runway job not found")
        if api_key_id is not None and task.api_key_id is not None and int(api_key_id) != int(task.api_key_id):
            raise PermissionError("Not authorized to view this Runway job")
        if task.status in {"completed", "failed", "cancelled"}:
            return task

        config = await self.db.get_runway_config()
        account = await self.db.get_runway_account(int(task.account_id or 0))
        if not account:
            await self.db.update_runway_task(job_id, status="failed", error_message="Runway account no longer exists", completed_at=datetime.utcnow())
            return await self.db.get_runway_task(job_id) or task

        try:
            payload = await self.get_upstream_task(
                account=account,
                upstream_task_id=task.upstream_task_id or "",
                base_url=config.base_url,
            )
            upstream_task = payload.get("task") if isinstance(payload.get("task"), dict) else payload
            raw_status = upstream_task.get("status") if isinstance(upstream_task, dict) else ""
            status = self.normalize_status(raw_status)
            progress = 100 if status == "completed" else self.normalize_progress(upstream_task if isinstance(upstream_task, dict) else {})
            if status == "completed":
                try:
                    generation_payload = await self.get_upstream_task_generation(
                        account=account,
                        upstream_task_id=task.upstream_task_id or "",
                        base_url=config.base_url,
                    )
                    payload["generation"] = generation_payload
                except Exception as exc:
                    debug_logger.log_warning(f"Runway generation detail fetch failed: {exc}")
            raw_urls = self.extract_artifact_urls(payload) if status == "completed" else []
            model = await self.db.get_runway_model(task.public_model_id)
            model_kind = model.kind if model else "image"
            cached_urls = await self._cache_artifacts(
                raw_urls,
                model_kind=model_kind,
                api_key_id=task.api_key_id,
                base_url=base_url,
                enabled=bool(global_config.cache_enabled and getattr(config, "cache_outputs", True)),
            ) if raw_urls else []
            error_message = ""
            if status == "failed":
                error_message = str(
                    upstream_task.get("failureReason")
                    or upstream_task.get("error")
                    or upstream_task.get("message")
                    or raw_status
                    or "Runway task failed"
                )
            terminal = status in {"completed", "failed"}
            await self.db.update_runway_task(
                job_id,
                status=status,
                progress=progress,
                raw_artifact_urls=raw_urls if raw_urls else None,
                cached_artifact_urls=cached_urls if cached_urls else None,
                response_payload=json.dumps(payload, ensure_ascii=False),
                error_message=error_message or None,
                completed_at=datetime.utcnow() if terminal else None,
            )
            if terminal:
                await self.db.release_runway_account(task.account_id)
            return await self.db.get_runway_task(job_id) or task
        except Exception as exc:
            await self.db.update_runway_task(
                job_id,
                status="failed",
                error_message=str(exc),
                completed_at=datetime.utcnow(),
            )
            await self.db.release_runway_account(task.account_id)
            return await self.db.get_runway_task(job_id) or task

    async def cancel_task(self, job_id: str, *, api_key_id: Optional[int] = None) -> RunwayTask:
        task = await self.db.get_runway_task(job_id)
        if not task:
            raise KeyError("Runway job not found")
        if api_key_id is not None and task.api_key_id is not None and int(api_key_id) != int(task.api_key_id):
            raise PermissionError("Not authorized to cancel this Runway job")
        if task.status in {"completed", "failed", "cancelled"}:
            return task
        config = await self.db.get_runway_config()
        account = await self.db.get_runway_account(int(task.account_id or 0))
        if not account:
            raise RuntimeError("Runway account no longer exists")
        payload = await self.cancel_upstream_task(account=account, upstream_task_id=task.upstream_task_id or "", base_url=config.base_url)
        await self.db.update_runway_task(
            job_id,
            status="cancelled",
            progress=task.progress,
            response_payload=json.dumps(payload, ensure_ascii=False),
            completed_at=datetime.utcnow(),
        )
        await self.db.release_runway_account(task.account_id)
        return await self.db.get_runway_task(job_id) or task

    async def wait_for_task(self, job_id: str, *, api_key_id: Optional[int], base_url: Optional[str]) -> RunwayTask:
        config = await self.db.get_runway_config()
        deadline = time.monotonic() + float(config.timeout_sec)
        task = await self.db.get_runway_task(job_id)
        while time.monotonic() < deadline:
            task = await self.poll_task(job_id, api_key_id=api_key_id, base_url=base_url)
            if task.status in {"completed", "failed", "cancelled"}:
                return task
            await asyncio.sleep(float(config.poll_interval_sec))
        await self.db.update_runway_task(
            job_id,
            status="failed",
            error_message=f"Runway task did not finish within {config.timeout_sec}s",
            completed_at=datetime.utcnow(),
        )
        if task:
            await self.db.release_runway_account(task.account_id)
        return await self.db.get_runway_task(job_id) or task

    async def estimate_task(
        self,
        *,
        public_model_id: str,
        prompt: str,
        media: Optional[List[Dict[str, Any]]] = None,
        mode: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        orientation: Optional[str] = None,
        duration: Optional[int] = None,
        resolution: Optional[str] = None,
        image_size: Optional[str] = None,
        num_outputs: Optional[int] = None,
        seed: Optional[int] = None,
        sound: Optional[bool] = None,
        fps: Optional[int] = None,
        voice_id: Optional[str] = None,
        multi_shot: Optional[List[Dict[str, Any]]] = None,
        upscale: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        account = await self._first_active_account()
        if not account:
            raise RuntimeError("No active Runway account is available")
        model = await self.db.get_runway_model(public_model_id)
        if not model:
            raise RuntimeError(f"Runway model does not exist: {public_model_id}")
        payload = self.build_task_payload(
            model=model,
            account=account,
            prompt=prompt,
            media=media,
            mode=mode,
            aspect_ratio=aspect_ratio,
            orientation=orientation,
            duration=duration,
            resolution=resolution,
            image_size=image_size,
            num_outputs=num_outputs,
            seed=seed,
            sound=sound,
            fps=fps,
            voice_id=voice_id,
            multi_shot=multi_shot,
            upscale=upscale,
            options=options,
        )
        config = await self.db.get_runway_config()
        body = {
            "feature": model.cost_feature or payload["taskType"],
            "count": max(1, int(num_outputs or payload["options"].get("num_images") or 1)),
            "asTeamId": self._team_id_value(account),
            "taskOptions": payload["options"],
            "route": payload["options"].get("route") or mode,
        }
        return await self._api_request(
            account=account,
            base_url=config.base_url,
            method="POST",
            path="/billing/estimate_feature_cost_credits",
            json_body=body,
            timeout=30,
        )

    async def _create_runway_upload(
        self,
        *,
        account: RunwayAccount,
        base_url: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> Dict[str, Any]:
        parts = [content[i:i + RUNWAY_UPLOAD_PART_SIZE] for i in range(0, len(content), RUNWAY_UPLOAD_PART_SIZE)] or [b""]
        create_payload = {"filename": filename, "numberOfParts": len(parts), "type": "DATASET"}
        upload = await self._api_request(account=account, base_url=base_url, method="POST", path="/uploads", json_body=create_payload, timeout=60)

        upload_id = str(upload.get("uploadId") or upload.get("id") or upload.get("uuid") or "").strip()
        upload_urls = upload.get("uploadUrls") or upload.get("urls") or []
        upload_headers = upload.get("uploadHeaders") or upload.get("headers") or {}
        if isinstance(upload_urls, dict):
            upload_urls = [upload_urls.get(str(i + 1)) or upload_urls.get(i + 1) for i in range(len(parts))]
        if not upload_id or not isinstance(upload_urls, list) or len(upload_urls) < len(parts):
            raise RuntimeError("Runway upload response did not include upload id and URLs")

        completed_parts: List[Dict[str, Any]] = []
        proxy = await self._request_proxy()
        async with AsyncSession() as session:
            for index, part in enumerate(parts, start=1):
                headers = dict(upload_headers if isinstance(upload_headers, dict) else {})
                headers.setdefault("Content-Type", content_type)
                response = await session.put(str(upload_urls[index - 1]), headers=headers, data=part, timeout=120, proxy=proxy)
                if response.status_code >= 400:
                    raise RuntimeError(f"Runway upload part {index} failed HTTP {response.status_code}: {response.text[:240]}")
                etag = response.headers.get("ETag") or response.headers.get("etag") or ""
                completed_parts.append({"PartNumber": index, "ETag": etag.strip('"')})

        complete_payload = {"parts": completed_parts}
        completed = await self._api_request(
            account=account,
            base_url=base_url,
            method="POST",
            path=f"/uploads/{upload_id}/complete",
            json_body=complete_payload,
            timeout=60,
        )
        return {"upload": upload, "upload_id": upload_id, "parts": completed_parts, "complete": completed}

    async def upload_media(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str,
        api_key_id: Optional[int],
        base_url: Optional[str],
        media_role: str = "reference_image",
        asset_group_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not content:
            raise ValueError("Uploaded file is empty")
        config = await self.db.get_runway_config()
        if not config.enabled:
            raise RuntimeError("Runway integration is disabled")
        account = await self._first_active_account()
        if not account:
            raise RuntimeError("No active Runway account is available")

        filename = Path(filename or "upload.bin").name
        suffix = Path(filename).suffix.lower()
        if not suffix:
            suffix = mimetypes.guess_extension(content_type) or ".bin"
            filename = f"{Path(filename).stem or 'upload'}{suffix}"
        content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        upload_info = await self._create_runway_upload(
            account=account,
            base_url=config.base_url,
            filename=filename,
            content=content,
            content_type=content_type,
        )
        upload_id = upload_info["upload_id"]
        media_type = self._media_type_for_url(filename, "audio" if content_type.startswith("audio/") else "video" if content_type.startswith("video/") else "image")
        dataset_body = {
            "fileCount": 1,
            "name": Path(filename).stem,
            "uploadId": upload_id,
            "previewUploadIds": [],
            "metadata": {
                "filename": filename,
                "contentType": content_type,
                "size": len(content),
                "mediaType": media_type,
                **(metadata or {}),
            },
            "asTeamId": self._team_id_value(account),
            "type": {"name": Path(filename).stem, "type": media_type.upper(), "isDirectory": False},
        }
        if asset_group_id:
            dataset_body["parentAssetGroupId"] = asset_group_id
        dataset = await self._api_request(
            account=account,
            base_url=config.base_url,
            method="POST",
            path="/datasets",
            json_body=dataset_body,
            timeout=60,
        )

        safe_name = f"runway_upload_{uuid.uuid4().hex}{suffix}"
        await self.file_cache.store_bytes(
            safe_name,
            content,
            content_type,
            api_key_id=api_key_id,
            media_type=media_type,
            source_url="runway-upload",
        )
        cached_url = self._cache_url(safe_name, base_url)

        asset_id = str(
            dataset.get("id")
            or dataset.get("assetId")
            or dataset.get("datasetId")
            or dataset.get("asset", {}).get("id")
            or ""
        )
        files = dataset.get("files")
        first_file = files[0] if isinstance(files, list) and files and isinstance(files[0], dict) else {}
        asset_url = str(dataset.get("url") or dataset.get("asset", {}).get("url") or first_file.get("url") or "")
        return {
            "success": True,
            "filename": safe_name,
            "url": cached_url,
            "cached_url": cached_url,
            "data_url": f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}",
            "content_type": content_type,
            "size": len(content),
            "media_type": media_type,
            "role": media_role,
            "asset_id": asset_id,
            "asset_url": asset_url,
            "upload_id": upload_id,
            "dataset": dataset,
            "metadata": dataset_body["metadata"],
        }

    @staticmethod
    def task_to_public_dict(task: RunwayTask) -> Dict[str, Any]:
        return {
            "job_id": task.job_id,
            "upstream_task_id": task.upstream_task_id,
            "status": task.status,
            "progress": task.progress,
            "model": task.public_model_id,
            "raw_artifact_urls": task.raw_artifact_urls or [],
            "cached_artifact_urls": task.cached_artifact_urls or [],
            "result_urls": task.cached_artifact_urls or task.raw_artifact_urls or [],
            "error_message": task.error_message,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        }

    @staticmethod
    def task_to_openai_payload(task: RunwayTask) -> Dict[str, Any]:
        if task.status in {"failed", "cancelled"}:
            return {
                "error": {
                    "message": task.error_message or f"Runway task {task.status}",
                    "type": "server_error",
                    "code": "runway_generation_failed",
                    "status_code": 502,
                },
                "job_id": task.job_id,
                "raw_artifact_urls": task.raw_artifact_urls or [],
            }
        urls = task.cached_artifact_urls or task.raw_artifact_urls or []
        parts: List[str] = []
        for url in urls:
            suffix = Path(urlparse(url).path).suffix.lower()
            if suffix in VIDEO_SUFFIXES:
                parts.append(f"<video src='{url}' controls></video>")
            elif suffix in AUDIO_SUFFIXES:
                parts.append(f"[Generated Audio]({url})")
            else:
                parts.append(f"![Generated Image]({url})")
        content = "\n".join(parts) if parts else "Runway task completed without artifact URLs."
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": task.public_model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "job_id": task.job_id,
            "raw_artifact_urls": task.raw_artifact_urls or [],
            "cached_artifact_urls": task.cached_artifact_urls or [],
        }
