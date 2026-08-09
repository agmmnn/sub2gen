"""Metadata and cloning prompt generation service."""

import base64
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi.requests import AsyncSession

# Adobe Stock top-level metadata categories (id + label). Single source of truth for prompts and normalization.
ADOBE_STOCK_METADATA_CATEGORIES: Tuple[Tuple[int, str], ...] = (
    (1, "Animals"),
    (2, "Buildings and Architecture"),
    (3, "Business"),
    (4, "Drinks"),
    (5, "The Environment"),
    (6, "States of Mind"),
    (7, "Food"),
    (8, "Graphic Resources"),
    (9, "Hobbies and Leisure"),
    (10, "Industry"),
    (11, "Landscape"),
    (12, "Lifestyle"),
    (13, "People"),
    (14, "Plants and Flowers"),
    (15, "Culture and Religion"),
    (16, "Science"),
    (17, "Social Issues"),
    (18, "Sports"),
    (19, "Technology"),
    (20, "Transport"),
    (21, "Travel"),
)


def _normalize_image_mime_type(value: Optional[str]) -> Optional[str]:
    mime = str(value or "").split(";", 1)[0].strip().lower()
    if not mime or "/" not in mime:
        return None
    if mime in {"image/jpg", "image/pjpeg"}:
        return "image/jpeg"
    if mime.startswith("image/"):
        return mime
    return None


def _detect_image_mime_type(image_bytes: bytes, fallback: Optional[str] = None) -> str:
    hinted = _normalize_image_mime_type(fallback)
    if hinted:
        return hinted
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"<svg") or b"<svg" in image_bytes[:512].lower():
        return "image/svg+xml"
    return "image/jpeg"


def _adobe_stock_category_table_prompt_lines() -> str:
    return "\n".join(f"{cid} — {name}" for cid, name in ADOBE_STOCK_METADATA_CATEGORIES)


def _adobe_category_id_by_label(name: str) -> Optional[int]:
    n = (name or "").strip().lower()
    if not n:
        return None
    for cid, label in ADOBE_STOCK_METADATA_CATEGORIES:
        if label.lower() == n:
            return cid
    return None


