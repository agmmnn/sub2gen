"""Static GeminiGen Max model manifest."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


GEMINIGEN_MANIFEST_VERSION = "2026-06-22"

ASPECTS = {
    "square": "1:1",
    "landscape": "16:9",
    "portrait": "9:16",
    "three-four": "3:4",
    "four-three": "4:3",
    "vertical": "2:3",
    "horizontal": "3:2",
}

IMAGEN_MODELS = {
    "nano-banana-pro": "Nano Banana Pro",
    "nano-banana-2": "Nano Banana 2",
}

VEO_MODELS = {
    "veo-3.1-fast": "Veo 3.1 Fast",
    "veo-3.1-lite": "Veo 3.1 Lite",
}


def _title(token: str) -> str:
    return token.replace("-", " ").title()


def build_geminigen_manifest() -> List[Dict[str, Any]]:
    models: List[Dict[str, Any]] = []

    for model_key, display in IMAGEN_MODELS.items():
        for aspect in ("square", "landscape", "portrait", "three-four", "four-three"):
            for resolution in ("1k", "2k", "4k"):
                public_id = f"geminigen-{model_key}-image-{aspect}-{resolution}"
                models.append(
                    {
                        "id": public_id,
                        "display_name": f"GeminiGen {display} Image {_title(aspect)} {resolution.upper()}",
                        "kind": "image",
                        "endpoint_type": "imagen",
                        "options": {
                            "model": model_key,
                            "aspect_ratio": ASPECTS[aspect],
                            "resolution": resolution.upper(),
                            "output_format": "png",
                        },
                    }
                )

    for orientation in ("landscape", "portrait", "square", "vertical", "horizontal"):
        for mode in ("speed", "quality"):
            public_id = f"geminigen-grok-image-{orientation}-{mode}"
            models.append(
                {
                    "id": public_id,
                    "display_name": f"GeminiGen Grok Image {_title(orientation)} {_title(mode)}",
                    "kind": "image",
                    "endpoint_type": "grok-image",
                    "options": {
                        "orientation": orientation,
                        "mode": "normal" if mode == "speed" else "quality",
                        "num_result": 1,
                    },
                }
            )

    for model_key, display in VEO_MODELS.items():
        for mode in ("t2v",):
            for aspect in ("landscape", "portrait"):
                for resolution in ("720p", "1080p"):
                    for duration in ("4s", "6s", "8s"):
                        public_id = f"geminigen-{model_key}-{mode}-{aspect}-{resolution}-{duration}"
                        models.append(
                            {
                                "id": public_id,
                                "display_name": f"GeminiGen {display} T2V {_title(aspect)} {resolution} {duration}",
                                "kind": "video",
                                "endpoint_type": "veo-video",
                                "options": {
                                    "model": "veo-3-fast" if model_key == "veo-3.1-fast" else model_key,
                                    "aspect_ratio": ASPECTS[aspect],
                                    "resolution": resolution,
                                    "duration": duration.rstrip("s"),
                                    "service_mode": "unstable",
                                    "reference_mode": "none",
                                },
                            }
                        )

    for ref_mode in ("frame", "ingredient"):
        for aspect in ("landscape", "portrait"):
            for resolution in ("720p", "1080p"):
                durations = ("8s",) if ref_mode == "ingredient" else ("4s", "6s", "8s")
                for duration in durations:
                    public_id = f"geminigen-veo-3.1-fast-i2v-{ref_mode}-{aspect}-{resolution}-{duration}"
                    models.append(
                        {
                            "id": public_id,
                            "display_name": f"GeminiGen Veo 3.1 Fast I2V {_title(ref_mode)} {_title(aspect)} {resolution} {duration}",
                            "kind": "video",
                            "endpoint_type": "veo-video",
                            "options": {
                                "model": "veo-3-fast",
                                "aspect_ratio": ASPECTS[aspect],
                                "resolution": resolution,
                                "duration": duration.rstrip("s"),
                                "service_mode": "unstable",
                                "reference_mode": ref_mode,
                            },
                        }
                    )

    return models


GEMINIGEN_MODEL_MANIFEST = build_geminigen_manifest()
GEMINIGEN_MODEL_BY_ID = {model["id"]: model for model in GEMINIGEN_MODEL_MANIFEST}


def geminigen_manifest_entry(public_model_id: str) -> Optional[Dict[str, Any]]:
    return GEMINIGEN_MODEL_BY_ID.get((public_model_id or "").strip())
