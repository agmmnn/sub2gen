"""API routes for OpenAI-compatible and Gemini generateContent endpoints."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
import asyncio
import base64
import json
import mimetypes
import random
import re
import time
import uuid
from urllib.parse import parse_qs, urlparse

from curl_cffi.requests import AsyncSession
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..core import auth as auth_core
from ..core.auth import verify_api_key_flexible
from ..core.api_key_manager import AuthContext
from ..core.logger import debug_logger
from ..core.route_log_sanitize import dumps_for_request_log
from ..core.account_tiers import (
    PAYGATE_TIER_TWO,
    is_native_4k_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from ..core.model_resolver import get_base_model_aliases, resolve_model_name
from ..core.geminigen_manifest import GEMINIGEN_MODEL_MANIFEST, geminigen_manifest_entry
from ..core.studio_model_catalog import (
    geminigen_studio_metadata,
    native_studio_metadata,
    runway_studio_metadata,
)
from ..core.models import (
    ChatCompletionRequest,
    ChatMessage,
    GeminiContent,
    GeminiGenerateContentRequest,
    RunwayTask,
    GeminiGenTask,
)
from ..core.config import config as app_config
from ..services.cloning_metadata_service import CloningMetadataService
from ..services.generation_handler import MODEL_CONFIG, GenerationHandler
from ..services.geminigen_service import GeminiGenService
from ..services.llm_provider_chain import LlmProviderChain
from ..services.runway_service import RunwayService
from ..services.tas_tracker_service import TaskTrackerService

router = APIRouter()

MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\((.*?)\)")
HTML_VIDEO_RE = re.compile(r"<video[^>]+src=['\"](.*?)['\"]", re.IGNORECASE)
DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.DOTALL)
MEDIA_PROMPT_TOOL_BLOCK_RE = re.compile(r"<tools>.*?</tools>", re.IGNORECASE | re.DOTALL)
MEDIA_SYSTEM_INSTRUCTION_MARKERS = (
    "<tools>",
    "</tools>",
    "function calling ai model",
    "function signatures",
    "\"$schema\"",
    "\"additionalproperties\"",
)
MEDIA_PROMPT_PREAMBLE_PATTERNS = (
    re.compile(r"^you are a function calling ai model\.?$", re.IGNORECASE),
    re.compile(
        r"^you are provided with function signatures within .* xml tags\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^you may call one or more functions to assist with the user query\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^don't make assumptions about what values to plug into functions\.?$",
        re.IGNORECASE,
    ),
    re.compile(r"^here are the available tools:.*$", re.IGNORECASE),
)
GEMINI_STATUS_MAP = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ABORTED",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}

_llm_chain = LlmProviderChain()
cloning_metadata_service = CloningMetadataService(_llm_chain)
task_tracker_service = TaskTrackerService()


@dataclass
class NormalizedGenerationRequest:
    """Internal request shape shared by OpenAI and Gemini entrypoints."""

    model: str
    prompt: str
    images: List[bytes]
    messages: Optional[List[ChatMessage]] = None
    video_media_id: Optional[str] = None
    project_id: Optional[str] = None


def _strip_optional_project_id(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    return s or None


from ..transport.auth import (
    get_allowed_tokens,
    report_client_presence,
    router as auth_router,
)


router.include_router(auth_router)


LOG_OP_ADOBE_CLONING_PROMPTS = "adobe:cloning_prompts"
LOG_OP_ADOBE_CLONING_VIDEO = "adobe:cloning_video_prompt"
LOG_OP_ADOBE_METADATA = "adobe:metadata"
LOG_OP_ADOBE_TRACKER_CONTRIBUTOR = "adobe:tracker_contributor"
LOG_OP_ADOBE_TRACKER_KEYWORD = "adobe:tracker_keyword"


def _require_managed_scope(auth_ctx: AuthContext, scope_id: str) -> None:
    """Managed keys must include scope_id in their assigned scopes (legacy keys bypass)."""
    if auth_ctx.is_legacy:
        return
    if scope_id in auth_ctx.scopes:
        return
    raise HTTPException(
        status_code=403,
        detail=f"Missing scope: {scope_id}",
    )


def _require_runway_scope(auth_ctx: AuthContext) -> None:
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required for Runway")
    _require_managed_scope(auth_ctx, "runway:generate")


def _require_geminigen_scope(auth_ctx: AuthContext) -> None:
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required for GeminiGen")
    _require_managed_scope(auth_ctx, "geminigen:generate")


async def _logged_managed_adobe_call(
    auth_ctx: AuthContext,
    operation: str,
    request_payload: Any,
    coro_factory,
    database: Any,
):
    """Run async callable and persist one request_logs row (managed keys only)."""
    ldb = database
    started = time.perf_counter()
    status_code = 200
    response_payload: Any = None
    try:
        response_payload = await coro_factory()
        return response_payload
    except HTTPException as he:
        status_code = he.status_code
        response_payload = {"detail": he.detail}
        raise
    except asyncio.CancelledError:
        status_code = 499
        response_payload = {"detail": "Client disconnected before the Adobe request completed"}
        raise
    except Exception as exc:
        status_code = 500
        response_payload = {"error": str(exc)}
        raise
    finally:
        duration = max(0.0, time.perf_counter() - started)
        if auth_ctx.key_id is not None:
            if operation == LOG_OP_ADOBE_METADATA:
                try:
                    await ldb.increment_operation_stat(operation, success=status_code == 200)
                except Exception as stat_exc:
                    debug_logger.log_error(f"Managed Adobe metadata stats update failed: {stat_exc}")
            try:
                status_text = "completed" if status_code == 200 else "failed"
                await ldb.insert_managed_route_request_log(
                    api_key_id=int(auth_ctx.key_id),
                    operation=operation,
                    request_body=dumps_for_request_log(request_payload),
                    response_body=dumps_for_request_log(response_payload if response_payload is not None else {}),
                    status_code=status_code,
                    duration=duration,
                    status_text=status_text,
                )
            except Exception as log_exc:
                debug_logger.log_error(f"Managed Adobe route log insert failed: {log_exc}")


def _build_model_description(model_config: Dict[str, Any]) -> str:
    """Build a human-readable description for model listing endpoints."""
    description = f"{model_config['type'].capitalize()} generation"
    if model_config["type"] == "image":
        description += f" - {model_config['model_name']}"
    else:
        description += f" - {model_config['model_key']}"
    return description


async def _get_active_native_tokens(database: Any) -> List[Any]:
    return list(await database.get_active_tokens())


def _has_active_native_ultra_account(tokens: List[Any]) -> bool:
    return any(
        bool(getattr(token, "is_active", True))
        and normalize_user_paygate_tier(getattr(token, "user_paygate_tier", None))
        == PAYGATE_TIER_TWO
        for token in tokens
    )


async def _has_runway_accounts(service: RunwayService) -> bool:
    accounts = await service.db.list_runway_accounts()
    return any(
        bool(account.is_active) and bool((account.raw_credential or "").strip())
        for account in accounts
    )


async def _has_geminigen_accounts(service: GeminiGenService) -> bool:
    accounts = await service.db.list_geminigen_accounts()
    return any(
        bool(account.is_active) and bool((account.bearer_token or "").strip())
        for account in accounts
    )


async def _get_openai_model_catalog(database: Any) -> List[Dict[str, str]]:
    """Collect OpenAI-compatible model list entries."""
    active_tokens = await _get_active_native_tokens(database)
    if not active_tokens:
        return []
    include_4k = _has_active_native_ultra_account(active_tokens)
    return [
        {
            "id": model_id,
            "description": _build_model_description(model_config),
            "studio": native_studio_metadata(model_id, model_config),
        }
        for model_id, model_config in MODEL_CONFIG.items()
        if include_4k or not is_native_4k_model(model_id)
    ]


async def _get_runway_openai_model_catalog(service: RunwayService) -> List[Dict[str, str]]:
    cfg = await service.db.get_runway_config()
    if not cfg.enabled:
        return []
    if not await _has_runway_accounts(service):
        return []
    models = await service.db.list_runway_models(enabled_only=True)
    return [
        {
            "id": model.public_model_id,
            "description": f"Runway {model.kind} generation - {model.task_type}",
            "studio": runway_studio_metadata(model),
        }
        for model in models
        if bool(model.live_available)
    ]


async def _get_geminigen_openai_model_catalog(service: GeminiGenService) -> List[Dict[str, str]]:
    cfg = await service.db.get_geminigen_config()
    if not cfg.enabled:
        return []
    if not await _has_geminigen_accounts(service):
        return []
    return [
        {
            "id": item["id"],
            "description": f"GeminiGen {item['kind']} generation - {item['endpoint_type']}",
            "studio": geminigen_studio_metadata(item),
        }
        for item in GEMINIGEN_MODEL_MANIFEST
        if bool(getattr(cfg, "video_enabled", True)) or item.get("kind") != "video"
    ]


async def _get_gemini_model_catalog(database: Any, service: GeminiGenService) -> Dict[str, str]:
    """Collect Gemini-compatible model metadata for /models endpoints."""
    catalog: Dict[str, str] = {}

    active_tokens = await _get_active_native_tokens(database)
    if active_tokens:
        include_4k = _has_active_native_ultra_account(active_tokens)
        aliases = get_base_model_aliases(include_4k=include_4k)
        for alias_id, description in aliases.items():
            catalog[alias_id] = description

        for model_id, model_config in MODEL_CONFIG.items():
            if not include_4k and is_native_4k_model(model_id):
                continue
            catalog.setdefault(model_id, _build_model_description(model_config))

    for model in await _get_geminigen_openai_model_catalog(service):
        catalog.setdefault(model["id"], model["description"])

    return catalog


def _build_gemini_model_resource(model_id: str, description: str) -> Dict[str, Any]:
    """Build a Gemini-compatible model resource payload."""
    return {
        "name": f"models/{model_id}",
        "displayName": model_id,
        "description": description,
        "version": "sub2gen",
        "inputTokenLimit": 0,
        "outputTokenLimit": 0,
        "supportedGenerationMethods": [
            "generateContent",
            "streamGenerateContent",
        ],
    }


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    match = DATA_URL_RE.match(data_url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid data URL")
    return match.group("mime"), base64.b64decode(match.group("data"))


def _detect_image_mime_type(image_bytes: bytes, fallback: str = "image/png") -> str:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def _guess_mime_type(uri: str, fallback: str) -> str:
    guessed, _ = mimetypes.guess_type(urlparse(uri).path)
    return guessed or fallback


def _extract_cache_filename(url: str) -> Optional[str]:
    """Resolve filename from owner-scoped cache URLs (blob path; legacy /api/cache/file/ still accepted)."""
    path = urlparse(url).path
    for marker in ("/api/cache/blob/", "/api/cache/file/"):
        if marker not in path:
            continue
        filename = path.split(marker, 1)[-1].strip().split("/", 1)[0]
        if not filename:
            return None
        return Path(filename).name
    return None


async def retrieve_image_data(
    url: str,
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
) -> Optional[bytes]:
    """Read image bytes from protected cache endpoint or remote URL."""
    file_cache = getattr(handler, "file_cache", None)
    try:
        cache_filename = _extract_cache_filename(url)
        if cache_filename and file_cache and api_key_id is not None:
            db = getattr(handler, "db", None)
            if db is None:
                return None
            metadata = await db.get_cache_file_for_api_key(cache_filename, api_key_id)
            if not metadata:
                return None
            meta_flow = _strip_optional_project_id(metadata.get("flow_project_id"))
            if meta_flow:
                parsed = urlparse(url)
                q_vals = parse_qs(parsed.query).get("project_id") or []
                url_project = _strip_optional_project_id(q_vals[0] if q_vals else None)
                if not url_project or url_project != meta_flow:
                    return None
                proj = await db.get_project_by_id(url_project, api_key_id)
                if not proj:
                    return None
                if allowed_token_ids is not None and int(proj.token_id) not in allowed_token_ids:
                    return None
            filename = Path(cache_filename).name
            data = await file_cache.read_bytes(filename)
            if data:
                return data
    except Exception as exc:
        debug_logger.log_warning(f"[CONTEXT] 本地缓存读取失败: {str(exc)}")

    proxy_url = None
    try:
        if file_cache and hasattr(file_cache, "_resolve_download_proxy"):
            proxy_url = await file_cache._resolve_download_proxy("image")
    except Exception as exc:
        debug_logger.log_warning(f"[CONTEXT] 图片下载代理解析失败: {str(exc)}")

    try:
        async with AsyncSession() as session:
            response = await session.get(
                url,
                timeout=60,
                proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
                headers={
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Referer": "https://labs.google/",
                },
                impersonate="chrome120",
                verify=False,
            )
            if response.status_code == 200 and response.content:
                return response.content
            debug_logger.log_warning(
                f"[CONTEXT] 图片下载失败，状态码: {response.status_code}"
            )
    except Exception as exc:
        debug_logger.log_error(f"[CONTEXT] 图片下载异常: {str(exc)}")

    return None


async def _load_image_bytes_from_uri(
    uri: str,
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
) -> bytes:
    if not uri:
        raise HTTPException(status_code=400, detail="Image URI cannot be empty")

    if uri.startswith("data:image"):
        _, image_bytes = _decode_data_url(uri)
        return image_bytes

    if (
        uri.startswith("http://")
        or uri.startswith("https://")
        or "/api/cache/blob/" in uri
        or "/api/cache/file/" in uri
    ):
        image_bytes = await retrieve_image_data(
            uri, handler, api_key_id=api_key_id, allowed_token_ids=allowed_token_ids
        )
        if image_bytes:
            return image_bytes
        raise HTTPException(status_code=400, detail=f"Failed to load image from {uri}")

    raise HTTPException(status_code=400, detail=f"Unsupported image URI: {uri}")


def _coerce_gemini_contents(raw_contents: Optional[List[Any]]) -> List[GeminiContent]:
    contents: List[GeminiContent] = []
    for item in raw_contents or []:
        if isinstance(item, GeminiContent):
            contents.append(item)
        else:
            contents.append(GeminiContent.model_validate(item))
    return contents


def _extract_text_from_gemini_content(content: Optional[GeminiContent]) -> str:
    if content is None:
        return ""
    text_parts = [part.text.strip() for part in content.parts if part.text]
    return "\n".join(part for part in text_parts if part).strip()


def _should_ignore_media_system_instruction(system_instruction: str) -> bool:
    """Drop agent/tool scaffolding before sending media prompts upstream."""
    if not system_instruction:
        return False

    normalized = system_instruction.lower()
    if len(system_instruction) > 1200:
        return True

    return any(marker in normalized for marker in MEDIA_SYSTEM_INSTRUCTION_MARKERS)


def _sanitize_media_prompt(prompt: str) -> str:
    """Strip agent/tool scaffolding that image/video models cannot use."""
    if not prompt:
        return ""

    sanitized = MEDIA_PROMPT_TOOL_BLOCK_RE.sub(" ", prompt.strip())
    cleaned_lines: List[str] = []
    for raw_line in sanitized.splitlines():
        line = raw_line.strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        if any(pattern.fullmatch(line) for pattern in MEDIA_PROMPT_PREAMBLE_PATTERNS):
            continue
        cleaned_lines.append(line)

    sanitized = "\n".join(cleaned_lines).strip()
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()


async def _extract_prompt_and_images_from_openai_messages(
    messages: List[ChatMessage],
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
) -> tuple[str, List[bytes], Optional[str]]:
    last_message = messages[-1]
    content = last_message.content
    prompt_parts: List[str] = []
    images: List[bytes] = []
    video_media_id: Optional[str] = None

    if isinstance(content, str):
        prompt_parts.append(content)
    elif isinstance(content, list):
        for item in content:
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text", "").strip()
                if text:
                    prompt_parts.append(text)
            elif item_type == "image_url":
                image_url = item.get("image_url", {}).get("url", "")
                if image_url.startswith("extend://"):
                    video_media_id = image_url[len("extend://"):].strip() or None
                    continue
                images.append(
                    await _load_image_bytes_from_uri(
                        image_url,
                        handler,
                        api_key_id=api_key_id,
                        allowed_token_ids=allowed_token_ids,
                    )
                )

    prompt = "\n".join(part for part in prompt_parts if part).strip()
    return prompt, images, video_media_id


async def _append_openai_reference_images(
    model: str,
    messages: List[ChatMessage],
    images: List[bytes],
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
) -> List[bytes]:
    model_config = MODEL_CONFIG.get(model)
    if not model_config or model_config["type"] != "image" or len(messages) <= 1:
        return images

    debug_logger.log_info(f"[CONTEXT] 开始查找历史参考图，消息数量: {len(messages)}")

    for msg in reversed(messages[:-1]):
        if msg.role == "assistant" and isinstance(msg.content, str):
            matches = MARKDOWN_IMAGE_RE.findall(msg.content)
            if not matches:
                continue

            for image_url in reversed(matches):
                if (
                    not image_url.startswith("http")
                    and "/api/cache/blob/" not in image_url
                    and "/api/cache/file/" not in image_url
                ):
                    continue
                try:
                    downloaded_bytes = await retrieve_image_data(
                        image_url,
                        handler,
                        api_key_id=api_key_id,
                        allowed_token_ids=allowed_token_ids,
                    )
                    if downloaded_bytes:
                        images.insert(0, downloaded_bytes)
                        debug_logger.log_info(
                            f"[CONTEXT] ✅ 添加历史参考图: {image_url}"
                        )
                        return images
                    debug_logger.log_warning(
                        f"[CONTEXT] 图片下载失败或为空，尝试下一个: {image_url}"
                    )
                except Exception as exc:
                    debug_logger.log_error(
                        f"[CONTEXT] 处理参考图时出错: {str(exc)}"
                    )
    return images


async def _extract_prompt_and_images_from_gemini_contents(
    contents: List[GeminiContent],
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
) -> tuple[str, List[bytes]]:
    if not contents:
        raise HTTPException(status_code=400, detail="contents cannot be empty")

    target_content = next(
        (content for content in reversed(contents) if (content.role or "user") == "user"),
        contents[-1],
    )

    prompt_parts: List[str] = []
    images: List[bytes] = []

    for part in target_content.parts:
        if part.text:
            text = part.text.strip()
            if text:
                prompt_parts.append(text)
        elif part.inlineData is not None:
            mime_type = part.inlineData.mimeType.lower()
            if not mime_type.startswith("image/"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported inlineData mime type: {part.inlineData.mimeType}",
                )
            images.append(base64.b64decode(part.inlineData.data))
        elif part.fileData is not None:
            mime_type = (part.fileData.mimeType or "").lower()
            if mime_type and not mime_type.startswith("image/"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported fileData mime type: {part.fileData.mimeType}",
                )
            images.append(
                await _load_image_bytes_from_uri(
                    part.fileData.fileUri,
                    handler,
                    api_key_id=api_key_id,
                    allowed_token_ids=allowed_token_ids,
                )
            )

    prompt = "\n".join(part for part in prompt_parts if part).strip()
    return prompt, images


def _resolve_request_model(
    model: str,
    request: Any,
    images: Optional[List[bytes]] = None,
) -> str:
    if RunwayService.is_runway_model(model) or GeminiGenService.is_geminigen_model(model):
        return model
    resolved_model = resolve_model_name(
        model=model,
        request=request,
        model_config=MODEL_CONFIG,
        images=images,
    )
    if resolved_model != model:
        debug_logger.log_info(f"[ROUTE] 模型名已转换: {model} → {resolved_model}")
    return resolved_model


def _get_request_base_url(request: Request) -> Optional[str]:
    """根据实际请求头推导对外可访问的基础地址。"""
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = (forwarded_host or request.headers.get("host") or "").strip()

    if not host:
        return None

    proto = forwarded_proto or request.url.scheme or "http"
    return f"{proto}://{host}"


async def _normalize_openai_request(
    request: ChatCompletionRequest,
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
) -> NormalizedGenerationRequest:
    if request.messages:
        prompt, images, video_media_id = await _extract_prompt_and_images_from_openai_messages(
            request.messages,
            handler,
            api_key_id=api_key_id,
            allowed_token_ids=allowed_token_ids,
        )
        if request.image and not images:
            images.append(
                await _load_image_bytes_from_uri(
                    request.image,
                    handler,
                    api_key_id=api_key_id,
                    allowed_token_ids=allowed_token_ids,
                )
            )
        model = _resolve_request_model(request.model, request, images=images)
        initial_image_count = len(images)
        images = await _append_openai_reference_images(
            model,
            request.messages,
            images,
            handler,
            api_key_id=api_key_id,
            allowed_token_ids=allowed_token_ids,
        )
        if len(images) != initial_image_count:
            model = _resolve_request_model(request.model, request, images=images)
        return NormalizedGenerationRequest(
            model=model,
            prompt=prompt,
            images=images,
            messages=request.messages,
            video_media_id=video_media_id,
            project_id=_strip_optional_project_id(request.project_id),
        )

    if request.contents:
        gemini_request = GeminiGenerateContentRequest(
            contents=_coerce_gemini_contents(request.contents),
            generationConfig=request.generationConfig,
            project_id=request.project_id,
        )
        normalized = await _normalize_gemini_request(
            request.model,
            gemini_request,
            handler,
            api_key_id=api_key_id,
            allowed_token_ids=allowed_token_ids,
        )
        normalized.messages = request.messages
        if request.project_id is not None and normalized.project_id is None:
            normalized.project_id = _strip_optional_project_id(request.project_id)
        return normalized

    raise HTTPException(status_code=400, detail="Messages or contents cannot be empty")


async def _normalize_gemini_request(
    model: str,
    request: GeminiGenerateContentRequest,
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
) -> NormalizedGenerationRequest:
    prompt, images = await _extract_prompt_and_images_from_gemini_contents(
        request.contents,
        handler,
        api_key_id=api_key_id,
        allowed_token_ids=allowed_token_ids,
    )
    resolved_model = _resolve_request_model(model, request, images=images)
    system_instruction = _extract_text_from_gemini_content(request.systemInstruction)
    model_config = MODEL_CONFIG.get(resolved_model)
    media_model = bool(model_config and model_config.get("type") in {"image", "video"})

    if media_model:
        prompt = _sanitize_media_prompt(prompt)

    if system_instruction:
        if media_model and _should_ignore_media_system_instruction(system_instruction):
            debug_logger.log_warning(
                f"[GEMINI] 忽略媒体模型的 systemInstruction: model={resolved_model}, len={len(system_instruction)}"
            )
        else:
            if media_model:
                system_instruction = _sanitize_media_prompt(system_instruction)
            prompt = f"{system_instruction}\n\n{prompt}".strip()

    return NormalizedGenerationRequest(
        model=resolved_model,
        prompt=prompt,
        images=images,
        project_id=_strip_optional_project_id(request.project_id),
    )


async def _collect_non_stream_result(
    normalized: NormalizedGenerationRequest,
    handler: GenerationHandler,
    base_url_override: Optional[str] = None,
    allowed_token_ids: Optional[set[int]] = None,
    selection_context: Optional[Dict[str, Any]] = None,
    api_key_id: Optional[int] = None,
    poll_task_id: Optional[str] = None,
) -> str:
    result = None
    async for chunk in handler.handle_generation(
        model=normalized.model,
        prompt=normalized.prompt,
        images=normalized.images if normalized.images else None,
        stream=False,
        base_url_override=base_url_override,
        allowed_token_ids=allowed_token_ids,
        selection_context=selection_context,
        api_key_id=api_key_id,
        requested_project_id=normalized.project_id,
        video_media_id=normalized.video_media_id,
        poll_task_id=poll_task_id,
    ):
        result = chunk

    if result is None:
        raise HTTPException(status_code=500, detail="Generation failed: No response")

    return result


def _is_runway_model(model: str) -> bool:
    return RunwayService.is_runway_model(model)


def _is_geminigen_model(model: str) -> bool:
    return GeminiGenService.is_geminigen_model(model)


async def _require_geminigen_model_enabled(model: str, service: GeminiGenService) -> None:
    manifest = geminigen_manifest_entry(model or "")
    if not manifest or manifest.get("kind") != "video":
        return
    cfg = await service.db.get_geminigen_config()
    if not bool(getattr(cfg, "video_enabled", True)):
        raise HTTPException(status_code=404, detail="GeminiGen video mode is disabled")


def _request_extra_dict(request: Any) -> Dict[str, Any]:
    extra = getattr(request, "model_extra", None)
    data = dict(extra) if isinstance(extra, dict) else {}
    extra_body = data.get("extra_body")
    if isinstance(extra_body, dict):
        merged = dict(extra_body)
        merged.update({k: v for k, v in data.items() if k != "extra_body"})
        return merged
    return data


def _runway_media_from_images(images: List[bytes]) -> List[Dict[str, Any]]:
    media: List[Dict[str, Any]] = []
    for image in images or []:
        mime_type = _detect_image_mime_type(image)
        media.append(
            {
                "data_url": f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}",
            }
        )
    return media


def _runway_request_params_from_openai(
    request: ChatCompletionRequest,
    normalized: NormalizedGenerationRequest,
) -> Dict[str, Any]:
    def parse_bool(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return bool(value)

    extras = _request_extra_dict(request)
    gen_cfg = request.generationConfig
    image_cfg = getattr(gen_cfg, "imageConfig", None) if gen_cfg is not None else None
    aspect_ratio = (
        extras.get("aspect_ratio")
        or extras.get("aspectRatio")
        or getattr(gen_cfg, "aspectRatio", None)
        or getattr(image_cfg, "aspectRatio", None)
    )
    image_size = (
        extras.get("image_size")
        or extras.get("imageSize")
        or getattr(gen_cfg, "imageSize", None)
        or getattr(image_cfg, "imageSize", None)
    )
    duration = extras.get("duration")
    resolution = extras.get("resolution")
    num_outputs = extras.get("num_outputs", extras.get("n"))
    seed = extras.get("seed")
    sound = extras.get("sound")
    fps = extras.get("fps")
    voice_id = extras.get("voice_id") or extras.get("voiceId")
    mode = extras.get("mode")
    media_extra = extras.get("media")
    options = extras.get("options") or extras.get("runway_options") or {}
    media = _runway_media_from_images(normalized.images)
    if isinstance(media_extra, list):
        media.extend(item for item in media_extra if isinstance(item, dict))
    return {
        "media": media,
        "mode": str(mode) if mode else None,
        "aspect_ratio": str(aspect_ratio) if aspect_ratio else None,
        "duration": int(duration) if duration is not None and str(duration).strip().isdigit() else None,
        "resolution": str(resolution) if resolution else None,
        "image_size": str(image_size) if image_size else None,
        "num_outputs": int(num_outputs) if num_outputs is not None and str(num_outputs).strip().isdigit() else None,
        "seed": int(seed) if seed is not None and str(seed).strip().lstrip("-").isdigit() else None,
        "sound": parse_bool(sound),
        "fps": int(fps) if fps is not None and str(fps).strip().isdigit() else None,
        "voice_id": str(voice_id) if voice_id else None,
        "multi_shot": extras.get("multi_shot") if isinstance(extras.get("multi_shot"), list) else None,
        "upscale": extras.get("upscale") if isinstance(extras.get("upscale"), dict) else None,
        "options": options if isinstance(options, dict) else {},
    }


def _geminigen_options_from_request(request: Any) -> Dict[str, Any]:
    extras = _request_extra_dict(request)
    gen_cfg = getattr(request, "generationConfig", None)
    image_cfg = getattr(gen_cfg, "imageConfig", None) if gen_cfg is not None else None
    options = extras.get("options") or extras.get("geminigen_options") or {}
    if not isinstance(options, dict):
        options = {}
    for key in (
        "aspect_ratio",
        "aspectRatio",
        "resolution",
        "duration",
        "orientation",
        "num_result",
        "numResult",
        "mode",
        "service_mode",
        "serviceMode",
        "output_format",
        "outputFormat",
    ):
        if key in extras and extras[key] is not None:
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
            options[snake] = extras[key]
    if gen_cfg is not None:
        for attr, target in (("aspectRatio", "aspect_ratio"), ("imageSize", "resolution")):
            value = getattr(gen_cfg, attr, None)
            if value is not None:
                options[target] = value
    if image_cfg is not None:
        for attr, target in (("aspectRatio", "aspect_ratio"), ("imageSize", "resolution")):
            value = getattr(image_cfg, attr, None)
            if value is not None:
                options[target] = value
    if "numResult" in options and "num_result" not in options:
        options["num_result"] = options.pop("numResult")
    if "serviceMode" in options and "service_mode" not in options:
        options["service_mode"] = options.pop("serviceMode")
    if "outputFormat" in options and "output_format" not in options:
        options["output_format"] = options.pop("outputFormat")
    return options


def _geminigen_openai_chunk(content: str, *, role: Optional[str] = None) -> str:
    delta: Dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    payload = {
        "id": f"chatcmpl-geminigen-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "geminigen",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _geminigen_openai_done_chunk() -> str:
    payload = {
        "id": f"chatcmpl-geminigen-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "geminigen",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _runway_openai_chunk(content: str, *, role: Optional[str] = None) -> str:
    delta: Dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    payload = {
        "id": f"chatcmpl-runway-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "runway",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _runway_openai_done_chunk() -> str:
    payload = {
        "id": f"chatcmpl-runway-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "runway",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _start_runway_from_openai_request(
    request: ChatCompletionRequest,
    normalized: NormalizedGenerationRequest,
    api_key_id: Optional[int],
    service: RunwayService,
) -> RunwayTask:
    params = _runway_request_params_from_openai(request, normalized)
    return await service.start_task(
        public_model_id=normalized.model,
        prompt=normalized.prompt,
        media=params["media"],
        mode=params["mode"],
        aspect_ratio=params["aspect_ratio"],
        duration=params["duration"],
        resolution=params["resolution"],
        image_size=params["image_size"],
        num_outputs=params["num_outputs"],
        seed=params["seed"],
        sound=params["sound"],
        fps=params["fps"],
        voice_id=params["voice_id"],
        multi_shot=params["multi_shot"],
        upscale=params["upscale"],
        options=params["options"],
        api_key_id=api_key_id,
    )


async def _start_geminigen_from_request(
    request: Any,
    normalized: NormalizedGenerationRequest,
    api_key_id: Optional[int],
    service: GeminiGenService,
) -> GeminiGenTask:
    return await service.start_task(
        public_model_id=normalized.model,
        prompt=normalized.prompt,
        images=normalized.images,
        options=_geminigen_options_from_request(request),
        api_key_id=api_key_id,
    )


async def _enqueue_geminigen_from_request(
    request: Any,
    normalized: NormalizedGenerationRequest,
    api_key_id: Optional[int],
    service: GeminiGenService,
) -> GeminiGenTask:
    return await service.enqueue_task(
        public_model_id=normalized.model,
        prompt=normalized.prompt,
        images=normalized.images,
        options=_geminigen_options_from_request(request),
        api_key_id=api_key_id,
    )


async def _geminigen_openai_non_stream(
    request: Any,
    normalized: NormalizedGenerationRequest,
    *,
    api_key_id: Optional[int],
    base_url: Optional[str],
    service: GeminiGenService,
) -> Dict[str, Any]:
    task = await _start_geminigen_from_request(request, normalized, api_key_id, service)
    final = await service.wait_for_task(task.job_id, api_key_id=api_key_id, base_url=base_url)
    return service.task_to_openai_payload(final)


async def _iterate_geminigen_openai_stream(
    request: Any,
    normalized: NormalizedGenerationRequest,
    *,
    api_key_id: Optional[int],
    base_url: Optional[str],
    service: GeminiGenService,
):
    yield _geminigen_openai_chunk("GeminiGen task submitted.\n", role="assistant")
    task = await _start_geminigen_from_request(request, normalized, api_key_id, service)
    last_progress = -1
    while True:
        current = await service.poll_task(task.job_id, api_key_id=api_key_id, base_url=base_url)
        if current.progress != last_progress and current.status == "processing":
            last_progress = current.progress
            yield _geminigen_openai_chunk(f"GeminiGen progress: {current.progress}%\n")
        if service.is_geminigen_terminal_status(current.status):
            payload = service.task_to_openai_payload(current)
            if "error" in payload:
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            else:
                yield _geminigen_openai_chunk(_extract_openai_message_content(payload))
                yield _geminigen_openai_done_chunk()
            yield "data: [DONE]\n\n"
            return
        cfg = await service.db.get_geminigen_config()
        interval = cfg.poll_interval_video_sec if current.kind == "video" else cfg.poll_interval_image_sec
        await asyncio.sleep(float(interval))


async def _runway_openai_non_stream(
    request: ChatCompletionRequest,
    normalized: NormalizedGenerationRequest,
    *,
    api_key_id: Optional[int],
    base_url: Optional[str],
    service: RunwayService,
) -> Dict[str, Any]:
    task = await _start_runway_from_openai_request(request, normalized, api_key_id, service)
    final = await service.wait_for_task(task.job_id, api_key_id=api_key_id, base_url=base_url)
    return service.task_to_openai_payload(final)


async def _iterate_runway_openai_stream(
    request: ChatCompletionRequest,
    normalized: NormalizedGenerationRequest,
    *,
    api_key_id: Optional[int],
    base_url: Optional[str],
    service: RunwayService,
):
    yield _runway_openai_chunk("Runway task submitted.\n", role="assistant")
    task = await _start_runway_from_openai_request(request, normalized, api_key_id, service)
    last_progress = -1
    while True:
        current = await service.poll_task(task.job_id, api_key_id=api_key_id, base_url=base_url)
        if current.progress != last_progress and current.status == "processing":
            last_progress = current.progress
            yield _runway_openai_chunk(f"Runway progress: {current.progress}%\n")
        if current.status in {"completed", "failed"}:
            payload = service.task_to_openai_payload(current)
            if "error" in payload:
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            else:
                content = _extract_openai_message_content(payload)
                yield _runway_openai_chunk(content)
                yield _runway_openai_done_chunk()
            yield "data: [DONE]\n\n"
            return
        cfg = await service.db.get_runway_config()
        await asyncio.sleep(float(cfg.poll_interval_sec))


def _parse_handler_result(result: str) -> Dict[str, Any]:
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return {"result": result}


def _get_error_status_code(payload: Dict[str, Any]) -> int:
    error = payload.get("error")
    if isinstance(error, dict):
        status_code = error.get("status_code")
        if isinstance(status_code, int):
            return status_code
        if isinstance(status_code, str) and status_code.isdigit():
            return int(status_code)
        return 400
    return 200


def _build_openai_json_response(payload: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(content=payload, status_code=_get_error_status_code(payload))


def _build_gemini_error_payload(status_code: int, message: str) -> Dict[str, Any]:
    return {
        "error": {
            "code": status_code,
            "message": message,
            "status": GEMINI_STATUS_MAP.get(status_code, "UNKNOWN"),
        }
    }


def _build_gemini_error_response_from_handler(payload: Dict[str, Any]) -> JSONResponse:
    error = payload.get("error", {})
    status_code = _get_error_status_code(payload)
    message = error.get("message", "Generation failed")
    return JSONResponse(
        status_code=status_code,
        content=_build_gemini_error_payload(status_code, message),
    )


def _extract_openai_message_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices", [])
    if not choices:
        return payload.get("result", "")

    message = choices[0].get("message", {})
    content = message.get("content", "")
    return content if isinstance(content, str) else ""


def _extract_url_from_openai_payload(payload: Dict[str, Any]) -> Optional[str]:
    direct_url = payload.get("url")
    if isinstance(direct_url, str) and direct_url.strip():
        return direct_url.strip()

    content = _extract_openai_message_content(payload).strip()
    if not content:
        return None

    image_match = MARKDOWN_IMAGE_RE.search(content)
    if image_match:
        return image_match.group(1).strip()

    video_match = HTML_VIDEO_RE.search(content)
    if video_match:
        return video_match.group(1).strip()

    return None


def _enrich_payload_with_direct_url(payload: Dict[str, Any]) -> Dict[str, Any]:
    extracted_url = _extract_url_from_openai_payload(payload)
    if extracted_url and not payload.get("url"):
        payload["url"] = extracted_url
    return payload


def _with_projectid(payload: Dict[str, Any], project_id: Optional[str]) -> Dict[str, Any]:
    if project_id and not payload.get("project_id"):
        payload["project_id"] = project_id
    return payload


def _infer_requested_resolution(model: str) -> Optional[str]:
    model_config = MODEL_CONFIG.get(model) or {}
    upsample_cfg = model_config.get("upsample")
    if isinstance(upsample_cfg, str):
        if "4K" in upsample_cfg:
            return "4k"
        if "2K" in upsample_cfg:
            return "2k"
        return upsample_cfg
    if isinstance(upsample_cfg, dict):
        resolution = upsample_cfg.get("resolution")
        if isinstance(resolution, str) and resolution:
            if "1080" in resolution:
                return "1080p"
            if "4K" in resolution:
                return "4k"
            return resolution
    return None


def _new_async_job_id() -> str:
    # Example: gen-20260429-181455-a1b2c3d4
    return f"gen-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _infer_async_failed_captcha_status(message: str) -> str:
    m = (message or "").lower()
    if any(s in m for s in ("recaptcha", "captcha", "public_error")):
        return "upstream_rejected"
    return "unknown"


def _looks_like_mojibake(text: str) -> bool:
    sample = (text or "").strip()
    if not sample:
        return False
    suspicious_chars = {"σ", "Γ", "╜", "╗", "╝", "╣", "╦", "Φ", "τ", "µ"}
    hit_count = sum(1 for ch in sample if ch in suspicious_chars)
    if hit_count >= 3:
        return True
    replacement_count = sample.count("\ufffd")
    if replacement_count >= 2:
        return True
    non_ascii = sum(1 for ch in sample if ord(ch) > 127)
    return non_ascii > 0 and (hit_count + replacement_count) >= max(2, non_ascii // 3)


def _sanitize_async_error_message(
    raw_error: Any,
    *,
    selection_context: Optional[Dict[str, Any]] = None,
) -> str:
    text = str(raw_error or "").strip()
    if not text:
        text = "生成失败，请稍后重试"
    if _looks_like_mojibake(text):
        text = "生成失败：上游返回了不可读错误，请稍后重试"

    if isinstance(selection_context, dict):
        reason_type = str(selection_context.get("allowlist_filter_reason_type") or "").strip()
        if reason_type == "project_pin":
            suffix = "（当前请求已绑定项目账号）"
            if suffix not in text:
                text = f"{text}{suffix}"
    return text


def _extract_async_delivery_fields(
    payload: Dict[str, Any], model: str
) -> Dict[str, Any]:
    direct_url = payload.get("url")
    direct_urls = [direct_url] if isinstance(direct_url, str) and direct_url.strip() else []
    generated_assets = payload.get("generated_assets") if isinstance(payload, dict) else None

    requested_resolution = _infer_requested_resolution(model)
    output_resolution: Optional[str] = None
    upscale_status = "not_requested" if requested_resolution is None else "failed"
    upscale_error_message: Optional[str] = None

    base_result_urls = list(direct_urls)
    result_urls = list(direct_urls)
    delivery_urls = list(direct_urls)
    suppress_delivery_urls = False

    if isinstance(generated_assets, dict):
        asset_type = generated_assets.get("type")
        if asset_type == "image":
            origin_image_url = generated_assets.get("origin_image_url")
            if isinstance(origin_image_url, str) and origin_image_url.strip():
                base_result_urls = [origin_image_url.strip()]
                if not delivery_urls:
                    delivery_urls = list(base_result_urls)
                    result_urls = list(base_result_urls)
            final_image_url = generated_assets.get("final_image_url")
            if isinstance(final_image_url, str) and final_image_url.strip():
                result_urls = [final_image_url.strip()]
                delivery_urls = [final_image_url.strip()]
            upscaled_image = generated_assets.get("upscaled_image")
            if isinstance(upscaled_image, dict):
                upscale_url = upscaled_image.get("url") or upscaled_image.get("local_url")
                has_upscale_url = isinstance(upscale_url, str) and bool(upscale_url.strip())
                if isinstance(upscale_url, str) and upscale_url.strip():
                    result_urls = [upscale_url.strip()]
                    delivery_urls = [upscale_url.strip()]
                resolution = upscaled_image.get("resolution")
                if isinstance(resolution, str) and resolution.strip():
                    output_resolution = resolution.lower()
                delivery_mode = upscaled_image.get("delivery_mode")
                if delivery_mode == "inline_base64_fallback":
                    upscale_status = "failed"
                    upscale_error_message = "Upscale cache delivery failed; returned base64 fallback"
                elif has_upscale_url:
                    upscale_status = "completed"
                else:
                    upscale_status = "failed"
                    upscale_error_message = "Upscale completed without a deliverable URL"
            elif requested_resolution is not None:
                upscale_status = "failed"
                if delivery_urls:
                    output_resolution = "1k"
            if requested_resolution is not None and upscale_status != "completed":
                suppress_delivery_urls = True
                result_urls = []
                delivery_urls = []
                upscale_error_message = upscale_error_message or "Requested image upscale did not complete"
        elif asset_type == "video":
            final_video_url = generated_assets.get("final_video_url")
            if isinstance(final_video_url, str) and final_video_url.strip():
                result_urls = [final_video_url.strip()]
                delivery_urls = [final_video_url.strip()]
                if not base_result_urls:
                    base_result_urls = [final_video_url.strip()]
            if requested_resolution is not None:
                # Current payload does not expose pre-upscale URL for video.
                upscale_status = "completed"
                output_resolution = requested_resolution
    elif requested_resolution is None:
        upscale_status = "not_requested"

    if not suppress_delivery_urls and not delivery_urls and base_result_urls:
        delivery_urls = list(base_result_urls)
    if not output_resolution and delivery_urls:
        output_resolution = requested_resolution

    return {
        "result_urls": result_urls or None,
        "base_result_urls": base_result_urls or None,
        "delivery_urls": delivery_urls or None,
        "requested_resolution": requested_resolution,
        "output_resolution": output_resolution,
        "upscale_status": upscale_status,
        "upscale_error_message": upscale_error_message,
    }


async def _run_async_generation_task(
    task_id: str,
    normalized: NormalizedGenerationRequest,
    base_url_override: Optional[str],
    allowed_token_ids: Optional[set[int]],
    selection_context: Optional[Dict[str, Any]],
    api_key_id: Optional[int],
    handler: GenerationHandler,
) -> None:
    debug_logger.log_info(
        "[ASYNC JOB] start generation task: "
        f"task_id={task_id}, model={normalized.model}, "
        f"project_id={normalized.project_id}, "
        f"allowlist={sorted(int(x) for x in (allowed_token_ids or set()))}, "
        f"selection_type={(selection_context or {}).get('allowlist_filter_reason_type') if isinstance(selection_context, dict) else None}"
    )
    try:
        raw_result = await _collect_non_stream_result(
            normalized,
            handler,
            base_url_override,
            allowed_token_ids,
            selection_context,
            api_key_id,
            poll_task_id=task_id,
        )
        payload = _enrich_payload_with_direct_url(_parse_handler_result(raw_result))
        if "error" in payload:
            error_msg = _sanitize_async_error_message(
                payload.get("error", {}).get("message", "Upstream generation error"),
                selection_context=selection_context,
            )
            requested_resolution = _infer_requested_resolution(normalized.model)
            upscale_error_message = payload.get("upscale_error_message")
            if not isinstance(upscale_error_message, str) or not upscale_error_message.strip():
                upscale_error_message = error_msg if requested_resolution else None
            captcha_status = _infer_async_failed_captcha_status(error_msg)
            if payload.get("upscale_status") == "failed" and captcha_status == "unknown":
                captcha_status = "idle"
            debug_logger.log_error(
                "[ASYNC JOB] upstream error payload: "
                f"task_id={task_id}, error={error_msg}, raw_error={payload.get('error')}"
            )
            await handler.db.update_task(
                task_id,
                status="failed",
                result_urls=[],
                base_result_urls=[],
                delivery_urls=[],
                error_message=error_msg,
                upscale_status="failed" if requested_resolution else "not_requested",
                upscale_error_message=upscale_error_message,
                completed_at=datetime.utcnow(),
                job_phase="failed",
                captcha_status=captcha_status,
            )
            return

        fields = _extract_async_delivery_fields(payload, normalized.model)
        model_config = MODEL_CONFIG.get(normalized.model) or {}
        if (
            model_config.get("type") == "image"
            and fields["requested_resolution"] is not None
            and fields["upscale_status"] != "completed"
        ):
            error_msg = fields["upscale_error_message"] or "Requested image upscale did not complete"
            await handler.db.update_task(
                task_id,
                status="failed",
                result_urls=[],
                base_result_urls=[],
                delivery_urls=[],
                requested_resolution=fields["requested_resolution"],
                output_resolution=fields["output_resolution"],
                upscale_status="failed",
                upscale_error_message=error_msg,
                error_message=error_msg,
                completed_at=datetime.utcnow(),
                job_phase="failed",
                captcha_status="idle",
            )
            debug_logger.log_error(
                f"[ASYNC JOB] failed requested image upscale: task_id={task_id}, error={error_msg}"
            )
            return
        await handler.db.update_task(
            task_id,
            status="completed",
            progress=100,
            result_urls=fields["result_urls"],
            base_result_urls=fields["base_result_urls"],
            delivery_urls=fields["delivery_urls"],
            requested_resolution=fields["requested_resolution"],
            output_resolution=fields["output_resolution"],
            upscale_status=fields["upscale_status"],
            upscale_error_message=fields["upscale_error_message"],
            completed_at=datetime.utcnow(),
            job_phase="completed",
            captcha_status="not_applicable",
        )
        debug_logger.log_info(
            f"[ASYNC JOB] completed task: task_id={task_id}, status=completed, result_count={len(fields.get('delivery_urls') or [])}"
        )
    except Exception as exc:
        debug_logger.log_error(f"[ASYNC JOB] Generation failed for task {task_id}: {exc}")
        err_final = _sanitize_async_error_message(exc, selection_context=selection_context)
        await handler.db.update_task(
            task_id,
            status="failed",
            result_urls=[],
            base_result_urls=[],
            delivery_urls=[],
            error_message=err_final,
            upscale_status="failed" if _infer_requested_resolution(normalized.model) else "not_requested",
            completed_at=datetime.utcnow(),
            job_phase="failed",
            captcha_status=_infer_async_failed_captcha_status(err_final),
        )


def _inject_projectid_into_openai_sse_chunk(
    chunk: str,
    project_id: Optional[str],
) -> str:
    if not project_id or not chunk.startswith("data: "):
        return chunk
    payload_text = chunk[6:].strip()
    if payload_text == "[DONE]":
        return chunk
    payload = _parse_handler_result(payload_text)
    if not isinstance(payload, dict):
        return chunk
    payload = _with_projectid(payload, project_id)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _build_image_parts_from_uri(
    uri: str,
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
) -> List[Dict[str, Any]]:
    if uri.startswith("data:image"):
        mime_type, _ = _decode_data_url(uri)
        match = DATA_URL_RE.match(uri)
        if match:
            return [{"inlineData": {"mimeType": mime_type, "data": match.group("data")}}]

    image_bytes = await retrieve_image_data(
        uri, handler, api_key_id=api_key_id, allowed_token_ids=allowed_token_ids
    )
    if image_bytes:
        mime_type = _detect_image_mime_type(
            image_bytes,
            fallback=_guess_mime_type(uri, "image/png"),
        )
        return [
            {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            }
        ]

    return [
        {
            "fileData": {
                "mimeType": _guess_mime_type(uri, "image/png"),
                "fileUri": uri,
            }
        },
        {"text": uri},
    ]


def _build_video_parts_from_uri(uri: str) -> List[Dict[str, Any]]:
    return [
        {
            "fileData": {
                "mimeType": _guess_mime_type(uri, "video/mp4"),
                "fileUri": uri,
            }
        }
    ]


async def _build_gemini_parts_from_output(
    output: str,
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
) -> List[Dict[str, Any]]:
    if not output:
        return []

    image_matches = MARKDOWN_IMAGE_RE.findall(output)
    if image_matches:
        parts: List[Dict[str, Any]] = []
        for uri in image_matches:
            parts.extend(
                await _build_image_parts_from_uri(
                    uri, handler, api_key_id=api_key_id, allowed_token_ids=allowed_token_ids
                )
            )
        return parts

    video_matches = HTML_VIDEO_RE.findall(output)
    if video_matches:
        parts: List[Dict[str, Any]] = []
        for uri in video_matches:
            parts.extend(_build_video_parts_from_uri(uri))
        return parts

    # Progress / thought streams: blank-line-separated blocks render as separate steps in Gemini UIs.
    blocks = [b.strip() for b in output.split("\n\n") if b.strip()]
    if len(blocks) > 1:
        return [{"text": b} for b in blocks]
    return [{"text": output}]


async def _build_gemini_success_payload(
    payload: Dict[str, Any],
    response_model: str,
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    output = _extract_openai_message_content(payload)
    response = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": await _build_gemini_parts_from_output(
                        output,
                        handler,
                        api_key_id=api_key_id,
                        allowed_token_ids=allowed_token_ids,
                    ),
                },
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "modelVersion": response_model,
    }
    return _with_projectid(response, project_id)


def _normalize_finish_reason(reason: Optional[str]) -> Optional[str]:
    if reason is None:
        return None
    mapping = {
        "stop": "STOP",
        "length": "MAX_TOKENS",
        "content_filter": "SAFETY",
    }
    return mapping.get(reason, "STOP")


async def _convert_openai_stream_chunk_to_gemini_event(
    payload: Dict[str, Any],
    response_model: str,
    handler: GenerationHandler,
    api_key_id: Optional[int] = None,
    allowed_token_ids: Optional[Set[int]] = None,
    project_id: Optional[str] = None,
) -> Optional[str]:
    choices = payload.get("choices", [])
    if not choices:
        return None

    choice = choices[0]
    delta = choice.get("delta", {})
    text = delta.get("reasoning_content") or delta.get("content") or ""
    finish_reason = _normalize_finish_reason(choice.get("finish_reason"))

    candidate: Dict[str, Any] = {"index": choice.get("index", 0)}
    if text:
        candidate["content"] = {
            "role": "model",
            "parts": await _build_gemini_parts_from_output(
                text,
                handler,
                api_key_id=api_key_id,
                allowed_token_ids=allowed_token_ids,
            ),
        }
    if finish_reason:
        candidate["finishReason"] = finish_reason

    if len(candidate) == 1:
        return None

    chunk = {
        "candidates": [candidate],
        "modelVersion": response_model,
    }
    chunk = _with_projectid(chunk, project_id)
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def _iterate_openai_stream(
    normalized: NormalizedGenerationRequest,
    handler: GenerationHandler,
    base_url_override: Optional[str] = None,
    allowed_token_ids: Optional[set[int]] = None,
    selection_context: Optional[Dict[str, Any]] = None,
    api_key_id: Optional[int] = None,
    project_id: Optional[str] = None,
):
    async for chunk in handler.handle_generation(
        model=normalized.model,
        prompt=normalized.prompt,
        images=normalized.images if normalized.images else None,
        stream=True,
        base_url_override=base_url_override,
        allowed_token_ids=allowed_token_ids,
        selection_context=selection_context,
        api_key_id=api_key_id,
        requested_project_id=normalized.project_id,
        video_media_id=normalized.video_media_id,
    ):
        if chunk.startswith("data: "):
            yield _inject_projectid_into_openai_sse_chunk(chunk, project_id)
            continue

        payload = _parse_handler_result(chunk)
        payload = _with_projectid(payload, project_id)
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"


async def _iterate_gemini_stream(
    normalized: NormalizedGenerationRequest,
    response_model: str,
    handler: GenerationHandler,
    base_url_override: Optional[str] = None,
    allowed_token_ids: Optional[set[int]] = None,
    selection_context: Optional[Dict[str, Any]] = None,
    api_key_id: Optional[int] = None,
    project_id: Optional[str] = None,
):
    async for chunk in handler.handle_generation(
        model=normalized.model,
        prompt=normalized.prompt,
        images=normalized.images if normalized.images else None,
        stream=True,
        base_url_override=base_url_override,
        allowed_token_ids=allowed_token_ids,
        selection_context=selection_context,
        api_key_id=api_key_id,
        requested_project_id=normalized.project_id,
        video_media_id=normalized.video_media_id,
    ):
        if chunk.startswith("data: "):
            payload_text = chunk[6:].strip()
            if payload_text == "[DONE]":
                continue
            payload = _parse_handler_result(payload_text)
            if "error" in payload:
                yield (
                    f"data: {json.dumps(_build_gemini_error_payload(_get_error_status_code(payload), payload['error'].get('message', 'Generation failed')), ensure_ascii=False)}\n\n"
                )
                return

            event = await _convert_openai_stream_chunk_to_gemini_event(
                payload,
                response_model,
                handler,
                api_key_id=api_key_id,
                allowed_token_ids=allowed_token_ids,
                project_id=project_id,
            )
            if event:
                yield event
            continue

        payload = _parse_handler_result(chunk)
        if "error" in payload:
            yield (
                f"data: {json.dumps(_build_gemini_error_payload(_get_error_status_code(payload), payload['error'].get('message', 'Generation failed')), ensure_ascii=False)}\n\n"
            )
            return

        event = await _convert_openai_stream_chunk_to_gemini_event(
            payload,
            response_model,
            handler,
            api_key_id=api_key_id,
            allowed_token_ids=allowed_token_ids,
            project_id=project_id,
        )
        if event:
            yield event


def _resolve_allowed_token_ids(auth_ctx: AuthContext) -> Optional[set[int]]:
    if not auth_ctx.is_legacy and auth_ctx.allowed_accounts:
        return {int(x) for x in auth_ctx.allowed_accounts}
    return None


def _build_selection_context(
    auth_ctx: AuthContext,
    allowed_token_ids: Optional[Set[int]],
    selected_project_id: Optional[str],
) -> Dict[str, Any]:
    key_allowed_accounts = sorted(int(x) for x in (auth_ctx.allowed_accounts or set()))
    effective_allowed = sorted(int(x) for x in (allowed_token_ids or set()))
    reason_type = "api_key_assignment"
    if effective_allowed and len(effective_allowed) < len(key_allowed_accounts):
        reason_type = "project_pin"
    context = {
        "allowlist_filter_reason_type": reason_type,
        "key_allowed_account_ids": key_allowed_accounts,
        "effective_allowed_token_ids": effective_allowed,
        "selected_project_id": (selected_project_id or "").strip() or None,
    }
    debug_logger.log_info(
        "[ROUTES] selection context built: "
        f"type={context['allowlist_filter_reason_type']}, "
        f"project_id={context['selected_project_id']}, "
        f"key_allowed={context['key_allowed_account_ids']}, "
        f"effective_allowed={context['effective_allowed_token_ids']}"
    )
    return context


async def _resolve_project_pin(
    project_id: Optional[str],
    auth_ctx: AuthContext,
    handler: GenerationHandler,
) -> Tuple[Optional[Set[int]], Optional[str]]:
    """If project_id is set, validate DB row and return ({token_id}, canonical_id); else (None, None)."""
    pid = _strip_optional_project_id(project_id)
    if not pid:
        return (None, None)
    if auth_ctx.key_id is not None:
        proj = await handler.db.get_project_by_id(pid, auth_ctx.key_id)
        if not proj:
            raise HTTPException(
                status_code=400,
                detail="project_id not found for this API key",
            )
        if not bool(proj.is_active):
            raise HTTPException(status_code=400, detail="project_id is inactive")
        tid = int(proj.token_id)
        if tid not in auth_ctx.allowed_accounts:
            raise HTTPException(
                status_code=400,
                detail="project_id is not assigned to this API key",
            )
        return ({tid}, pid)
    proj = await handler.db.get_project_by_id(pid, None)
    if not proj:
        raise HTTPException(status_code=400, detail="project_id not found")
    if not bool(proj.is_active):
        raise HTTPException(status_code=400, detail="project_id is inactive")
    return ({int(proj.token_id)}, pid)


async def _select_generation_target(
    auth_ctx: AuthContext,
    model: str,
    project_id: Optional[str],
    handler: GenerationHandler,
) -> Tuple[Set[int], Optional[str]]:
    """Use an authorized project pin when provided, otherwise retain automatic routing."""
    pinned_token_ids, pinned_project_id = await _resolve_project_pin(project_id, auth_ctx, handler)
    if pinned_project_id:
        return (pinned_token_ids or set(), pinned_project_id)
    return await _select_random_active_project_for_api_key(auth_ctx, model, handler)


async def _select_random_active_project_for_api_key(
    auth_ctx: AuthContext,
    model: str,
    handler: GenerationHandler,
) -> Tuple[Set[int], Optional[str]]:
    if auth_ctx.key_id is None:
        raise HTTPException(status_code=403, detail="Managed API key required for generation")
    if not auth_ctx.allowed_accounts:
        raise HTTPException(status_code=400, detail="No accounts assigned to this API key")


    model_config = MODEL_CONFIG.get(model) or {}
    generation_type = model_config.get("type")
    for_image_generation = generation_type == "image"
    for_video_generation = generation_type == "video"

    active_tokens = await handler.load_balancer.token_manager.get_active_tokens()
    assigned_active_tokens = [
        token for token in active_tokens if int(token.id) in auth_ctx.allowed_accounts
    ]
    if (
        model in MODEL_CONFIG
        and is_native_4k_model(model)
        and not any(
            supports_model_for_tier(model, token.user_paygate_tier)
            for token in assigned_active_tokens
        )
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Native 4K generation requires an active Ultra account assigned "
                f"to this API key: {model}"
            ),
        )

    token_candidates: List[Dict[str, Any]] = []
    for token in active_tokens:
        token_id = int(token.id)
        if token_id not in auth_ctx.allowed_accounts:
            continue
        if for_image_generation and not token.image_enabled:
            continue
        if for_video_generation and not token.video_enabled:
            continue
        if model and not supports_model_for_tier(model, normalize_user_paygate_tier(token.user_paygate_tier)):
            continue

        inflight, remaining = await handler.load_balancer._get_token_load(
            token_id,
            for_image_generation=for_image_generation,
            for_video_generation=for_video_generation,
        )
        token_candidates.append(
            {
                "token_id": token_id,
                "inflight": inflight,
                "remaining": remaining,
                "random": random.random(),
            }
        )

    if not token_candidates:
        raise HTTPException(
            status_code=400,
            detail="No eligible assigned account is available for this model",
        )

    token_candidates.sort(
        key=lambda item: (
            item["inflight"],
            0 if item["remaining"] is None else 1,
            -(item["remaining"] or 0),
            item["random"],
        )
    )
    selected_token_id = int(token_candidates[0]["token_id"])
    return ({selected_token_id}, None)


from ..transport.projects import (
    create_flow_project,
    get_flow_project,
    list_flow_projects,
    router as projects_router,
)


router.include_router(projects_router)


from ..transport.extensions import (
    ExtensionAccountImportRequest,
    _require_token_import_scope,
    extension_generation_upload,
    extension_import_current_account,
    extension_metadata_session,
    router as extensions_router,
)


router.include_router(extensions_router)


from ..transport.cache import (
    get_cached_blob,
    list_cache_files_for_key,
    list_cache_files_for_key_project,
    router as cache_router,
)


router.include_router(cache_router)


from ..transport.adobe import (
    fetch_task_tracker_contributor_assets,
    fetch_task_tracker_keyword_search,
    generate_cloning_prompts,
    generate_cloning_video_prompt,
    generate_metadata,
    router as adobe_router,
)


router.include_router(adobe_router)


from ..transport.models import (
    get_gemini_model,
    get_generation_capacity,
    list_gemini_models,
    list_model_aliases,
    list_models,
    router as models_router,
)


router.include_router(models_router)


from ..transport.runway import (
    RunwayEstimateRequest,
    RunwayMediaInput,
    RunwayTaskCreateRequest,
    cancel_runway_task,
    create_runway_task,
    estimate_runway_task,
    get_runway_task,
    list_runway_models,
    list_runway_voices,
    router as runway_router,
    upload_runway_media,
)


router.include_router(runway_router)


from ..transport.openai import (
    create_chat_completion,
    create_chat_completion_async,
    get_job_status,
    router as openai_router,
)


router.include_router(openai_router)


from ..transport.gemini import (
    generate_content,
    router as gemini_router,
    stream_generate_content,
)


router.include_router(gemini_router)


from ..transport.websocket import (
    captcha_websocket_endpoint,
    router as websocket_router,
)


router.include_router(websocket_router)