def _parse_adobe_category_id_value(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 21 else None
    if isinstance(value, float):
        if value == int(value) and 1 <= int(value) <= 21:
            return int(value)
        return None
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            v = int(s)
            return v if 1 <= v <= 21 else None
    return None


def _resolve_adobe_stock_category_id(raw: Dict[str, Any]) -> Optional[int]:
    parsed = _parse_adobe_category_id_value(raw.get("categoryId"))
    if parsed is not None:
        return parsed
    cat = raw.get("category")
    if cat is None:
        return None
    s = str(cat).strip()
    if s.isdigit():
        v = int(s)
        if 1 <= v <= 21:
            return v
    return _adobe_category_id_by_label(s)


def _adobe_stock_categories_for_csvgen_settings() -> List[Dict[str, Any]]:
    """Adobe Stock taxonomy as CSVGEN expects in settings.categories ({id, name} rows)."""
    return [{"id": cid, "name": label} for cid, label in ADOBE_STOCK_METADATA_CATEGORIES]


from fastapi import HTTPException
from ..core.config import config as app_config
from .llm_provider_chain import (
    CLONING_PROVIDERS,
    LlmProviderChain,
    METADATA_PROVIDERS,
    get_csv,
    is_retryable_error,
    normalized_retry_count,
)


DEFAULT_TEMPLATE: Dict[str, Any] = {
    "scene": "",
    "style": "",
    "constraints": ["SILENT_OUTPUT"],
    "shot": {
        "composition": "",
        "camera_motion": "",
        "frame_rate": "60 fps",
        "resolution": "1920 × 1080",
        "lens": "",
    },
    "voice_over": {
        "language": "",
        "tone": "",
        "mode": "",
        "emotion": "",
        "narration_text": "",
        "duration_sec": "",
    },
    "house_settings": {
        "typeface": {"hook": "", "subtext": ""},
        "overlay_style": "none",
        "animation": {"enter": "", "enter_duration_ms": 600, "exit": "", "exit_duration_ms": 500},
        "callouts": {"stroke_px": 0, "corner_radius_px": 0},
        "sizes": {"hook_font_height_pct": "", "sublabel_font_height_pct": "", "safe_margins_pct": 7},
    },
    "timeline": [
        {"time": "0.0–1.5 s", "action": ""},
        {"time": "1.5–3.0 s", "action": ""},
        {"time": "3.0–4.0 s", "action": ""},
        {"time": "4.0–5.5 s", "action": ""},
        {"time": "5.5–6.5 s", "action": ""},
        {"time": "6.5–7.5 s", "action": ""},
        {"time": "7.5–END", "action": ""},
    ],
    "lighting": {"primary": "", "secondary": "", "accents": ""},
    "audio": {
        "mode": "none",
        "ambient": "none",
        "sfx": [],
        "music": {
            "track": "none",
            "description": "no music",
            "tempo": "n/a",
            "key": "n/a",
            "dynamic_curve": "flat",
        },
        "mix": {"integrated_loudness": "-inf", "sidechain_music_db_on_impacts": 0, "natural_reverb": False},
    },
    "text_rules": {"emoji_policy": "no emojis", "contrast": ""},
    "color_palette": {"background": "", "ink_primary": "", "ink_secondary": "", "splatter": "", "text_primary": ""},
    "transitions": {"between_scenes": "", "impact_frame_usage": "", "forbidden": []},
    "vfx_rules": {"grain": "none", "particles": "none", "camera_shake": "none"},
    "visual_rules": {"prohibited_elements": [], "grain": "none", "sharpen": "none"},
    "export": {"preset": "1920x1080_h264_high", "target_duration_sec": ""},
    "metadata": {"series": "", "task": "", "scene_number": "", "tags": []},
}

CLONING_PROMPTS_DEADLINE_SECONDS = 105.0
_MIN_CLONING_IO_TIMEOUT_SECONDS = 1.0


def _cloning_remaining_timeout(deadline_at: Optional[float], default_timeout: float) -> float:
    if deadline_at is None:
        return default_timeout
    remaining = float(deadline_at) - time.monotonic()
    if remaining <= _MIN_CLONING_IO_TIMEOUT_SECONDS:
        raise HTTPException(status_code=504, detail="Cloning prompt generation deadline exceeded")
    return max(_MIN_CLONING_IO_TIMEOUT_SECONDS, min(default_timeout, remaining))


def _template_text() -> str:
    return json.dumps(DEFAULT_TEMPLATE, ensure_ascii=False, indent=2)


def _normalize_image_prompt(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "scene": p.get("scene") or "",
        "style": p.get("style") or "",
        "constraints": p.get("constraints") if isinstance(p.get("constraints"), list) else ["SILENT_OUTPUT"],
        "shot": {
            "composition": ((p.get("shot") or {}).get("composition")) or "",
            "resolution": ((p.get("shot") or {}).get("resolution")) or "1920 × 1080",
            "lens": ((p.get("shot") or {}).get("lens")) or "",
        },
        "lighting": {
            "primary": ((p.get("lighting") or {}).get("primary")) or "",
            "secondary": ((p.get("lighting") or {}).get("secondary")) or "",
            "accents": ((p.get("lighting") or {}).get("accents")) or "",
        },
        "color_palette": {
            "background": ((p.get("color_palette") or {}).get("background")) or "",
            "ink_primary": ((p.get("color_palette") or {}).get("ink_primary")) or "",
            "ink_secondary": ((p.get("color_palette") or {}).get("ink_secondary")) or "",
            "text_primary": ((p.get("color_palette") or {}).get("text_primary")) or "",
        },
        "visual_rules": {
            "prohibited_elements": ((p.get("visual_rules") or {}).get("prohibited_elements"))
            if isinstance((p.get("visual_rules") or {}).get("prohibited_elements"), list)
            else [],
            "grain": ((p.get("visual_rules") or {}).get("grain")) or "none",
            "sharpen": ((p.get("visual_rules") or {}).get("sharpen")) or "none",
        },
        "metadata": {
            "series": ((p.get("metadata") or {}).get("series")) or "",
            "task": ((p.get("metadata") or {}).get("task")) or "",
            "scene_number": ((p.get("metadata") or {}).get("scene_number")) or "",
            "tags": ((p.get("metadata") or {}).get("tags")) if isinstance((p.get("metadata") or {}).get("tags"), list) else [],
        },
    }


def _non_empty_str(value: Any) -> str:
    return str(value or "").strip() if value is not None else ""


def _has_meaningful_image_prompt_content(prompt: Dict[str, Any]) -> bool:
    shot = prompt.get("shot") if isinstance(prompt.get("shot"), dict) else {}
    lighting = prompt.get("lighting") if isinstance(prompt.get("lighting"), dict) else {}
    metadata = prompt.get("metadata") if isinstance(prompt.get("metadata"), dict) else {}
    tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
    return any(
        [
            _non_empty_str(prompt.get("scene")),
            _non_empty_str(prompt.get("style")),
            _non_empty_str(shot.get("composition")),
            _non_empty_str(lighting.get("primary")),
            any(_non_empty_str(tag) for tag in tags),
        ]
    )


def _ensure_meaningful_image_prompt(prompt: Dict[str, Any]) -> Dict[str, Any]:
    if not _has_meaningful_image_prompt_content(prompt):
        raise HTTPException(
            status_code=422,
            detail=(
                "Model returned a blank cloning prompt. Expected at least one descriptive field "
                "(scene, style, shot.composition, lighting.primary, or metadata.tags)."
            ),
        )
    return prompt


class CloningMetadataService:
    def __init__(self, llm_chain: Optional[LlmProviderChain] = None) -> None:
        self._llm = llm_chain or LlmProviderChain()

    async def _fetch_image(
        self,
        image_url: Optional[str],
        image_base64: Optional[str],
        deadline_at: Optional[float] = None,
        mime_type_hint: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        if image_url:
            async with AsyncSession() as session:
                resp = await session.get(image_url, timeout=_cloning_remaining_timeout(deadline_at, 60.0), verify=False)
                if resp.status_code != 200 or not resp.content:
                    raise HTTPException(status_code=400, detail=f"Failed to fetch image: HTTP {resp.status_code}")
                content = bytes(resp.content)
                mime = _detect_image_mime_type(content, resp.headers.get("content-type") or mime_type_hint)
                return content, mime
        if image_base64:
            raw = image_base64.strip()
            data_url_mime: Optional[str] = None
            if raw.lower().startswith("data:"):
                header = raw.split(",", 1)[0]
                if ";base64" in header.lower():
                    data_url_mime = header[5:].split(";", 1)[0]
            if "base64," in raw:
                raw = raw.split("base64,", 1)[1]
            try:
                content = base64.b64decode(raw)
                return content, _detect_image_mime_type(content, mime_type_hint or data_url_mime)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Invalid base64 image: {exc}") from exc
        raise HTTPException(status_code=400, detail="One image source is required")

    def _build_clone_instruction(self, image: Dict[str, Any]) -> str:
        item_id = str(image.get("id") or "")
        title = str(image.get("title") or "")
        default_prompt = (
            "You are an OCR + structured prompt generator.\n"
            "Read the image, extract visible text, and return ONLY valid JSON.\n"
            "Do not output analysis, thoughts, markdown, or extra text.\n"
            "Output must be one JSON object only, using this exact schema:\n"
            + _template_text()
            + "\n\nNexus DNA cloning context: This is a reference stock image. "
            + f'Title: "{title}". '
            + 'Set metadata.scene_number to the asset id when filling the object; use "'
            + item_id
            + '" for this image. Prefer metadata.series "cloning" and metadata.task "clone" when appropriate. '
            + "The scene, style, and related fields you output must describe original stock imagery in the spirit of that title and image-not a pixel-perfect or slavish recreation-so a generated image can be clearly distinct from the reference."
        )
        return default_prompt

    def _build_video_instruction(self, image_clone_prompt: str, camera_motion: str, duration: str, negative_prompt: str, title: str) -> str:
        default_prompt = (
            "You are a structured JSON generator for Nexus DNA video cloning.\n"
            "Return ONLY one JSON object. No markdown fences, no analysis, no extra text.\n\n"
            "The object MUST conform to this schema (same structure and nesting; fill every section appropriately for video):\n"
            + _template_text()
            + "\n\nNexus DNA video cloning instructions:\n"
            "- The reference JSON below was used to generate a cloned STILL image. Produce a NEW object of the SAME SCHEMA tailored for I2V (start-frame video): temporal beats, motion-aware scene and style wording, and meaningful timeline[].action entries across the clip.\n"
            "- Preserve the general subject theme but optimize for movement and duration; do not copy the still prompt verbatim.\n"
            "- Prefer metadata.series 'cloning' and metadata.task that reflects video (e.g. clone_video) when appropriate.\n"
            "- The server will overwrite shot.camera_motion, export.target_duration_sec, and merge negative prompts; align your draft with these targets:\n"
            + f"  camera_motion: {json.dumps(str(camera_motion or '').strip())}\n"
            + f"  target_duration_sec: {json.dumps(str(duration or '').strip())}\n"
            + f"  extra avoid terms: {json.dumps(str(negative_prompt or '').strip() or '(none)')}\n"
            + (f"  reference stock title (concept only; never render as text): {json.dumps(str(title or '').strip())}\n" if title else "")
            + "\nReference image-clone JSON:\n"
            + image_clone_prompt
        )
        return default_prompt

    def _build_metadata_prompt(self, metadata_settings: Dict[str, Any], dna_no_bg: bool) -> str:
        meta = metadata_settings or {}
        language_code = str(meta.get("language") or "en").strip().lower()
        language_name = {
            "en": "English",
            "fr": "French",
            "de": "German",
            "es": "Spanish",
            "it": "Italian",
            "pt": "Portuguese",
            "ja": "Japanese",
            "pl": "Polish",
            "ko": "Korean",
        }.get(language_code, "English")
        asset_type = str(meta.get("assetType") or "photo").strip() or "photo"
        title_min = int(meta.get("titleMin", 50) or 50)
        title_max = int(meta.get("titleMax", 80) or 80)
        keyword_min = int(meta.get("keywordMin", 32) or 32)
        keyword_max = int(meta.get("keywordMax", 50) or 50)
        desc_min = int(meta.get("descriptionMin", 0) or 0)
        desc_max = int(meta.get("descriptionMax", 0) or 0)
        platforms = ", ".join([str(x).strip() for x in (meta.get("platforms") or ["adobe-stock"]) if str(x).strip()]) or "adobe-stock"
        title_style = str(meta.get("titleStyle") or "seo-optimized")
        keyword_types = meta.get("keywordTypes") or {}
        mixed = bool(keyword_types.get("mixed"))
        double = bool(keyword_types.get("doubleWord"))
        keyword_shape = (
            "Each keyword may be one to three words: use a mix of single-word tokens and two-word phrases where they add disambiguation; never exceed three words per keyword token."
            if mixed
            else ("Each keyword MUST be exactly two words." if double else "Each keyword MUST be a single English token.")
        )
        description_rule = '`description` MUST be exactly "" (empty string).' if desc_max == 0 else f"`description` MUST be between {desc_min} and {desc_max} characters (plain text, no HTML)."
        include_cat = bool(meta.get("includeCategory"))
        category_block = (
            (
                "Adobe Stock category table — classify the attached image with exactly ONE `categoryId` from this list "
                "(integers 1–21 only). Choose the single best-matching row for what is visible:\n"
                + _adobe_stock_category_table_prompt_lines()
                + "\n\n"
                "For Adobe Stock taxonomy, output ONLY the integer field `categoryId` in the metadata object. "
                "Do not output a separate `category` string field for Adobe.\n\n"
            )
            if include_cat
            else ""
        )
        releases = (
            "Releases: if the image shows identifiable people, private property, or prominent branded products, buyers may need model or property releases."
            if bool(meta.get("includeReleases"))
            else ""
        )
        bg_line = (
            "Transparent-background contract: this request is for a transparent PNG/cutout asset. "
            "Transparent alpha may appear black, dark, white, gray, or checkerboard in some viewers or provider previews; that viewer surface is NOT part of the asset. "
            "Describe the asset as isolated on transparent background, transparent background, cutout, or isolated transparent PNG. "
            "Do not say solid black background, black background, dark background, white background, gray background, studio background, or any other background color."
            if bool(meta.get("transparentBackground")) or dna_no_bg
            else "Do not suggest transparent cutout language unless the image is clearly transparent."
        )
        custom_cfg = meta.get("customPrompt") or {}
        custom = (
            "\n\nAdditional client rules (must follow):\n" + str(custom_cfg.get("text") or "").strip()
            if bool(custom_cfg.get("enabled")) and str(custom_cfg.get("text") or "").strip()
            else ""
        )
        title_style_line = f'Follow this title style label: "{title_style}".'
        optional_key = ', "categoryId": 12' if include_cat else ""
        optional_instruction = (
            "categoryId is REQUIRED; it MUST be one of the integers listed in the Adobe Stock category table above."
            if include_cat
            else "Do not include categoryId or Adobe category fields in the JSON."
        )
        title_length = f"Title MUST be between {title_min} and {title_max} characters inclusive."
        keyword_count = f"Generate between {keyword_min} and {keyword_max} keywords inclusive."
        default_prompt = (
            "You are generating agency microstock metadata for exactly ONE image (attached).\n\n"
            f"Output language: Write the title, keywords, and non-empty description strictly in {language_name}.\n"
            f"Adobe asset type: {asset_type}.\n\n"
            "CRITICAL LENGTH (stay as close as possible):\n"
            f"* {title_length}\n"
            f"* {keyword_count}\n"
            f"* {keyword_shape}\n"
            f"* {description_rule}\n\n"
            "Target platforms:\n"
            f"* Optimize for: {platforms}.\n\n"
            "Title content:\n"
            f"* {title_style_line}\n"
            "* Sentence case for the title; no clickbait; no redundant site names.\n"
            "* Do not put keyword lists in the title.\n\n"
            "Keywords content:\n"
            f"* Write keywords in {language_name}; retain a proper noun in its established form only when necessary.\n"
            "* No duplicate or near-duplicate keywords; no camera serials; no filler words image, photo, picture.\n"
            "* No hashtags.\n\n"
            "Safety / accuracy:\n"
            "* Describe only what is visible. Do not invent people, brands, or locations.\n"
            "* No URLs, email addresses, or watermark text in any field.\n\n"
            + category_block
            + (f"{releases}\n\n" if releases else "")
            + f"{bg_line}\n"
            + custom
            + "\n\nOUTPUT - respond with ONLY valid JSON (no markdown fences, no commentary before or after). Use exactly this shape with one object inside the array:\n"
            "{\n"
            '  "metadataSets": [\n'
            "    {\n"
            '      "title": "...",\n'
            '      "keywords": ["word1", "word2"],\n'
            '      "description": ""'
            + optional_key
            + "\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            + f"Where keywords is a JSON array of strings ({keyword_count}). {optional_instruction}\n\n"
            "REMINDER: Return ONLY the JSON object."
        )
        return default_prompt

    async def generate_cloning_prompts(
        self,
        images: List[Dict[str, Any]],
        provider: Optional[str],
        model: Optional[str],
        fallback_models: Optional[List[str]],
    ) -> Dict[str, Any]:
        provider_chain = self._llm.resolve_provider_chain(
            provider,
            provider_order_csv=app_config.sub2gen_cloning_provider_order,
            enabled_providers_csv=app_config.sub2gen_cloning_enabled_providers,
            legacy_backend=app_config.sub2gen_cloning_backend or "gemini_native",
            allowed_providers=CLONING_PROVIDERS,
        )
        selected_model = (model or app_config.sub2gen_cloning_model or "gemini-2.5-flash").strip()
        configured_fallback = get_csv(app_config.sub2gen_metadata_fallback_models)
        effective_fallback = fallback_models
        if effective_fallback is None and "cliproxy" in provider_chain:
            effective_fallback = [candidate for candidate in configured_fallback if candidate != selected_model]
        retry_count = normalized_retry_count(app_config.sub2gen_cloning_provider_retry_count)
        out: List[Dict[str, Any]] = []
        deadline_at = time.monotonic() + CLONING_PROMPTS_DEADLINE_SECONDS
        for image in images:
            image_bytes, mime_type = await self._fetch_image(
                image.get("image_url"),
                image.get("image_base64"),
                deadline_at,
                image.get("mimeType") or image.get("mime_type"),
            )
            prompt = self._build_clone_instruction(image)
            response_json = await self._llm.invoke_with_provider_chain(
                providers=provider_chain,
                retry_count=retry_count,
                model=selected_model,
                fallback_models=effective_fallback,
                prompt_text=prompt,
                image_bytes=image_bytes,
                mime_type=str(image.get("mimeType") or mime_type),
                use_cloning_credentials=True,
                deadline_at=deadline_at,
            )
            out.append(_ensure_meaningful_image_prompt(_normalize_image_prompt(response_json)))
        return {"prompts": out}

    async def generate_cloning_video_prompt(
        self,
        payload: Dict[str, Any],
        provider: Optional[str],
        model: Optional[str],
        fallback_models: Optional[List[str]],
    ) -> Dict[str, Any]:
        provider_chain = self._llm.resolve_provider_chain(
            provider,
            provider_order_csv=app_config.sub2gen_cloning_provider_order,
            enabled_providers_csv=app_config.sub2gen_cloning_enabled_providers,
            legacy_backend=app_config.sub2gen_cloning_backend or "gemini_native",
            allowed_providers=CLONING_PROVIDERS,
        )
        selected_model = (model or app_config.sub2gen_cloning_model or "gemini-2.5-flash").strip()
        configured_fallback = get_csv(app_config.sub2gen_metadata_fallback_models)
        effective_fallback = fallback_models
        if effective_fallback is None and "cliproxy" in provider_chain:
            effective_fallback = [candidate for candidate in configured_fallback if candidate != selected_model]
        retry_count = normalized_retry_count(app_config.sub2gen_cloning_provider_retry_count)
        clone_prompt_raw = payload.get("imageClonePrompt") or ""
        try:
            clone_prompt_json = json.dumps(json.loads(clone_prompt_raw), ensure_ascii=False, indent=2)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid imageClonePrompt: {exc}") from exc
        instruction = self._build_video_instruction(
            clone_prompt_json,
            str(payload.get("cameraMotion") or ""),
            str(payload.get("duration") or ""),
            str(payload.get("negativePrompt") or ""),
            str(payload.get("title") or ""),
        )
        image_bytes = None
        mime_type = "image/jpeg"
        if payload.get("image_base64"):
            image_bytes, mime_type = await self._fetch_image(
                None,
                payload.get("image_base64"),
                mime_type_hint=payload.get("mimeType") or payload.get("mime_type"),
            )
        response_json = await self._llm.invoke_with_provider_chain(
            providers=provider_chain,
            retry_count=retry_count,
            model=selected_model,
            fallback_models=effective_fallback,
            prompt_text=instruction,
            image_bytes=image_bytes,
            mime_type=str(payload.get("mimeType") or mime_type),
            use_cloning_credentials=True,
        )
        merged = deepcopy(DEFAULT_TEMPLATE)
        merged.update({k: v for k, v in response_json.items() if k in merged and not isinstance(v, dict)})
        for key in ("shot", "voice_over", "house_settings", "lighting", "audio", "text_rules", "color_palette", "transitions", "vfx_rules", "visual_rules", "export", "metadata"):
            if isinstance(response_json.get(key), dict):
                merged[key].update(response_json[key])
        if isinstance(response_json.get("timeline"), list):
            for i, seg in enumerate(response_json["timeline"][: len(merged["timeline"])]):
                if isinstance(seg, dict) and isinstance(seg.get("action"), str):
                    merged["timeline"][i]["action"] = seg["action"]
        merged["shot"]["camera_motion"] = str(payload.get("cameraMotion") or "").strip()
        merged["export"]["target_duration_sec"] = str(payload.get("duration") or "").strip()
        negatives = [x.strip() for x in str(payload.get("negativePrompt") or "").split(",") if x.strip()]
        existing = merged.get("visual_rules", {}).get("prohibited_elements") or []
        if not isinstance(existing, list):
            existing = []
        merged["visual_rules"]["prohibited_elements"] = list(dict.fromkeys([*existing, *negatives]))
        return {"prompt": json.dumps(merged, ensure_ascii=False)}

    async def generate_metadata(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        explicit_provider = str(payload.get("backend") or "").strip().lower() or None
        saved_metadata_backend = str(app_config.sub2gen_metadata_backend or "").strip().lower()
        # UI "Use CSVGEN Only for Metadata" sets sub2gen_metadata_backend to csvgen but still
        # persists a ranked provider_order; resolve_provider_chain would otherwise try OpenRouter first.
        if explicit_provider:
            provider_chain = self._llm.resolve_provider_chain(
                explicit_provider,
                provider_order_csv=app_config.sub2gen_metadata_provider_order,
                enabled_providers_csv=app_config.sub2gen_metadata_enabled_providers,
                legacy_backend=saved_metadata_backend or "gemini_native",
                allowed_providers=METADATA_PROVIDERS,
            )
        elif saved_metadata_backend == "csvgen":
            provider_chain = ["csvgen"]
        else:
            provider_chain = self._llm.resolve_provider_chain(
                None,
                provider_order_csv=app_config.sub2gen_metadata_provider_order,
                enabled_providers_csv=app_config.sub2gen_metadata_enabled_providers,
                legacy_backend=saved_metadata_backend or "gemini_native",
                allowed_providers=METADATA_PROVIDERS,
            )
        retry_count = normalized_retry_count(app_config.sub2gen_metadata_provider_retry_count)
        backend = provider_chain[0] if provider_chain else "gemini_native"
        configured_primary = str(
            app_config.sub2gen_metadata_primary_model
            or app_config.sub2gen_metadata_model
            or "gemini-2.5-flash"
        ).strip()
        configured_enabled = get_csv(app_config.sub2gen_metadata_enabled_models)
        configured_fallback = get_csv(app_config.sub2gen_metadata_fallback_models)
        if not configured_enabled:
            configured_enabled = [configured_primary, *configured_fallback]
        configured_enabled = list(dict.fromkeys([m for m in configured_enabled if m]))
        if configured_primary not in configured_enabled:
            configured_enabled.insert(0, configured_primary)
        default_fallback_chain = [m for m in configured_enabled if m != configured_primary]
        if configured_fallback:
            default_fallback_chain = list(
                dict.fromkeys([m for m in configured_fallback if m and m != configured_primary] + default_fallback_chain)
            )

        model = str(payload.get("model") or configured_primary).strip()
        fallback_models = payload.get("fallbackModels") or default_fallback_chain
        image_bytes, mime_type = await self._fetch_image(
            payload.get("image_url"),
            payload.get("image_base64"),
            mime_type_hint=payload.get("mimeType") or payload.get("mime_type"),
        )
        metadata_settings = payload.get("metadataSettings") or {}
        include_category = bool(metadata_settings.get("includeCategory"))
        prompt = self._build_metadata_prompt(metadata_settings, bool(payload.get("dnaNoBgWorkflowActive")))
        last_err: Optional[Exception] = None
        attempt_failures: List[str] = []

        for provider_name in provider_chain:
            for attempt in range(retry_count + 1):
                try:
                    if provider_name == "csvgen":
                        cookie = str(app_config.sub2gen_csvgen_cookie or "").strip()
                        if not cookie:
                            raise HTTPException(status_code=400, detail="CSVGEN cookie not configured")
                        b64 = base64.b64encode(image_bytes).decode("ascii")
                        mt = str(mime_type or "image/jpeg").split(";", 1)[0].strip() or "image/jpeg"
                        if "/" not in mt:
                            mt = "image/jpeg"
                        data_url = f"data:{mt};base64,{b64}"
                        keys_csv = str(app_config.sub2gen_csvgen_api_keys or "")
                        api_keys_list = [s.strip() for s in keys_csv.split(",") if s.strip()]
                        settings_payload = (
                            dict(metadata_settings) if isinstance(metadata_settings, dict) else {}
                        )
                        platforms_norm = [
                            str(p).strip().lower()
                            for p in (metadata_settings.get("platforms") or [])
                            if str(p).strip()
                        ]
                        if include_category and "adobe-stock" in platforms_norm:
                            settings_payload["categories"] = _adobe_stock_categories_for_csvgen_settings()
                        csvgen_model = str(model or "").strip() or "@cf/moonshotai/kimi-k2.5"
                        body = {
                            "image": data_url,
                            "fileType": "image",
                            "provider": "csvgen",
                            "model": csvgen_model,
                            "apiKeys": api_keys_list,
                            "settings": settings_payload,
                        }
                        async with AsyncSession() as session:
                            resp = await session.post(
                                "https://www.csvgen.com/api/generate-metadata",
                                headers={
                                    "Content-Type": "application/json",
                                    "Accept": "*/*",
                                    "Origin": "https://www.csvgen.com",
                                    "Referer": "https://www.csvgen.com/app",
                                    # csvgen rejects requests without this (and often a browser UA); matches web app.
                                    "x-csvgen-client": "web-app",
                                    "User-Agent": (
                                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                                        "Chrome/131.0.0.0 Safari/537.36"
                                    ),
                                    "Cookie": cookie,
                                },
                                json=body,
                                timeout=120,
                            )
                            text = resp.text
                            try:
                                data = json.loads(text)
                            except Exception:
                                raise HTTPException(
                                    status_code=resp.status_code if resp.status_code >= 400 else 500,
                                    detail=text[:500] or "Invalid JSON from csvgen",
                                )
                            if resp.status_code >= 400:
                                raise HTTPException(
                                    status_code=resp.status_code,
                                    detail={
                                        "error": data.get("error") or data.get("message") or "csvgen request failed",
                                        "details": data,
                                    },
                                )
                            return self._normalize_csvgen_response(data, include_category=include_category)

                    parsed = await self._llm.invoke_model_json(
                        provider=provider_name,
                        model=model,
                        fallback_models=fallback_models,
                        prompt_text=prompt,
                        image_bytes=image_bytes,
                        mime_type=mime_type,
                    )
                    row = parsed.get("metadataSets", [{}])[0] if isinstance(parsed.get("metadataSets"), list) else parsed
                    return self._normalize_csvgen_response(
                        {"optionA": row, "optionB": row},
                        include_category=include_category,
                    )
                except Exception as exc:
                    last_err = exc
                    attempt_failures.append(f"{provider_name}#{attempt + 1}: {exc}")
                    if attempt < retry_count and is_retryable_error(exc):
                        continue
                    break

        detail = str(last_err or "Metadata generation failed")
        if attempt_failures:
            detail = f"{detail} | attempts: {'; '.join(attempt_failures[-6:])}"
        raise HTTPException(status_code=500, detail=detail)

    def _normalize_csvgen_response(
        self,
        data: Dict[str, Any],
        *,
        include_category: bool = False,
    ) -> Dict[str, Any]:
        def coerce(raw: Any) -> Dict[str, Any]:
            if not isinstance(raw, dict):
                return {"title": "", "keywords": "", "description": ""}
            keywords = raw.get("keywords")
            if isinstance(keywords, list):
                keywords = ", ".join([str(x) for x in keywords])
            if not isinstance(keywords, str):
                keywords = ""
            out: Dict[str, Any] = {
                "title": str(raw.get("title") or ""),
                "keywords": keywords,
                "description": str(raw.get("description") or ""),
            }
            if include_category:
                rid = _resolve_adobe_stock_category_id(raw)
                if rid is not None:
                    out["categoryId"] = rid
            return out

        a = coerce(data.get("optionA") if isinstance(data, dict) else None)
        b = coerce(data.get("optionB") if isinstance(data, dict) else None)
        # csvgen may return a single flat object (title/keywords/description) instead of optionA/optionB.
        if not any(a.values()) and not any(b.values()) and isinstance(data, dict):
            flat = coerce(data)
            if any(flat.values()):
                a = flat
                b = deepcopy(flat)
        if not any(a.values()) and any(b.values()):
            a = deepcopy(b)
        if not any(b.values()) and any(a.values()):
            b = deepcopy(a)
        base: Dict[str, Any] = {"optionA": a, "optionB": b}
        if isinstance(data, dict) and "creditsRemaining" in data:
            base["creditsRemaining"] = data["creditsRemaining"]
        return base
