"""Studio-facing metadata for concrete sub2gen generation models.

The OpenAI-compatible model list remains the public transport.  This module only
adds an optional ``studio`` field so clients can group concrete model variants
without reverse-engineering model ids.
"""

from __future__ import annotations

import re
import json
from typing import Any, Dict, Iterable, List


_ASPECTS = {
    "IMAGE_ASPECT_RATIO_LANDSCAPE": "16:9",
    "IMAGE_ASPECT_RATIO_PORTRAIT": "9:16",
    "IMAGE_ASPECT_RATIO_SQUARE": "1:1",
    "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE": "4:3",
    "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR": "3:4",
    "VIDEO_ASPECT_RATIO_LANDSCAPE": "16:9",
    "VIDEO_ASPECT_RATIO_PORTRAIT": "9:16",
}

_IMAGE_FAMILIES = {
    "GEM_PIX": ("gemini-2.5-flash-image", "Gemini 2.5 Flash Image"),
    "GEM_PIX_2": ("gemini-3.0-pro-image", "Gemini 3.0 Pro Image"),
    "IMAGEN_3_5": ("imagen-4.0", "Imagen 4.0"),
    "NARWHAL": ("gemini-3.1-flash-image", "Gemini 3.1 Flash Image"),
}


def _native_video_family(config: Dict[str, Any]) -> tuple[str, str]:
    video_type = str(config.get("video_type") or "video")
    model_key = str(config.get("model_key") or "veo-3.1")
    quality = " Lite" if "lite" in model_key else " Fast" if "fast" in model_key else " Quality"
    label = f"Veo 3.1{quality} {video_type.upper()}"
    return (f"veo-3.1-{quality.strip().lower()}-{video_type}", label)


def _native_resolution(config: Dict[str, Any]) -> str:
    upsample = config.get("upsample")
    if isinstance(upsample, dict):
        value = str(upsample.get("resolution") or "")
        if "4K" in value:
            return "4K"
        if "1080" in value:
            return "1080p"
    elif isinstance(upsample, str):
        if "4K" in upsample:
            return "4K"
        if "2K" in upsample:
            return "2K"
    return "720p" if config.get("type") == "video" else "1K"


def _duration_from_id(model_id: str) -> str | None:
    match = re.search(r"(?:_|-)(\d+)s$", model_id)
    return f"{match.group(1)}s" if match else None


def native_studio_metadata(model_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a native MODEL_CONFIG row into stable studio metadata."""
    kind = str(config.get("type") or "")
    aspect = _ASPECTS.get(str(config.get("aspect_ratio") or ""))
    if kind == "image":
        family_id, family_name = _IMAGE_FAMILIES.get(
            str(config.get("model_name") or ""), (str(config.get("model_name") or model_id), model_id)
        )
        modes = ["text_to_image", "image_to_image"]
        min_images, max_images = 0, 14
    else:
        family_id, family_name = _native_video_family(config)
        video_type = str(config.get("video_type") or "t2v")
        modes = {"t2v": ["text_to_video"], "i2v": ["image_to_video"], "r2v": ["reference_to_video"], "extend": ["video_extend"]}.get(video_type, [video_type])
        min_images, max_images = int(config.get("min_images") or 0), int(config.get("max_images") or 0)
    variant: Dict[str, Any] = {"resolution": _native_resolution(config)}
    if aspect:
        variant["aspect_ratio"] = aspect
    duration = _duration_from_id(model_id)
    if duration:
        variant["duration"] = duration
    return {
        "provider": "native",
        "kind": kind,
        "family_id": f"native:{family_id}",
        "family_name": family_name,
        "modes": modes,
        "variant": variant,
        "input": {"min_images": min_images, "max_images": max_images},
    }


def geminigen_studio_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    options = item.get("options") if isinstance(item.get("options"), dict) else {}
    kind = str(item.get("kind") or "")
    model_key = str(options.get("model") or item.get("id") or "geminigen")
    if kind == "image" and item.get("endpoint_type") == "grok-image":
        family_id, family_name = "grok-image", "Grok Image"
    elif kind == "image":
        family_id, family_name = model_key, f"GeminiGen {model_key.replace('-', ' ').title()}"
    else:
        family_id, family_name = model_key, f"GeminiGen {model_key.replace('-', ' ').title()}"
    variant = {k: str(v) for k, v in options.items() if k in {"aspect_ratio", "resolution", "duration", "orientation"} and v is not None}
    if variant.get("duration", "").isdigit():
        variant["duration"] = f"{variant['duration']}s"
    modes = ["text_to_image", "image_to_image"] if kind == "image" else ["text_to_video"]
    reference_mode = str(options.get("reference_mode") or "none")
    if kind == "video" and reference_mode == "frame":
        modes = ["image_to_video"]
    elif kind == "video" and reference_mode == "ingredient":
        modes = ["reference_to_video"]
    return {
        "provider": "geminigen",
        "kind": kind,
        "family_id": f"geminigen:{family_id}",
        "family_name": family_name,
        "modes": modes,
        "variant": variant,
        "input": {"min_images": 1 if reference_mode != "none" else 0, "max_images": 14 if kind == "image" else 3},
    }


def runway_studio_metadata(model: Any) -> Dict[str, Any]:
    def load(value: Any, fallback: Any) -> Any:
        if isinstance(value, type(fallback)):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, type(fallback)):
                    return parsed
            except json.JSONDecodeError:
                pass
        return fallback
    schema = load(getattr(model, "capability_schema", None), {})
    modes = load(getattr(model, "supported_modes", None), [])
    def enum(name: str) -> List[str]:
        value = schema.get(name) if isinstance(schema.get(name), dict) else {}
        return [str(v) for v in value.get("enum", [])] if isinstance(value, dict) else []
    variant_options = {name: enum(name) for name in ("aspect_ratio", "resolution", "image_size", "duration")}
    return {
        "provider": "runway",
        "kind": model.kind,
        "family_id": f"runway:{model.public_model_id}",
        "family_name": model.display_name,
        "modes": modes,
        "variant": {},
        "capabilities": {key: value for key, value in variant_options.items() if value},
        "input": {"min_images": 0, "max_images": int(load(getattr(model, "limits", None), {}).get("max_reference_images", 0))},
    }
