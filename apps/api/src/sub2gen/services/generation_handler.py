"""Generation handler for sub2gen"""
import asyncio
import base64
import json
import time
import re
from datetime import datetime
from urllib.parse import quote
from typing import Optional, AsyncGenerator, List, Dict, Any
from ..core.logger import debug_logger
from ..core.config import config, get_runtime_tmp_dir
from ..core.monitoring import record_generation_result
from ..core.models import RequestLog, Task, Token
from ..generation import (
    ImageGenerationPipeline,
    VideoGenerationPipeline,
    create_generation_result,
    create_response_state,
    mark_generation_failed,
    mark_generation_succeeded,
)
from ..core.account_tiers import (
    PAYGATE_TIER_NOT_PAID,
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from .file_cache import FileCache
from .extension_generation_service import ExtensionGenerationService
from .flow_client import classify_recaptcha_upstream_failure


# Model configuration
MODEL_CONFIG = {
    # 图片生成 - GEM_PIX (Gemini 2.5 Flash)
    "gemini-2.5-flash-image-landscape": {
        "type": "image",
        "model_name": "GEM_PIX",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "gemini-2.5-flash-image-portrait": {
        "type": "image",
        "model_name": "GEM_PIX",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },

    # 图片生成 - GEM_PIX_2 (Gemini 3.0 Pro)
    "gemini-3.0-pro-image-landscape": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "gemini-3.0-pro-image-portrait": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },
    "gemini-3.0-pro-image-square": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE"
    },
    "gemini-3.0-pro-image-four-three": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
    },
    "gemini-3.0-pro-image-three-four": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
    },

    # 图片生成 - GEM_PIX_2 (Gemini 3.0 Pro) 2K 放大版
    "gemini-3.0-pro-image-landscape-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-portrait-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-square-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-four-three-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-three-four-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },

    # 图片生成 - GEM_PIX_2 (Gemini 3.0 Pro) 4K 放大版
    "gemini-3.0-pro-image-landscape-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-portrait-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-square-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-four-three-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-three-four-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },

    # 图片生成 - IMAGEN_3_5 (Imagen 4.0)
    "imagen-4.0-generate-preview-landscape": {
        "type": "image",
        "model_name": "IMAGEN_3_5",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "imagen-4.0-generate-preview-portrait": {
        "type": "image",
        "model_name": "IMAGEN_3_5",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },

    # 图片生成 - NARWHAL (新版)
    "gemini-3.1-flash-image-landscape": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "gemini-3.1-flash-image-portrait": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },
    "gemini-3.1-flash-image-square": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE"
    },
    "gemini-3.1-flash-image-four-three": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
    },
    "gemini-3.1-flash-image-three-four": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
    },
    "gemini-3.1-flash-image-landscape-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-portrait-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-square-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-four-three-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-three-four-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-landscape-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-portrait-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-square-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-four-three-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-three-four-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },

    # ========== 文生视频 (T2V - Text to Video) ==========
    # 不支持上传图片，只使用文本提示词生成

    # veo_3_1_t2v_fast_portrait (竖屏)
    # 上游模型名: veo_3_1_t2v_fast_portrait
    "veo_3_1_t2v_fast_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    # veo_3_1_t2v_fast_landscape (横屏)
    # 上游模型名: veo_3_1_t2v_fast
    "veo_3_1_t2v_fast_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_3_1_t2v_fast_ultra (横竖屏)
    "veo_3_1_t2v_fast_portrait_ultra": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_3_1_t2v_fast_ultra": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_3_1_t2v_fast_ultra_relaxed (横竖屏)
    "veo_3_1_t2v_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_3_1_t2v_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },

    # veo_3_1_t2v (横竖屏)
    "veo_3_1_t2v_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False
    },
    "veo_3_1_t2v_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False
    },
    # veo_3_1_t2v_lite (横竖屏，来自 labs.google.har)
    "veo_3_1_t2v_lite_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    "veo_3_1_t2v_lite_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },


    # ========== 视频续写 (Video Extend) ==========
    "veo_3_1_extend_portrait": {
        "type": "video",
        "video_type": "extend",
        "model_key": "veo_3_1_extend_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "allow_tier_upgrade": False
    },
    "veo_3_1_extend": {
        "type": "video",
        "video_type": "extend",
        "model_key": "veo_3_1_extend_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "allow_tier_upgrade": False
    },

    # ========== 首尾帧模型 (I2V - Image to Video) ==========
    # 支持1-2张图片：1张作为首帧，2张作为首尾帧

    # veo_3_1_i2v_s_fast_fl (需要新增横竖屏)
    "veo_3_1_i2v_s_fast_portrait_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s_fast_ultra (横竖屏)
    "veo_3_1_i2v_s_fast_portrait_ultra_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_ultra_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s_fast_ultra_relaxed (需要新增横竖屏)
    "veo_3_1_i2v_s_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s (需要新增横竖屏)
    "veo_3_1_i2v_s_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    # veo_3_1_i2v_lite (横竖屏，仅首帧，来自 labs.google.har)
    "veo_3_1_i2v_lite_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 1,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    "veo_3_1_i2v_lite_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 1,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    # veo_3_1_interpolation_lite (横竖屏，首尾帧，来自 labs.google.har)
    "veo_3_1_interpolation_lite_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_interpolation_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 2,
        "max_images": 2,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    "veo_3_1_interpolation_lite_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_interpolation_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 2,
        "max_images": 2,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },

    # ========== 多图生成 (R2V - Reference Images to Video) ==========
    # 当前上游协议最多支持 3 张参考图

    # veo_3_1_r2v_fast (横竖屏)
    "veo_3_1_r2v_fast_portrait": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # veo_3_1_r2v_fast_ultra (横竖屏)
    "veo_3_1_r2v_fast_portrait_ultra": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast_ultra": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # veo_3_1_r2v_fast_ultra_relaxed (横竖屏)
    "veo_3_1_r2v_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # ========== 视频放大 (Video Upsampler) ==========
    # 仅 3.1 支持，需要先生成视频后再放大，可能需要 30 分钟

    # T2V 4K 放大版
    "veo_3_1_t2v_fast_portrait_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_portrait_ultra_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_ultra_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # T2V 1080P 放大版
    "veo_3_1_t2v_fast_portrait_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_portrait_ultra_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_ultra_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },

    # I2V 4K 放大版
    "veo_3_1_i2v_s_fast_portrait_ultra_fl_4k": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_i2v_s_fast_ultra_fl_4k": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # I2V 1080P 放大版
    "veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_i2v_s_fast_ultra_fl_1080p": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },

    # R2V 4K 放大版
    "veo_3_1_r2v_fast_portrait_ultra_4k": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_r2v_fast_ultra_4k": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # R2V 1080P 放大版
    "veo_3_1_r2v_fast_portrait_ultra_1080p": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_r2v_fast_ultra_1080p": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "omni": {
        "type": "video",
        "video_type": "omni",
        "model_key": "abra_t2v_8s",
        "reference_model_key": "abra_r2v_8s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False,
        "reference_duration": 8,
        "reference_model_display_name": "Omni Flash",
    },
    "omni_portrait": {
        "type": "video",
        "video_type": "omni",
        "model_key": "abra_t2v_8s",
        "reference_model_key": "abra_r2v_8s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False,
        "reference_duration": 8,
        "reference_model_display_name": "Omni Flash",
    },
}


def _make_t2v_config(
    model_key: str,
    aspect_ratio: str,
    *,
    use_v2_model_config: bool = False,
    allow_tier_upgrade: bool = True,
    upsample: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "type": "video",
        "video_type": "t2v",
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
        "supports_images": False,
    }
    if use_v2_model_config:
        cfg["use_v2_model_config"] = True
    if not allow_tier_upgrade:
        cfg["allow_tier_upgrade"] = False
    if upsample:
        cfg["upsample"] = upsample
    return cfg


def _make_i2v_config(
    model_key: str,
    aspect_ratio: str,
    *,
    min_images: int = 1,
    max_images: int = 2,
    use_v2_model_config: bool = False,
    allow_tier_upgrade: bool = True,
    upsample: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "type": "video",
        "video_type": "i2v",
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
        "supports_images": True,
        "min_images": min_images,
        "max_images": max_images,
    }
    if use_v2_model_config:
        cfg["use_v2_model_config"] = True
    if not allow_tier_upgrade:
        cfg["allow_tier_upgrade"] = False
    if upsample:
        cfg["upsample"] = upsample
    return cfg


def _apply_veo_3_1_model_updates():
    """Keep the public aliases aligned with the current Veo 3.1 model families."""
    landscape = "VIDEO_ASPECT_RATIO_LANDSCAPE"
    portrait = "VIDEO_ASPECT_RATIO_PORTRAIT"
    
    def add_alias(alias: str, target: str):
        MODEL_CONFIG[alias] = dict(MODEL_CONFIG[target])

    def add_default_duration_aliases(
        base_alias: str,
        landscape_target: str,
        portrait_target: str,
        *,
        fl_suffix: bool = False,
    ):
        if fl_suffix:
            add_alias(f"{base_alias}_8s_fl", landscape_target)
            add_alias(f"{base_alias}_portrait_8s_fl", portrait_target)
            add_alias(f"{base_alias}_landscape_8s_fl", landscape_target)
            return

        add_alias(f"{base_alias}_8s", landscape_target)
        add_alias(f"{base_alias}_portrait_8s", portrait_target)
        add_alias(f"{base_alias}_landscape_8s", landscape_target)

    def add_default_duration_upsample_aliases(
        base_alias: str,
        resolution_name: str,
        landscape_target: str,
        portrait_target: str,
    ):
        add_alias(f"{base_alias}_8s_{resolution_name}", landscape_target)
        add_alias(f"{base_alias}_portrait_8s_{resolution_name}", portrait_target)
        add_alias(f"{base_alias}_landscape_8s_{resolution_name}", landscape_target)

    # Non-fast/non-lite Veo 3.1 aliases must call Quality upstream keys.
    MODEL_CONFIG["veo_3_1_t2v_landscape"].update({"model_key": "veo_3_1_t2v"})
    MODEL_CONFIG["veo_3_1_t2v_portrait"].update({"model_key": "veo_3_1_t2v_portrait"})
    MODEL_CONFIG["veo_3_1_i2v_s_landscape"].update({"model_key": "veo_3_1_i2v_s_fl"})
    MODEL_CONFIG["veo_3_1_i2v_s_portrait"].update({"model_key": "veo_3_1_i2v_s_portrait_fl"})
    MODEL_CONFIG["veo_3_1_extend"].update({"model_key": "veo_3_1_extend_landscape"})
    MODEL_CONFIG["veo_3_1_extend_portrait"].update({"model_key": "veo_3_1_extend_portrait"})

    for seconds in (4, 6):
        suffix = f"{seconds}s"

        # T2V duration variants.
        MODEL_CONFIG[f"veo_3_1_t2v_fast_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_fast_{suffix}", landscape
        )
        MODEL_CONFIG[f"veo_3_1_t2v_fast_portrait_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_fast_{suffix}", portrait
        )
        MODEL_CONFIG[f"veo_3_1_t2v_lite_{suffix}_landscape"] = _make_t2v_config(
            f"veo_3_1_t2v_lite_{suffix}",
            landscape,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_t2v_lite_{suffix}_portrait"] = _make_t2v_config(
            f"veo_3_1_t2v_lite_{suffix}",
            portrait,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_t2v_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_quality_{suffix}", landscape
        )
        MODEL_CONFIG[f"veo_3_1_t2v_portrait_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_quality_{suffix}", portrait
        )

        # I2V duration variants. FL keys are used for 2 images; the single-image path strips "_fl".
        MODEL_CONFIG[f"veo_3_1_i2v_s_fast_{suffix}_fl"] = _make_i2v_config(
            f"veo_3_1_i2v_s_fast_{suffix}_fl", landscape
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_fast_portrait_{suffix}_fl"] = _make_i2v_config(
            f"veo_3_1_i2v_s_fast_{suffix}_fl", portrait
        )
        MODEL_CONFIG[f"veo_3_1_i2v_lite_{suffix}_landscape"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}",
            landscape,
            min_images=1,
            max_images=1,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_i2v_lite_{suffix}_portrait"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}",
            portrait,
            min_images=1,
            max_images=1,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_interpolation_lite_{suffix}_landscape"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}_fl",
            landscape,
            min_images=2,
            max_images=2,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_interpolation_lite_{suffix}_portrait"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}_fl",
            portrait,
            min_images=2,
            max_images=2,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_{suffix}"] = _make_i2v_config(
            f"veo_3_1_i2v_s_quality_{suffix}_fl", landscape
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{suffix}"] = _make_i2v_config(
            f"veo_3_1_i2v_s_quality_{suffix}_fl", portrait
        )

        for resolution_name, resolution, upsampler_model_key in (
            ("4k", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
            ("1080p", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
        ):
            upsample = {"resolution": resolution, "model_key": upsampler_model_key}
            MODEL_CONFIG[f"veo_3_1_t2v_{suffix}_{resolution_name}"] = _make_t2v_config(
                f"veo_3_1_t2v_quality_{suffix}", landscape, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_t2v_portrait_{suffix}_{resolution_name}"] = _make_t2v_config(
                f"veo_3_1_t2v_quality_{suffix}", portrait, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_i2v_s_{suffix}_{resolution_name}"] = _make_i2v_config(
                f"veo_3_1_i2v_s_quality_{suffix}_fl", landscape, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{suffix}_{resolution_name}"] = _make_i2v_config(
                f"veo_3_1_i2v_s_quality_{suffix}_fl", portrait, upsample=upsample
            )

    for resolution_name, resolution, upsampler_model_key in (
        ("4k", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
        ("1080p", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
    ):
        upsample = {"resolution": resolution, "model_key": upsampler_model_key}
        MODEL_CONFIG[f"veo_3_1_t2v_{resolution_name}"] = _make_t2v_config(
            "veo_3_1_t2v", landscape, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_t2v_portrait_{resolution_name}"] = _make_t2v_config(
            "veo_3_1_t2v_portrait", portrait, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_{resolution_name}"] = _make_i2v_config(
            "veo_3_1_i2v_s_fl", landscape, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{resolution_name}"] = _make_i2v_config(
            "veo_3_1_i2v_s_portrait_fl", portrait, upsample=upsample
        )
    
    for seconds in (4, 6):
        suffix = f"{seconds}s"
        
        # Explicit landscape names for /v1/models; short landscape names remain compatible.
        add_alias(f"veo_3_1_t2v_fast_landscape_{suffix}", f"veo_3_1_t2v_fast_{suffix}")
        add_alias(f"veo_3_1_t2v_landscape_{suffix}", f"veo_3_1_t2v_{suffix}")
        add_alias(f"veo_3_1_i2v_s_fast_landscape_{suffix}_fl", f"veo_3_1_i2v_s_fast_{suffix}_fl")
        add_alias(f"veo_3_1_i2v_s_landscape_{suffix}", f"veo_3_1_i2v_s_{suffix}")
        
        add_alias(f"veo_3_1_t2v_lite_landscape_{suffix}", f"veo_3_1_t2v_lite_{suffix}_landscape")
        add_alias(f"veo_3_1_t2v_lite_portrait_{suffix}", f"veo_3_1_t2v_lite_{suffix}_portrait")
        add_alias(f"veo_3_1_i2v_lite_landscape_{suffix}", f"veo_3_1_i2v_lite_{suffix}_landscape")
        add_alias(f"veo_3_1_i2v_lite_portrait_{suffix}", f"veo_3_1_i2v_lite_{suffix}_portrait")
        add_alias(
            f"veo_3_1_interpolation_lite_landscape_{suffix}",
            f"veo_3_1_interpolation_lite_{suffix}_landscape",
        )
        add_alias(
            f"veo_3_1_interpolation_lite_portrait_{suffix}",
            f"veo_3_1_interpolation_lite_{suffix}_portrait",
        )
        
        for resolution_name in ("4k", "1080p"):
            add_alias(
                f"veo_3_1_t2v_landscape_{suffix}_{resolution_name}",
                f"veo_3_1_t2v_{suffix}_{resolution_name}",
            )
            add_alias(
                f"veo_3_1_i2v_s_landscape_{suffix}_{resolution_name}",
                f"veo_3_1_i2v_s_{suffix}_{resolution_name}",
            )
    
    for resolution_name in ("4k", "1080p"):
        add_alias(f"veo_3_1_t2v_landscape_{resolution_name}", f"veo_3_1_t2v_{resolution_name}")
        add_alias(f"veo_3_1_i2v_s_landscape_{resolution_name}", f"veo_3_1_i2v_s_{resolution_name}")
    
    add_alias("veo_3_1_r2v_fast_landscape", "veo_3_1_r2v_fast")
    add_alias("veo_3_1_r2v_fast_landscape_ultra", "veo_3_1_r2v_fast_ultra")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_relaxed", "veo_3_1_r2v_fast_ultra_relaxed")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_4k", "veo_3_1_r2v_fast_ultra_4k")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_1080p", "veo_3_1_r2v_fast_ultra_1080p")

    add_default_duration_aliases(
        "veo_3_1_t2v_fast",
        "veo_3_1_t2v_fast_landscape",
        "veo_3_1_t2v_fast_portrait",
    )
    add_default_duration_aliases(
        "veo_3_1_t2v",
        "veo_3_1_t2v_landscape",
        "veo_3_1_t2v_portrait",
    )
    add_default_duration_aliases(
        "veo_3_1_i2v_s_fast",
        "veo_3_1_i2v_s_fast_fl",
        "veo_3_1_i2v_s_fast_portrait_fl",
        fl_suffix=True,
    )
    add_default_duration_aliases(
        "veo_3_1_i2v_s",
        "veo_3_1_i2v_s_landscape",
        "veo_3_1_i2v_s_portrait",
    )
    add_default_duration_aliases(
        "veo_3_1_r2v_fast",
        "veo_3_1_r2v_fast",
        "veo_3_1_r2v_fast_portrait",
    )
    add_alias("veo_3_1_r2v_fast_ultra_8s", "veo_3_1_r2v_fast_ultra")
    add_alias("veo_3_1_r2v_fast_portrait_ultra_8s", "veo_3_1_r2v_fast_portrait_ultra")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_8s", "veo_3_1_r2v_fast_ultra")
    add_alias(
        "veo_3_1_r2v_fast_ultra_relaxed_8s",
        "veo_3_1_r2v_fast_ultra_relaxed",
    )
    add_alias(
        "veo_3_1_r2v_fast_portrait_ultra_relaxed_8s",
        "veo_3_1_r2v_fast_portrait_ultra_relaxed",
    )
    add_alias(
        "veo_3_1_r2v_fast_landscape_ultra_relaxed_8s",
        "veo_3_1_r2v_fast_ultra_relaxed",
    )

    add_alias("veo_3_1_t2v_lite_8s_landscape", "veo_3_1_t2v_lite_landscape")
    add_alias("veo_3_1_t2v_lite_8s_portrait", "veo_3_1_t2v_lite_portrait")
    add_alias("veo_3_1_t2v_lite_landscape_8s", "veo_3_1_t2v_lite_landscape")
    add_alias("veo_3_1_t2v_lite_portrait_8s", "veo_3_1_t2v_lite_portrait")
    add_alias("veo_3_1_i2v_lite_8s_landscape", "veo_3_1_i2v_lite_landscape")
    add_alias("veo_3_1_i2v_lite_8s_portrait", "veo_3_1_i2v_lite_portrait")
    add_alias("veo_3_1_i2v_lite_landscape_8s", "veo_3_1_i2v_lite_landscape")
    add_alias("veo_3_1_i2v_lite_portrait_8s", "veo_3_1_i2v_lite_portrait")
    add_alias(
        "veo_3_1_interpolation_lite_8s_landscape",
        "veo_3_1_interpolation_lite_landscape",
    )
    add_alias(
        "veo_3_1_interpolation_lite_8s_portrait",
        "veo_3_1_interpolation_lite_portrait",
    )
    add_alias(
        "veo_3_1_interpolation_lite_landscape_8s",
        "veo_3_1_interpolation_lite_landscape",
    )
    add_alias(
        "veo_3_1_interpolation_lite_portrait_8s",
        "veo_3_1_interpolation_lite_portrait",
    )

    for resolution_name in ("4k", "1080p"):
        add_default_duration_upsample_aliases(
            "veo_3_1_t2v",
            resolution_name,
            f"veo_3_1_t2v_{resolution_name}",
            f"veo_3_1_t2v_portrait_{resolution_name}",
        )
        add_default_duration_upsample_aliases(
            "veo_3_1_i2v_s",
            resolution_name,
            f"veo_3_1_i2v_s_{resolution_name}",
            f"veo_3_1_i2v_s_portrait_{resolution_name}",
        )


_apply_veo_3_1_model_updates()


def _known_video_model_keys() -> set[str]:
    return {
        cfg["model_key"]
        for cfg in MODEL_CONFIG.values()
        if cfg.get("type") == "video" and cfg.get("model_key")
    }


def _resolve_tier_two_model_key(model_key: str) -> str:
    """Only upgrade to an ultra key when that exact upstream key is known valid."""
    if "ultra" in model_key:
        return model_key
    if "_fl" in model_key:
        candidate = model_key.replace("_fl", "_ultra_fl")
    else:
        candidate = model_key + "_ultra"
    return candidate if candidate in _known_video_model_keys() else model_key


def _needs_video_url_resolve(source_video_url: Optional[str], video_media_id: Optional[str]) -> bool:
    if not video_media_id:
        return False
    if not source_video_url:
        return True
    if "getMediaUrlRedirect" in source_video_url:
        return True
    if "flow-content.google" in source_video_url:
        return False
    return False


class GenerationHandler:
    """统一生成处理器"""

    def __init__(
        self,
        flow_client,
        token_manager,
        load_balancer,
        db,
        concurrency_manager,
        proxy_manager,
        *,
        cache_repository=None,
        request_log_repository=None,
    ):
        cache_dir = get_runtime_tmp_dir()
        self.flow_client = flow_client
        self.token_manager = token_manager
        self.load_balancer = load_balancer
        self.db = db
        self.request_logs = request_log_repository
        self.concurrency_manager = concurrency_manager
        self.file_cache = FileCache(
            cache_dir=str(cache_dir),
            default_timeout=config.cache_timeout,
            proxy_manager=proxy_manager,
            flow_client=flow_client,
            db=db,
            cache_repository=cache_repository,
        )
        self.extension_generation_service = ExtensionGenerationService()
        self.image_pipeline = ImageGenerationPipeline(self)
        self.video_pipeline = VideoGenerationPipeline(self)

    def _create_generation_result(self) -> Dict[str, Any]:
        """????????????????"""
        return create_generation_result()

    def _create_response_state(self) -> Dict[str, Any]:
        """为单次请求创建独立的响应状态，避免并发请求互相污染。"""
        return create_response_state()

    def _mark_generation_failed(
        self,
        generation_result: Optional[Dict[str, Any]],
        error_message: str,
        *,
        status_code: int = 500,
        error_extra: Optional[Dict[str, Any]] = None,
    ):
        """????????????????????"""
        mark_generation_failed(
            generation_result,
            error_message,
            status_code=status_code,
            error_extra=error_extra,
        )

    def _mark_generation_succeeded(self, generation_result: Optional[Dict[str, Any]]):
        """???????"""
        mark_generation_succeeded(generation_result)

    async def _resolve_video_asset(
        self,
        token: Token,
        operation: Dict[str, Any],
        *,
        api_key_id: Optional[int] = None,
        project_id: Optional[str] = None,
        response_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        operation_body = operation.get("operation") if isinstance(operation.get("operation"), dict) else {}
        metadata = operation_body.get("metadata") if isinstance(operation_body.get("metadata"), dict) else {}
        video_info = metadata.get("video") if isinstance(metadata.get("video"), dict) else {}
        media_name = (
            operation.get("mediaName")
            or video_info.get("mediaName")
            or video_info.get("mediaGenerationId")
            or operation.get("name")
            or operation_body.get("name")
        )
        video_url = str(
            video_info.get("fifeUrl")
            or video_info.get("servingBaseUri")
            or operation.get("videoUrl")
            or ""
        )
        media_fetch_error = None
        video_is_cached = False
        if media_name and getattr(token, "st", None):
            try:
                video_url = await self.flow_client.get_media_url_redirect(token.st, str(media_name)) or video_url
            except Exception as exc:
                debug_logger.log_warning(f"[VIDEO] media redirect resolution failed: {type(exc).__name__}")
        if not video_url and media_name and getattr(token, "at", None):
            try:
                media_result = await self.flow_client.get_media(token.at, str(media_name))
                media_video = media_result.get("video") if isinstance(media_result, dict) else None
                encoded_video = (
                    media_video.get("encodedVideo")
                    if isinstance(media_video, dict)
                    else None
                ) or (media_result.get("encodedVideo") if isinstance(media_result, dict) else None)
                if encoded_video:
                    cached_filename = await self.file_cache.cache_base64_video(
                        str(encoded_video),
                        api_key_id=api_key_id,
                        token_id=getattr(token, "id", None),
                        flow_project_id=project_id,
                        source_media_name=str(media_name),
                    )
                    video_url = self._build_cache_url(
                        cached_filename,
                        response_state,
                        flow_project_id=project_id,
                    )
                    video_info["fifeUrl"] = video_url
                    video_is_cached = True
                    debug_logger.log_info(
                        f"[VIDEO] fetched encoded video through get_media and cached: {cached_filename}"
                    )
                else:
                    media_fetch_error = "get_media returned empty encodedVideo"
                    debug_logger.log_warning(f"[VIDEO] {media_fetch_error}")
            except Exception as exc:
                media_fetch_error = self._normalize_error_message(exc, max_length=240)
                debug_logger.log_warning(f"[VIDEO] get_media fallback failed: {media_fetch_error}")
        uuid_match = re.search(r"/video/([0-9a-f-]{36})", video_url or "")
        return {
            "media_name": media_name,
            "video_url": video_url,
            "video_media_id": uuid_match.group(1) if uuid_match else str(media_name or video_info.get("mediaGenerationId") or ""),
            "aspect_ratio": video_info.get("aspectRatio", "VIDEO_ASPECT_RATIO_LANDSCAPE"),
            "model": video_info.get("model"),
            "video_info": video_info,
            "metadata": metadata,
            "video_is_cached": video_is_cached,
            "media_fetch_error": media_fetch_error,
        }

    def _normalize_error_message(self, error_message: Any, max_length: int = 1000) -> str:
        """归一化错误文本，避免写入超长内容。"""
        text = str(error_message or "").strip() or "未知错误"
        if len(text) <= max_length:
            return text
        return f"{text[:max_length - 3]}..."

    def _infer_failed_upscale_captcha_status(self, error_message: str) -> str:
        text = str(error_message or "")
        lowered = text.lower()
        if "failed to obtain recaptcha token" in lowered:
            return "token_failed"
        if classify_recaptcha_upstream_failure(400, text) == "upstream_rejected":
            return "upstream_rejected"
        if "recaptcha" in lowered or "captcha" in lowered or "public_error" in lowered:
            return "unknown"
        return "idle"

    async def _fail_image_upscale_generation(
        self,
        *,
        generation_result: Optional[Dict[str, Any]],
        poll_task_id: Optional[str],
        error_message: str,
    ) -> str:
        clean_error = self._normalize_error_message(error_message)
        error_extra = {
            "upscale_status": "failed",
            "upscale_error_message": clean_error,
        }
        self._mark_generation_failed(
            generation_result,
            clean_error,
            status_code=502,
            error_extra=error_extra,
        )
        await self._maybe_update_poll_task(
            poll_task_id,
            status="failed",
            result_urls=[],
            base_result_urls=[],
            delivery_urls=[],
            upscale_status="failed",
            upscale_error_message=clean_error,
            error_message=clean_error,
            completed_at=datetime.utcnow(),
            job_phase="failed",
            captcha_status=self._infer_failed_upscale_captcha_status(clean_error),
        )
        return clean_error

    def _is_extension_generation_enabled(self) -> bool:
        return bool(config.extension_generation_enabled)

    def _should_fallback_to_local_http(self, error: Exception) -> bool:
        if str(config.extension_generation_fallback_mode).strip().lower() != "local_http_on_recaptcha":
            return False
        message = str(error or "")
        return classify_recaptcha_upstream_failure(400, message) == "upstream_rejected"

    def _is_backend_poll_blocked_error(self, error: Exception) -> bool:
        msg = str(error or "").lower()
        if not msg:
            return False
        markers = (
            "recaptcha",
            "unusual_activity",
            "captcha",
            "403",
            "429",
            "forbidden",
            "too many requests",
            "flow api request failed",
        )
        return any(m in msg for m in markers)

    async def _execute_with_extension_fallback(
        self,
        operation_name: str,
        extension_operation,
        local_operation,
    ):
        if not self._is_extension_generation_enabled():
            return await local_operation()
        try:
            return await extension_operation()
        except Exception as exc:
            if self._should_fallback_to_local_http(exc):
                debug_logger.log_warning(
                    f"[EXT-GEN] fallback to local HTTP on reCAPTCHA rejection: op={operation_name}, err={exc}"
                )
                try:
                    if hasattr(self.flow_client, "set_force_local_http"):
                        self.flow_client.set_force_local_http(True)
                    return await local_operation()
                finally:
                    if hasattr(self.flow_client, "clear_force_local_http"):
                        self.flow_client.clear_force_local_http()
            raise

    async def _maybe_update_poll_task(self, poll_task_id: Optional[str], **kwargs: Any) -> None:
        """Best-effort `tasks` row updates for `GET /v1/jobs/{job_id}` when async polling is active."""
        if not poll_task_id:
            return
        updates = {k: v for k, v in kwargs.items() if v is not None}
        if not updates:
            return
        try:
            await self.db.update_task(poll_task_id, **updates)
        except Exception as exc:
            debug_logger.log_warning(f"[poll_task] update_task failed: {exc}")

    def _build_poll_task_progress_hook(
        self,
        poll_task_id: Optional[str],
        request_log_state: Optional[Dict[str, Any]] = None,
        token_id: Optional[int] = None,
    ):
        if not poll_task_id and not isinstance(request_log_state, dict):
            return None

        async def _hook(updates: Dict[str, Any]) -> None:
            if updates.get("captcha_user_agent_set") and isinstance(request_log_state, dict):
                provider = str(updates.get("captcha_provider") or "").strip() or None
                request_log_state["captcha_user_agent_set"] = True
                request_log_state["captcha_provider"] = provider
                await self._update_request_log_progress(
                    request_log_state,
                    token_id=token_id,
                    status_text="captcha_user_agent_set",
                    progress=int(request_log_state.get("progress") or 0),
                )

            if poll_task_id:
                task_updates = {
                    key: value
                    for key, value in updates.items()
                    if key not in {"captcha_user_agent_set", "captcha_provider"}
                }
                await self._maybe_update_poll_task(poll_task_id, **task_updates)

        return _hook

    def _resolve_video_model_key_for_tier(self, model_config: Dict[str, Any], user_tier: str) -> tuple[str, Optional[str]]:
        """根据账号层级调整视频模型 key。"""
        model_key = model_config["model_key"]
        allow_tier_upgrade = bool(model_config.get("allow_tier_upgrade", True))

        if user_tier == "PAYGATE_TIER_TWO":
            if allow_tier_upgrade and "ultra" not in model_key:
                upgraded_model_key = _resolve_tier_two_model_key(model_key)
                if upgraded_model_key != model_key:
                    return upgraded_model_key, f"TIER_TWO 账号自动切换到 ultra 模型: {upgraded_model_key}"
            return model_key, None

        if user_tier == "PAYGATE_TIER_ONE" and "ultra" in model_key:
            # veo_3_1_extend_fast_portrait_ultra -> veo_3_1_extend_fast_portrait
            model_key = model_key.replace("_ultra_fl", "_fl").replace("_ultra", "")
            return model_key, f"TIER_ONE 账号自动切换到标准模型: {model_key}"

        return model_key, None

    async def _fail_video_task(self, operations: Optional[List[Dict[str, Any]]], error_message: str):
        """将视频任务收口到失败态，避免残留 processing。"""
        if not operations:
            return

        operation = operations[0] if operations else {}
        task_id = (operation.get("operation") or {}).get("name")
        if not task_id:
            return

        try:
            await self.db.update_task(
                task_id,
                status="failed",
                error_message=self._normalize_error_message(error_message),
                completed_at=time.time()
            )
        except Exception as exc:
            debug_logger.log_error(f"[VIDEO] 更新任务失败状态失败: {exc}")

    async def check_token_availability(self, is_image: bool, is_video: bool) -> bool:
        """检查Token可用性

        Args:
            is_image: 是否检查图片生成Token
            is_video: 是否检查视频生成Token

        Returns:
            True表示有可用Token, False表示无可用Token
        """
        token_obj = await self.load_balancer.select_token(
            for_image_generation=is_image,
            for_video_generation=is_video
        )
        return token_obj is not None

    async def handle_generation(
        self,
        model: str,
        prompt: str,
        images: Optional[List[bytes]] = None,
        stream: bool = False,
        base_url_override: Optional[str] = None,
        allowed_token_ids: Optional[set[int]] = None,
        selection_context: Optional[Dict[str, Any]] = None,
        api_key_id: Optional[int] = None,
        requested_project_id: Optional[str] = None,
        video_media_id: Optional[str] = None,
        poll_task_id: Optional[str] = None,
    ) -> AsyncGenerator:
        """统一生成入口

        Args:
            model: 模型名称
            prompt: 提示词
            images: 图片列表 (bytes格式)
            stream: 是否流式输出
            poll_task_id: When set (async OpenAI job id), persist captcha/upscale phase to `tasks` for polling.
        """
        start_time = time.time()
        token = None
        generation_type = None
        pending_token_state = {"active": False}
        request_id = (poll_task_id or "").strip() or (
            f"gen-{int(start_time * 1000)}-{id(asyncio.current_task())}"
        )
        perf_trace: Dict[str, Any] = {
            "request_id": request_id,
            "model": model,
            "status": "processing",
        }
        generation_result = self._create_generation_result()
        response_state = self._create_response_state()
        response_state["base_url"] = (base_url_override or "").strip().rstrip("/") or None
        request_log_state: Dict[str, Any] = {
            "id": None,
            "progress": 0,
            "api_key_id": api_key_id,
            "request_id": request_id,
            "captcha_user_agent_set": False,
            "captcha_provider": None,
        }

        # 防止并发链路复用到上一次请求的指纹上下文
        if hasattr(self.flow_client, "clear_request_fingerprint"):
            self.flow_client.clear_request_fingerprint()
        if hasattr(self.flow_client, "set_managed_api_key_id"):
            self.flow_client.set_managed_api_key_id(api_key_id)

        # 1. 验证模型
        if model not in MODEL_CONFIG:
            error_msg = f"不支持的模型: {model}"
            debug_logger.log_error(error_msg)
            record_generation_result("unknown", "invalid", time.time() - start_time)
            yield self._create_error_response(error_msg, status_code=400)
            return

        model_config = MODEL_CONFIG[model]
        generation_type = model_config["type"]
        request_operation = f"generate_{generation_type}"
        prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
        request_payload = {
            "request_id": request_id,
            "model": model,
            "prompt": prompt_for_log,
            "has_images": images is not None and len(images) > 0,
        }
        if requested_project_id:
            request_payload["project_id"] = requested_project_id
        debug_logger.log_info(f"[GENERATION] 开始生成 - 模型: {model}, 类型: {generation_type}, Prompt: {prompt[:50]}...")

        request_log_state["id"] = await self._log_request(
            token_id=None,
            api_key_id=api_key_id,
            operation=request_operation,
            request_data=request_payload,
            response_data={
                "status": "processing",
                "status_text": "started",
                "progress": 0,
                "request_id": request_id,
            },
            status_code=102,
            duration=0,
            status_text="started",
            progress=0,
            request_log_state=request_log_state,
        )

        async def _finalize_early_failure(error_message: str, status_code: int) -> None:
            duration = time.time() - start_time
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_message
            selected_token_id = (
                token.id if token else request_log_state.get("token_id")
            )
            await self._log_request(
                token_id=selected_token_id,
                api_key_id=api_key_id,
                operation=request_operation,
                request_data=request_payload,
                response_data={
                    "error": error_message,
                    "request_id": request_id,
                    "performance": perf_trace,
                },
                status_code=status_code,
                duration=duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
                request_log_state=request_log_state,
            )

        # 向用户展示开始信息
        if stream:
            yield self._create_stream_chunk(
                f"✨ {'视频' if generation_type == 'video' else '图片'}生成任务已启动\n",
                role="assistant"
            )

        # 2. 选择Token
        debug_logger.log_info(f"[GENERATION] 正在选择可用Token...")
        token_select_started_at = time.time()
        token_selection_diagnostics: Dict[str, Any] = {}
        allowlist_filter_reason_type = None
        if isinstance(selection_context, dict):
            allowlist_filter_reason_type = selection_context.get("allowlist_filter_reason_type")

        try:
            if generation_type == "image":
                token = await self.load_balancer.select_token(
                    for_image_generation=True,
                    model=model,
                    reserve=False,
                    enforce_concurrency_filter=False,
                    track_pending=True,
                    allowed_token_ids=allowed_token_ids,
                    managed_api_key_id=api_key_id,
                    allowlist_filter_reason_type=allowlist_filter_reason_type,
                    diagnostics_sink=token_selection_diagnostics,
                )
            else:
                token = await self.load_balancer.select_token(
                    for_video_generation=True,
                    model=model,
                    reserve=False,
                    enforce_concurrency_filter=False,
                    track_pending=True,
                    allowed_token_ids=allowed_token_ids,
                    managed_api_key_id=api_key_id,
                    allowlist_filter_reason_type=allowlist_filter_reason_type,
                    diagnostics_sink=token_selection_diagnostics,
                )
        except asyncio.CancelledError:
            error_msg = "生成已取消: 客户端连接已断开"
            record_generation_result(generation_type, "cancelled", time.time() - start_time)
            await _finalize_early_failure(error_msg, 499)
            if hasattr(self.flow_client, "clear_managed_api_key_id"):
                self.flow_client.clear_managed_api_key_id()
            raise
        except Exception as e:
            error_msg = f"生成失败: {str(e)}"
            debug_logger.log_error(f"[GENERATION] token selection failed: {error_msg}")
            record_generation_result(generation_type, "failed", time.time() - start_time)
            await _finalize_early_failure(error_msg, 500)
            if hasattr(self.flow_client, "clear_managed_api_key_id"):
                self.flow_client.clear_managed_api_key_id()
            if stream:
                yield self._create_stream_chunk(f"❌ {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=500)
            return
        perf_trace["token_select_ms"] = int((time.time() - token_select_started_at) * 1000)
        if token_selection_diagnostics:
            perf_trace["token_selection_diagnostics"] = token_selection_diagnostics
            debug_logger.log_info(
                "[GENERATION] token selection diagnostics: "
                f"allowlist_type={token_selection_diagnostics.get('allowlist_filter_reason_type')}, "
                f"effective_allowed={token_selection_diagnostics.get('effective_allowed_token_ids')}, "
                f"filtered_summary={token_selection_diagnostics.get('filtered_reason_summary')}, "
                f"post_check_summary={token_selection_diagnostics.get('post_check_failure_summary')}"
            )
        if isinstance(selection_context, dict) and selection_context:
            perf_trace["selection_context"] = dict(selection_context)
            debug_logger.log_info(
                "[GENERATION] selection context: "
                f"project_id={selection_context.get('selected_project_id')}, "
                f"key_allowed={selection_context.get('key_allowed_account_ids')}, "
                f"effective_allowed={selection_context.get('effective_allowed_token_ids')}, "
                f"type={selection_context.get('allowlist_filter_reason_type')}"
            )

        if not token:
            error_msg = None
            detailed_no_token_reason = None
            if self.load_balancer and hasattr(self.load_balancer, "build_diagnostics_reason"):
                detailed_no_token_reason = self.load_balancer.build_diagnostics_reason(token_selection_diagnostics)
            if detailed_no_token_reason:
                error_msg = detailed_no_token_reason
            if self.load_balancer and hasattr(self.load_balancer, "get_unavailable_reason"):
                unavailable_reason = await self.load_balancer.get_unavailable_reason(
                    for_image_generation=(generation_type == "image"),
                    for_video_generation=(generation_type == "video"),
                    model=model,
                )
                if unavailable_reason:
                    if error_msg:
                        error_msg = f"{error_msg}；{unavailable_reason}"
                    elif False:
                        error_msg = unavailable_reason
            if not error_msg:
                error_msg = self._get_no_token_error_message(generation_type)
            debug_logger.log_error(
                "[GENERATION] no-token failure summary: "
                f"reason={error_msg}, "
                f"filtered_summary={token_selection_diagnostics.get('filtered_reason_summary')}, "
                f"post_check_summary={token_selection_diagnostics.get('post_check_failure_summary')}"
            )
            debug_logger.log_error(f"[GENERATION] {error_msg}")
            record_generation_result(generation_type, "no_token", time.time() - start_time)
            await self._log_request(
                token_id=None,
                api_key_id=api_key_id,
                operation=request_operation,
                request_data=request_payload,
                response_data={
                    "error": error_msg,
                    "request_id": request_id,
                    "performance": perf_trace,
                },
                status_code=503,
                duration=time.time() - start_time,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
                request_log_state=request_log_state,
            )
            if stream:
                yield self._create_stream_chunk(f"❌ {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=503)
            return

        debug_logger.log_info(f"[GENERATION] 已选择Token: {token.id} ({token.email})")
        pending_token_state["active"] = True
        request_log_state["token_id"] = token.id
        await self._update_request_log_progress(
            request_log_state,
            token_id=token.id,
            status_text="token_selected",
            progress=8,
            response_extra={"token_email": token.email},
        )

        try:
            # 3. 确保AT有效
            debug_logger.log_info(f"[GENERATION] 检查Token AT有效性...")
            if stream:
                yield self._create_stream_chunk("初始化生成环境...\n")

            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="token_ready",
                progress=15,
            )
            ensure_at_started_at = time.time()
            token = await self.token_manager.ensure_valid_token(token)
            perf_trace["ensure_at_ms"] = int((time.time() - ensure_at_started_at) * 1000)
            if not token:
                error_msg = "Token AT无效或刷新失败"
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                record_generation_result(generation_type, "failed", time.time() - start_time)
                await _finalize_early_failure(error_msg, 503)
                if stream:
                    yield self._create_stream_chunk(f"❌ {error_msg}\n")
                yield self._create_error_response(error_msg, status_code=503)
                return

            if getattr(token, "auth_mode", "session_token") == "browser_profile":
                profile_status = getattr(token, "browser_profile_status", None) or "unknown"
                login_state = getattr(token, "browser_profile_login_state", None) or "unknown"
                if profile_status != "connected" or login_state != "logged_in":
                    error_msg = (
                        "Browser profile is not logged in. Open the profile in admin, "
                        "log in inside Fluxbox, then click Sync."
                    )
                    debug_logger.log_error(f"[GENERATION] {error_msg} token_id={token.id}")
                    record_generation_result(generation_type, "failed", time.time() - start_time)
                    await _finalize_early_failure(error_msg, 409)
                    if stream:
                        yield self._create_stream_chunk(f"❌ {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=409)
                    return
                debug_logger.log_info(
                    f"[GENERATION] Browser-profile token {token.id} is connected; using synced ST/AT for Flow request pipeline"
                )

            # 4. 确保Project存在
            debug_logger.log_info(f"[GENERATION] 检查/创建Project...")

            if not supports_model_for_tier(model, token.user_paygate_tier):
                required_tier = get_required_paygate_tier_for_model(model)
                error_msg = "当前模型需要 " + get_paygate_tier_label(required_tier) + " 账号: " + model
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                record_generation_result(generation_type, "failed", time.time() - start_time)
                await _finalize_early_failure(error_msg, 403)
                if stream:
                    yield self._create_stream_chunk(f"❌ {error_msg}\n")
                yield self._create_error_response(error_msg, status_code=403)
                return

            ensure_project_started_at = time.time()
            try:
                project_id = await self.token_manager.ensure_project_exists(
                    token.id,
                    api_key_id=api_key_id,
                    preferred_project_id=requested_project_id,
                )
            except ValueError as e:
                err = str(e)
                debug_logger.log_error(f"[GENERATION] {err}")
                record_generation_result(generation_type, "failed", time.time() - start_time)
                await _finalize_early_failure(err, 400)
                if stream:
                    yield self._create_stream_chunk(f"❌ {err}\n")
                yield self._create_error_response(err, status_code=400)
                return
            perf_trace["ensure_project_ms"] = int((time.time() - ensure_project_started_at) * 1000)
            debug_logger.log_info(f"[GENERATION] Project ID: {project_id}")
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="project_ready",
                progress=22,
                response_extra={"project_id": project_id},
            )
            await self._maybe_update_poll_task(
                poll_task_id,
                job_phase="generation_captcha",
                captcha_status="pending",
            )
            prefill_action = "IMAGE_GENERATION" if generation_type == "image" else "VIDEO_GENERATION"
            await self.flow_client.prefill_remote_browser_pool(
                project_id=project_id,
                action=prefill_action,
                token_id=token.id,
            )

            # 5. 根据类型处理
            generation_pipeline_started_at = time.time()
            if generation_type == "image":
                debug_logger.log_info(f"[GENERATION] 开始图片生成流程...")
                async for chunk in self.image_pipeline.run(
                    token, project_id, model_config, prompt, images, stream,
                    api_key_id=api_key_id,
                    perf_trace=perf_trace,
                    generation_result=generation_result,
                    response_state=response_state,
                    request_log_state=request_log_state,
                    pending_token_state=pending_token_state,
                    poll_task_id=poll_task_id,
                ):
                    yield chunk
            else:  # video
                debug_logger.log_info(f"[GENERATION] 开始视频生成流程...")
                async for chunk in self.video_pipeline.run(
                    token, project_id, model_config, prompt, images, stream,
                    api_key_id=api_key_id,
                    perf_trace=perf_trace,
                    generation_result=generation_result,
                    response_state=response_state,
                    request_log_state=request_log_state,
                    pending_token_state=pending_token_state,
                    video_media_id=video_media_id,
                    poll_task_id=poll_task_id,
                ):
                    yield chunk
            perf_trace["generation_pipeline_ms"] = int((time.time() - generation_pipeline_started_at) * 1000)

            # 6. 记录使用
            if not generation_result.get("success"):
                error_status_code = int(generation_result.get("error_status_code") or 500)
                error_extra = generation_result.get("error_extra")
                if not isinstance(error_extra, dict):
                    error_extra = {}
                error_msg = generation_result.get("error_message") or "生成未成功完成"
                debug_logger.log_warning(f"[GENERATION] 生成未成功，不扣次数: {error_msg}")
                if token:
                    error = Exception(error_msg)
                    if self._should_count_token_error(error):
                        await self.token_manager.record_error(token.id)
                    else:
                        debug_logger.log_info(
                            f"[GENERATION] 跳过 token 错误计数: token_id={token.id}, reason={error_msg[:200]}"
                        )
                duration = time.time() - start_time
                record_generation_result(generation_type, "failed", duration)
                perf_trace["status"] = "failed"
                perf_trace["total_ms"] = int(duration * 1000)
                perf_trace["error"] = error_msg
                prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
                response_data = {
                    "error": error_msg,
                    "request_id": request_id,
                    "performance": perf_trace,
                }
                response_data.update(error_extra)
                await self._log_request(
                    token.id if token else None,
                    api_key_id,
                    request_operation,
                    request_payload,
                    response_data,
                    error_status_code,
                    duration,
                    log_id=request_log_state.get("id"),
                    status_text="failed",
                    progress=request_log_state.get("progress", 0),
                    request_log_state=request_log_state,
                )
                if not generation_result.get("error_emitted"):
                    if stream:
                        yield self._create_stream_chunk(f"❌ {error_msg}\n")
                    yield self._create_error_response(
                        error_msg,
                        status_code=error_status_code,
                        extra_fields=error_extra,
                    )
                return

            is_video = (generation_type == "video")
            await self.token_manager.record_usage(token.id, is_video=is_video)

            # 重置错误计数 (请求成功时清空连续错误计数)
            await self.token_manager.record_success(token.id)

            debug_logger.log_info(f"[GENERATION] ✅ 生成成功完成")

            # 7. 记录成功日志
            duration = time.time() - start_time
            record_generation_result(generation_type, "success", duration)
            perf_trace["status"] = "success"
            perf_trace["total_ms"] = int(duration * 1000)
            # 日志中保留更完整的 prompt，避免管理页只看到过短内容
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"

            # 构建响应数据，包含生成的URL
            response_data = {
                "status": "success",
                "request_id": request_id,
                "model": model,
                "prompt": prompt_for_log,
                "performance": perf_trace
            }

            # 添加生成的URL（如果有）
            if response_state.get("url"):
                response_data["url"] = response_state["url"]
            if response_state.get("generated_assets"):
                response_data["generated_assets"] = response_state["generated_assets"]
            image_perf = perf_trace.get("image_generation", {}) if isinstance(perf_trace, dict) else {}
            video_perf = perf_trace.get("video_generation", {}) if isinstance(perf_trace, dict) else {}
            debug_logger.log_info(
                f"[PERF] [{request_id}] total={perf_trace.get('total_ms', 0)}ms, "
                f"select={perf_trace.get('token_select_ms', 0)}ms, "
                f"ensure_at={perf_trace.get('ensure_at_ms', 0)}ms, "
                f"project={perf_trace.get('ensure_project_ms', 0)}ms, "
                f"pipeline={perf_trace.get('generation_pipeline_ms', 0)}ms, "
                f"slot_wait={image_perf.get('slot_wait_ms', 0)}ms, "
                f"launch_queue={image_perf.get('launch_queue_wait_ms', 0)}ms, "
                f"launch_stagger={image_perf.get('launch_stagger_wait_ms', 0)}ms, "
                f"video_slot_wait={video_perf.get('slot_wait_ms', 0)}ms"
            )

            await self._log_request(
                token.id,
                api_key_id,
                request_operation,
                request_payload,
                response_data,
                200,
                duration,
                log_id=request_log_state.get("id"),
                status_text="completed",
                progress=100,
                request_log_state=request_log_state,
            )

        except asyncio.CancelledError:
            error_msg = "生成已取消: 客户端连接已断开"
            debug_logger.log_warning(f"[GENERATION] ⚠️ {error_msg}")
            duration = time.time() - start_time
            record_generation_result(generation_type or "unknown", "cancelled", duration)
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_msg
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
            await self._log_request(
                token.id if token else None,
                api_key_id,
                request_operation if generation_type else "generate_unknown",
                request_payload if 'request_payload' in locals() else {"model": model},
                {
                    "error": error_msg,
                    "request_id": request_id,
                    "performance": perf_trace,
                },
                499,
                duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
                request_log_state=request_log_state,
            )
            raise
        except Exception as e:
            error_msg = f"生成失败: {str(e)}"
            debug_logger.log_error(f"[GENERATION] ❌ {error_msg}")
            if token:
                if self._should_count_token_error(e):
                    await self.token_manager.record_error(token.id)
                else:
                    debug_logger.log_info(
                        f"[GENERATION] 跳过 token 错误计数: token_id={token.id}, reason={str(e)[:200]}"
                    )

            # 先将最终失败状态落库，再返回错误响应，避免日志停在 102。
            duration = time.time() - start_time
            record_generation_result(generation_type or "unknown", "failed", duration)
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_msg
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
            await self._log_request(
                token.id if token else None,
                api_key_id,
                request_operation if generation_type else "generate_unknown",
                request_payload if 'request_payload' in locals() else {"model": model},
                {
                    "error": error_msg,
                    "request_id": request_id,
                    "performance": perf_trace,
                },
                500,
                duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
                request_log_state=request_log_state,
            )
            if stream:
                yield self._create_stream_chunk(f"❌ {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=500)
        finally:
            if hasattr(self.flow_client, "clear_managed_api_key_id"):
                self.flow_client.clear_managed_api_key_id()
            pending_token_id = token.id if token else request_log_state.get("token_id")
            if pending_token_state.get("active") and pending_token_id and self.load_balancer:
                await self.load_balancer.release_pending(
                    pending_token_id,
                    for_image_generation=(generation_type == "image"),
                    for_video_generation=(generation_type == "video"),
                )
                pending_token_state["active"] = False


    def _should_count_token_error(self, error: Exception) -> bool:
        """Return whether a failure should count against the account token."""
        error_text = str(error or "").strip().lower()
        if not error_text:
            return True

        non_token_fault_markers = (
            "failed to obtain recaptcha token",
            "recaptcha evaluation failed",
            "recaptcha 验证失败",
            "recaptcha 错误",
            "public_error_unusual_activity",
            "too much traffic",
            "error_no_slot_available",
            "打码服务资源不足",
            "打码服务资源阻塞",
            "yescaptcha",
            "capsolver",
            "capmonster",
            "ezcaptcha",
        )
        if any(marker in error_text for marker in non_token_fault_markers):
            return False
        if "没有可用的token进行" in error_text:
            return False
        return True

    def _get_no_token_error_message(self, generation_type: str) -> str:
        """获取无可用Token时的详细错误信息"""
        if generation_type == "image":
            return "没有可用的Token进行图片生成。所有Token都处于禁用、冷却、锁定或已过期状态。"
        else:
            return "没有可用的Token进行视频生成。所有Token都处于禁用、冷却、配额耗尽或已过期状态。"

    async def _handle_image_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool,
        api_key_id: Optional[int] = None,
        perf_trace: Optional[Dict[str, Any]] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        pending_token_state: Optional[Dict[str, bool]] = None,
        poll_task_id: Optional[str] = None,
    ) -> AsyncGenerator:
        """处理图片生成 (同步返回)"""

        if response_state is None:
            response_state = self._create_response_state()

        image_trace: Optional[Dict[str, Any]] = None
        if isinstance(perf_trace, dict):
            image_trace = perf_trace.setdefault("image_generation", {})
            image_trace["input_image_count"] = len(images) if images else 0

        # 不在本地等待图片硬并发槽位；请求一到就直接向上游提交。
        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)

        if image_trace is not None:
            image_trace["slot_wait_ms"] = 0

        if images and len(images) > 0:
            await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="uploading_images", progress=28)
        else:
            await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="submitting_image", progress=28)

        try:
            # 上传图片 (如果有)
            upload_started_at = time.time()
            image_inputs = []
            if images and len(images) > 0:
                if stream:
                    yield self._create_stream_chunk(f"上传 {len(images)} 张参考图片...\n")

                # 支持多图输入
                for idx, image_bytes in enumerate(images):
                    media_id = await self.flow_client.upload_image(
                        token.at,
                        image_bytes,
                        model_config["aspect_ratio"],
                        project_id=project_id
                    )
                    image_inputs.append({
                        "name": media_id,
                        "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"
                    })
                    if stream:
                        yield self._create_stream_chunk(f"已上传第 {idx + 1}/{len(images)} 张图片\n")
            if image_trace is not None:
                image_trace["upload_images_ms"] = int((time.time() - upload_started_at) * 1000)

            # 调用生成API
            if stream:
                if images and len(images) > 0:
                    yield self._create_stream_chunk("参考图片上传完成，正在进行打码验证...\n")
                else:
                    yield self._create_stream_chunk("正在进行打码验证并提交图片生成请求...\n")

            async def _image_progress_callback(status_text: str, progress: int):
                await self._update_request_log_progress(
                    request_log_state,
                    token_id=token.id,
                    status_text=status_text,
                    progress=progress,
                )

            poll_hook = self._build_poll_task_progress_hook(
                poll_task_id,
                request_log_state=request_log_state,
                token_id=token.id,
            )
            generate_started_at = time.time()
            async def _extension_generate_image():
                return await self.flow_client.generate_image(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_name=model_config["model_name"],
                    aspect_ratio=model_config["aspect_ratio"],
                    image_inputs=image_inputs,
                    token_id=token.id,
                    token_image_concurrency=token.image_concurrency,
                    progress_callback=_image_progress_callback,
                    poll_task_progress=poll_hook,
                )

            result, generation_session_id, upstream_trace = await self._execute_with_extension_fallback(
                "generate_image",
                _extension_generate_image,
                _extension_generate_image,
            )
            if image_trace is not None:
                image_trace["generate_api_ms"] = int((time.time() - generate_started_at) * 1000)
                image_trace["upstream_trace"] = upstream_trace
                attempts = upstream_trace.get("generation_attempts") if isinstance(upstream_trace, dict) else None
                if isinstance(attempts, list) and attempts:
                    first_attempt = attempts[0] if isinstance(attempts[0], dict) else {}
                    image_trace["launch_queue_wait_ms"] = int(first_attempt.get("launch_queue_ms") or 0)
                    image_trace["launch_stagger_wait_ms"] = int(first_attempt.get("launch_stagger_ms") or 0)
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="image_generated",
                progress=72,
            )

            # 提取URL和mediaId
            media = result.get("media", [])
            if not media:
                self._mark_generation_failed(generation_result, "\u751f\u6210\u7ed3\u679c\u4e3a\u7a7a", status_code=502)
                yield self._create_error_response("生成结果为空", status_code=502)
                return

            image_url = media[0]["image"]["generatedImage"]["fifeUrl"]
            media_id = media[0].get("name")  # 用于 upsample
            response_state["generated_assets"] = {
                "type": "image",
                "origin_image_url": image_url
            }
            upsample_resolution = model_config.get("upsample")

            if poll_task_id and not upsample_resolution:
                await self._maybe_update_poll_task(
                    poll_task_id,
                    job_phase="finalizing",
                    captcha_status="idle",
                )

            # 检查是否需要 upsample
            if upsample_resolution and not media_id:
                error_msg = await self._fail_image_upscale_generation(
                    generation_result=generation_result,
                    poll_task_id=poll_task_id,
                    error_message="Requested image upscale failed: missing media id from generation response",
                )
                if stream:
                    yield self._create_stream_chunk(f"⚠️ {error_msg}\n")
                yield self._create_error_response(
                    error_msg,
                    status_code=502,
                    extra_fields={
                        "upscale_status": "failed",
                        "upscale_error_message": error_msg,
                    },
                )
                return

            if upsample_resolution and media_id:
                await self._maybe_update_poll_task(
                    poll_task_id,
                    job_phase="upscale_captcha",
                    captcha_status="pending",
                )
                upsample_started_at = time.time()
                resolution_name = "4K" if "4K" in upsample_resolution else "2K"
                await self._update_request_log_progress(request_log_state, token_id=token.id, status_text=f"upsampling_{resolution_name.lower()}", progress=82)
                if stream:
                    yield self._create_stream_chunk(f"正在放大图片到 {resolution_name}...\n")

                # 4K/2K 图片重试逻辑 - 使用配置的最大重试次数
                max_retries = config.flow_max_retries
                upscale_failure_message = None
                for retry_attempt in range(max_retries):
                    try:
                        # 调用 upsample API
                        async def _extension_upsample_image():
                            return await self.flow_client.upsample_image(
                                at=token.at,
                                project_id=project_id,
                                media_id=media_id,
                                target_resolution=upsample_resolution,
                                user_paygate_tier=normalized_tier,
                                session_id=generation_session_id,
                                token_id=token.id,
                                poll_task_progress=poll_hook,
                            )

                        encoded_image = await self._execute_with_extension_fallback(
                            "upsample_image",
                            _extension_upsample_image,
                            _extension_upsample_image,
                        )

                        if encoded_image:
                            await self._maybe_update_poll_task(
                                poll_task_id,
                                job_phase="finalizing",
                                upscale_status="processing",
                            )
                            debug_logger.log_info(f"[UPSAMPLE] 图片已放大到 {resolution_name}")

                            if stream:
                                yield self._create_stream_chunk(f"✅ 图片已放大到 {resolution_name}\n")

                            # 2K/4K 图片统一落盘为真实文件，日志里只保留链接。
                            response_state["generated_assets"] = {
                                "type": "image",
                                "origin_image_url": image_url,
                                "upscaled_image": {
                                    "resolution": resolution_name
                                }
                            }

                            if not config.cache_enabled:
                                data_url = f"data:image/png;base64,{encoded_image}"
                                response_state["url"] = data_url
                                response_state["generated_assets"]["upscaled_image"]["url"] = data_url
                                self._mark_generation_succeeded(generation_result)
                                if image_trace is not None:
                                    image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)
                                if stream:
                                    yield self._create_stream_chunk(
                                        f"![Generated Image]({data_url})",
                                        finish_reason="stop",
                                        extra_fields={
                                            "generated_assets": response_state.get("generated_assets")
                                        },
                                    )
                                else:
                                    yield self._create_completion_response(
                                        data_url,
                                        media_type="image",
                                        extra_fields={
                                            "generated_assets": response_state.get("generated_assets")
                                        },
                                    )
                                return

                            try:
                                await self._update_request_log_progress(
                                    request_log_state,
                                    token_id=token.id,
                                    status_text="caching_image",
                                    progress=90,
                                )
                                if stream:
                                    yield self._create_stream_chunk(f"缓存 {resolution_name} 图片中...\n")
                                cached_filename = await self.file_cache.cache_base64_image(
                                    encoded_image,
                                    resolution_name,
                                    api_key_id=api_key_id,
                                    token_id=token.id,
                                    flow_project_id=project_id,
                                )
                                local_url = self._build_cache_url(
                                    cached_filename, response_state, flow_project_id=project_id
                                )
                                response_state["url"] = local_url
                                response_state["generated_assets"]["upscaled_image"]["local_url"] = local_url
                                response_state["generated_assets"]["upscaled_image"]["url"] = local_url
                                self._mark_generation_succeeded(generation_result)
                                if stream:
                                    yield self._create_stream_chunk(f"✅ {resolution_name} 图片缓存成功\n")
                                    yield self._create_stream_chunk(
                                        f"![Generated Image]({local_url})",
                                        finish_reason="stop",
                                        extra_fields={
                                            "generated_assets": response_state.get("generated_assets")
                                        },
                                    )
                                else:
                                    yield self._create_completion_response(
                                        local_url,
                                        media_type="image",
                                        extra_fields={
                                            "generated_assets": response_state.get("generated_assets")
                                        },
                                    )
                                if image_trace is not None:
                                    image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)
                                return
                            except Exception as e:
                                debug_logger.log_error(f"Failed to cache {resolution_name} image: {str(e)}")
                                error_msg = await self._fail_image_upscale_generation(
                                    generation_result=generation_result,
                                    poll_task_id=poll_task_id,
                                    error_message=(
                                        f"Requested {resolution_name} image upscale failed: "
                                        f"could not cache high-resolution image ({self._normalize_error_message(e, max_length=160)})"
                                    ),
                                )
                                if stream:
                                    yield self._create_stream_chunk(f"⚠️ {error_msg}\n")
                                if image_trace is not None:
                                    image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)
                                yield self._create_error_response(
                                    error_msg,
                                    status_code=502,
                                    extra_fields={
                                        "upscale_status": "failed",
                                        "upscale_error_message": error_msg,
                                    },
                                )
                                return
                        else:
                            debug_logger.log_warning("[UPSAMPLE] 返回结果为空")
                            upscale_failure_message = f"Requested {resolution_name} image upscale failed: upstream returned an empty image"
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ {upscale_failure_message}\n")
                            break  # 空结果不重试

                    except Exception as e:
                        error_str = str(e)
                        debug_logger.log_error(f"[UPSAMPLE] 放大失败 (尝试 {retry_attempt + 1}/{max_retries}): {error_str}")
                        
                        # 检查是否是可重试错误（403、reCAPTCHA、超时等）
                        retry_reason = self.flow_client._get_retry_reason(error_str)
                        if retry_reason and retry_attempt < max_retries - 1:
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ 放大遇到{retry_reason}，正在重试 ({retry_attempt + 2}/{max_retries})...\n")
                            # 等待一小段时间后重试
                            await asyncio.sleep(1)
                            continue
                        else:
                            upscale_failure_message = (
                                f"Requested {resolution_name} image upscale failed: "
                                f"{self._normalize_error_message(error_str, max_length=220)}"
                            )
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ {upscale_failure_message}\n")
                            break
                if image_trace is not None:
                    image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)
                error_msg = await self._fail_image_upscale_generation(
                    generation_result=generation_result,
                    poll_task_id=poll_task_id,
                    error_message=upscale_failure_message
                    or f"Requested {resolution_name} image upscale failed",
                )
                yield self._create_error_response(
                    error_msg,
                    status_code=502,
                    extra_fields={
                        "upscale_status": "failed",
                        "upscale_error_message": error_msg,
                    },
                )
                return

            local_url = image_url
            cache_started_at = time.time()
            requires_cached_fallback = bool(upsample_resolution)
            if config.cache_enabled:
                await self._update_request_log_progress(
                    request_log_state,
                    token_id=token.id,
                    status_text="caching_image",
                    progress=90,
                )
                if stream:
                    yield self._create_stream_chunk("正在缓存 1K 图片文件...\n")
                try:
                    cached_filename = await self.file_cache.download_and_cache(
                        image_url,
                        "image",
                        api_key_id=api_key_id,
                        token_id=token.id,
                        flow_project_id=project_id,
                    )
                    local_url = self._build_cache_url(
                        cached_filename, response_state, flow_project_id=project_id
                    )
                    if stream:
                        yield self._create_stream_chunk("✅ 1K 图片缓存成功,准备返回缓存地址...\n")
                except Exception as e:
                    debug_logger.log_error(f"Failed to cache 1K image: {str(e)}")
                    if requires_cached_fallback:
                        error_msg = (
                            "Upscale failed and 1K fallback cache failed; "
                            "refusing to return source Google URL"
                        )
                        if stream:
                            cache_error = self._normalize_error_message(e, max_length=120)
                            yield self._create_stream_chunk(f"{error_msg}: {cache_error}\n")
                        self._mark_generation_failed(generation_result, error_msg, status_code=502)
                        yield self._create_error_response(error_msg, status_code=502)
                        return
                    local_url = image_url
                    if stream:
                        cache_error = self._normalize_error_message(e, max_length=120)
                        yield self._create_stream_chunk(f"⚠️ 缓存失败: {cache_error}\n正在返回源链接...\n")
            elif requires_cached_fallback:
                error_msg = (
                    "Upscale failed and file cache is disabled; "
                    "refusing to return source Google URL"
                )
                if stream:
                    yield self._create_stream_chunk(f"{error_msg}\n")
                self._mark_generation_failed(generation_result, error_msg, status_code=502)
                yield self._create_error_response(error_msg, status_code=502)
                return
            elif stream:
                yield self._create_stream_chunk("缓存已关闭,正在返回官方图片链接...\n")
            if image_trace is not None:
                image_trace["cache_image_ms"] = int((time.time() - cache_started_at) * 1000)

            # 返回结果
            # 存储URL用于日志记录
            response_state["url"] = local_url
            response_state["generated_assets"] = {
                "type": "image",
                "origin_image_url": image_url,
                "final_image_url": local_url
            }
            self._mark_generation_succeeded(generation_result)

            if stream:
                yield self._create_stream_chunk(
                    f"![Generated Image]({local_url})",
                    finish_reason="stop",
                    extra_fields={"generated_assets": response_state.get("generated_assets")},
                )
            else:
                yield self._create_completion_response(
                    local_url,  # 直接传URL,让方法内部格式化
                    media_type="image",
                    extra_fields={"generated_assets": response_state.get("generated_assets")},
                )

        finally:
            pass

    async def _handle_video_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool,
        api_key_id: Optional[int] = None,
        perf_trace: Optional[Dict[str, Any]] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        pending_token_state: Optional[Dict[str, bool]] = None,
        video_media_id: Optional[str] = None,
        poll_task_id: Optional[str] = None,
    ) -> AsyncGenerator:
        """处理视频生成 (异步轮询)"""

        if response_state is None:
            response_state = self._create_response_state()

        video_trace: Optional[Dict[str, Any]] = None
        if isinstance(perf_trace, dict):
            video_trace = perf_trace.setdefault("video_generation", {})
            video_trace["input_image_count"] = len(images) if images else 0

        # 不在本地等待视频硬并发槽位；请求一到就直接向上游提交。
        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
        poll_hook = self._build_poll_task_progress_hook(
            poll_task_id,
            request_log_state=request_log_state,
            token_id=token.id,
        )

        if video_trace is not None:
            video_trace["slot_wait_ms"] = 0

        await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="preparing_video", progress=24)

        try:
            # 获取模型类型和配置
            video_type = model_config.get("video_type")
            supports_images = model_config.get("supports_images", False)
            min_images = model_config.get("min_images", 0)
            max_images = model_config.get("max_images", 0)
            use_v2_model_config = bool(model_config.get("use_v2_model_config", False))

            # 根据账号tier自动调整模型 key
            user_tier = normalized_tier
            original_model_key = model_config["model_key"]
            model_key, tier_message = self._resolve_video_model_key_for_tier(model_config, user_tier)
            if tier_message:
                if stream:
                    yield self._create_stream_chunk(f"{tier_message}\n")
                debug_logger.log_info(f"[VIDEO] 账号层级模型调整: {original_model_key} -> {model_key}")
            elif user_tier == "PAYGATE_TIER_TWO" and original_model_key == model_key:
                debug_logger.log_info(f"[VIDEO] TIER_TWO 账号，未找到有效 ultra 变体，保持模型: {model_key}")

            # 更新 model_config 中的 model_key
            model_config = dict(model_config)  # 创建副本避免修改原配置
            model_config["model_key"] = model_key

            # 图片数量
            image_count = len(images) if images else 0

            # ========== 验证和处理图片 ==========

            # T2V: 文生视频 - 不支持图片
            if video_type == "t2v":
                if image_count > 0:
                    if stream:
                        yield self._create_stream_chunk("⚠️ 文生视频模型不支持上传图片,将忽略图片仅使用文本提示词生成\n")
                    debug_logger.log_warning(f"[T2V] 模型 {model_config['model_key']} 不支持图片,已忽略 {image_count} 张图片")
                images = None  # 清空图片
                image_count = 0

            # I2V: 首尾帧模型 - 需要1-2张图片
            elif video_type == "i2v":
                if image_count < min_images or image_count > max_images:
                    error_msg = f"❌ 首尾帧模型需要 {min_images}-{max_images} 张图片,当前提供了 {image_count} 张"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # R2V: 多图生成 - 当前上游协议最多 3 张参考图
            elif video_type == "r2v":
                if max_images is not None and image_count > max_images:
                    error_msg = f"❌ 多图视频模型最多支持 {max_images} 张参考图,当前提供了 {image_count} 张"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # ========== 上传图片 ==========
            if video_type == "omni" and max_images is not None and image_count > max_images:
                error_msg = f"Omni supports at most {max_images} reference images; received {image_count}"
                if stream:
                    yield self._create_stream_chunk(f"{error_msg}\n")
                self._mark_generation_failed(generation_result, error_msg)
                yield self._create_error_response(error_msg, status_code=400)
                return

            start_media_id = None
            end_media_id = None
            reference_images = []

            # I2V: 首尾帧处理
            if video_type == "i2v" and images:
                if image_count == 1:
                    # 只有1张图: 仅作为首帧
                    if stream:
                        yield self._create_stream_chunk("上传首帧图片...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"], project_id=project_id
                    )
                    debug_logger.log_info(f"[I2V] 仅上传首帧: {start_media_id}")

                elif image_count == 2:
                    # 2张图: 首帧+尾帧
                    if stream:
                        yield self._create_stream_chunk("上传首帧和尾帧图片...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"], project_id=project_id
                    )
                    end_media_id = await self.flow_client.upload_image(
                        token.at, images[1], model_config["aspect_ratio"], project_id=project_id
                    )
                    debug_logger.log_info(f"[I2V] 上传首尾帧: {start_media_id}, {end_media_id}")

            # R2V: 多图处理
            elif video_type in {"r2v", "omni"} and images:
                if stream:
                    yield self._create_stream_chunk(f"上传 {image_count} 张参考图片...\n")

                for img in images:
                    media_id = await self.flow_client.upload_image(
                        token.at, img, model_config["aspect_ratio"], project_id=project_id
                    )
                    reference_images.append({
                        "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                        "mediaId": media_id
                    })
                debug_logger.log_info(f"[R2V] 上传了 {len(reference_images)} 张参考图片")

            # ========== 调用生成API ==========
            if stream:
                yield self._create_stream_chunk("提交视频生成任务...\n")
            submit_started_at = time.time()

            async def _submit_video_operation():
                # I2V: 首尾帧生成
                if video_type == "i2v" and start_media_id:
                    if end_media_id:
                        return await self.flow_client.generate_video_start_end(
                            at=token.at,
                            project_id=project_id,
                            prompt=prompt,
                            model_key=model_config["model_key"],
                            aspect_ratio=model_config["aspect_ratio"],
                            start_media_id=start_media_id,
                            end_media_id=end_media_id,
                            use_v2_model_config=use_v2_model_config,
                            user_paygate_tier=normalized_tier,
                            token_id=token.id,
                            token_video_concurrency=token.video_concurrency,
                            poll_task_progress=poll_hook,
                        )
                    # 只有首帧 - 需要去掉 model_key 中的 _fl
                    actual_model_key = model_config["model_key"].replace("_fl_", "_")
                    if actual_model_key.endswith("_fl"):
                        actual_model_key = actual_model_key[:-3]
                    debug_logger.log_info(
                        f"[I2V] 单帧模式，model_key: {model_config['model_key']} -> {actual_model_key}"
                    )
                    return await self.flow_client.generate_video_start_image(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=actual_model_key,
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        use_v2_model_config=use_v2_model_config,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                        poll_task_progress=poll_hook,
                    )
                if video_type in {"r2v", "omni"} and reference_images:
                    if video_type == "omni":
                        return await self.flow_client.generate_omni_reference_video(
                            at=token.at,
                            st=token.st,
                            project_id=project_id,
                            prompt=prompt,
                            aspect_ratio=model_config["aspect_ratio"],
                            reference_media_ids=[item["mediaId"] for item in reference_images],
                            model_usage_key=model_config.get("reference_model_key", "abra_r2v_8s"),
                            model_display_name=model_config.get("reference_model_display_name", "Omni Flash"),
                            duration=int(model_config.get("reference_duration", 8)),
                            user_paygate_tier=normalized_tier,
                            token_id=token.id,
                            token_video_concurrency=token.video_concurrency,
                            poll_task_progress=poll_hook,
                        )
                    return await self.flow_client.generate_video_reference_images(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=(
                            model_config.get("reference_model_key", "abra_r2v_8s")
                            if video_type == "omni"
                            else model_config["model_key"]
                        ),
                        aspect_ratio=model_config["aspect_ratio"],
                        reference_images=reference_images,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                        poll_task_progress=poll_hook,
                    )
                if video_type == "extend":
                    if not video_media_id:
                        raise ValueError(
                            "❌ 视频续写需要提供源视频的 mediaGenerationId，请在 image_url 中传入 extend://VIDEO_MEDIA_ID"
                        )
                    return await self.flow_client.generate_video_extend(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        video_media_id=video_media_id,
                        model_key=model_config["model_key"],
                        aspect_ratio=model_config["aspect_ratio"],
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                        poll_task_progress=poll_hook,
                    )
                return await self.flow_client.generate_video_text(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    use_v2_model_config=use_v2_model_config,
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                    poll_task_progress=poll_hook,
                )

            try:
                result = await self._execute_with_extension_fallback(
                    "submit_video_generation",
                    _submit_video_operation,
                    _submit_video_operation,
                )
            except ValueError as e:
                error_msg = str(e)
                if stream:
                    yield self._create_stream_chunk(f"{error_msg}\n")
                self._mark_generation_failed(generation_result, error_msg)
                yield self._create_error_response(error_msg, status_code=400)
                return
            if video_trace is not None:
                video_trace["submit_generation_ms"] = int((time.time() - submit_started_at) * 1000)

            # 获取task_id和operations
            operations = result.get("operations", [])
            if not operations:
                self._mark_generation_failed(generation_result, "\u751f\u6210\u4efb\u52a1\u521b\u5efa\u5931\u8d25")
                yield self._create_error_response("生成任务创建失败", status_code=502)
                return

            operation = operations[0]
            task_id = operation["operation"]["name"]
            scene_id = operation.get("sceneId")

            # 保存Task到数据库
            task = Task(
                task_id=task_id,
                token_id=token.id,
                api_key_id=api_key_id,
                model=model_config["model_key"],
                prompt=prompt,
                status="processing",
                scene_id=scene_id
            )
            await self.db.create_task(task)
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="video_submitted",
                progress=45,
                response_extra={"task_id": task_id, "scene_id": scene_id},
            )

            # 轮询结果
            if stream:
                yield self._create_stream_chunk(f"视频生成中...\n")

            # 检查是否需要放大
            upsample_config = model_config.get("upsample")

            extend_source_id = video_media_id if video_type == "extend" else None
            async for chunk in self._poll_video_result(
                token,
                project_id,
                operations,
                stream,
                upsample_config,
                api_key_id,
                generation_result,
                response_state,
                request_log_state,
                extend_source_media_id=extend_source_id,
                poll_task_id=poll_task_id,
            ):
                yield chunk

        finally:
            pass

    async def _poll_video_result(
        self,
        token,
        project_id: str,
        operations: List[Dict],
        stream: bool,
        upsample_config: Optional[Dict] = None,
        api_key_id: Optional[int] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        extend_source_media_id: Optional[str] = None,
        poll_task_id: Optional[str] = None,
    ) -> AsyncGenerator:
        """轮询视频生成结果
        
        Args:
            upsample_config: 放大配置 {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
        """

        if response_state is None:
            response_state = self._create_response_state()

        poll_hook = self._build_poll_task_progress_hook(
            poll_task_id,
            request_log_state=request_log_state,
            token_id=token.id,
        )

        max_attempts = config.max_poll_attempts
        poll_interval = config.poll_interval
        
        # 如果需要放大，轮询次数加倍（放大可能需要 30 分钟）
        if upsample_config:
            max_attempts = max_attempts * 3  # 放大需要更长时间

        consecutive_poll_errors = 0
        last_poll_error: Optional[Exception] = None
        max_consecutive_poll_errors = 3

        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)

            try:
                result = await self.flow_client.check_video_status(token.at, operations)
                checked_operations = result.get("operations", [])
                consecutive_poll_errors = 0
                last_poll_error = None

                operation = checked_operations[0] if checked_operations else {}
                status = operation.get("status") if isinstance(operation, dict) else None
                progress = min(int((attempt / max_attempts) * 100), 95)
                await self._update_request_log_progress(
                    request_log_state,
                    token_id=token.id,
                    status_text="video_polling",
                    progress=max(45, progress),
                    response_extra={"upstream_status": status or "pending"},
                )

                # 流式文本仍每20秒报告一次 (poll_interval=3秒, 20秒约7次轮询)
                progress_update_interval = 7  # 每7次轮询 = 21秒
                if stream and attempt % progress_update_interval == 0:
                    yield self._create_stream_chunk(f"生成进度: {progress}%\n")

                if not checked_operations:
                    continue

                # 检查状态
                if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                    resolved_video = await self._resolve_video_asset(
                        token,
                        operation,
                        api_key_id=api_key_id,
                        project_id=project_id,
                        response_state=response_state,
                    )
                    metadata = resolved_video["metadata"]
                    video_info = resolved_video["video_info"]
                    source_video_url = resolved_video["video_url"]
                    video_media_id = resolved_video["video_media_id"]
                    aspect_ratio = resolved_video["aspect_ratio"]
                    video_is_cached = bool(resolved_video.get("video_is_cached"))

                    if not source_video_url and not video_media_id:
                        error_msg = "视频生成失败: 视频URL为空"
                        operation_body = operation.get("operation", {}) if isinstance(operation, dict) else {}
                        url_empty_diagnostics = {
                            "operation_name": operation_body.get("name") if isinstance(operation_body, dict) else None,
                            "status": status,
                            "metadata_keys": sorted(metadata.keys()) if isinstance(metadata, dict) else [],
                            "video_keys": sorted(video_info.keys()) if isinstance(video_info, dict) else [],
                            "media_id": (video_media_id or operation.get("mediaName")) if isinstance(operation, dict) else None,
                        }
                        debug_logger.log_error(
                            f"[VIDEO] successful status missing URL: {url_empty_diagnostics}"
                        )
                        await self._fail_video_task(checked_operations, error_msg)
                        self._mark_generation_failed(
                            generation_result,
                            error_msg,
                            status_code=502,
                            error_extra={"video_url_empty_diagnostics": url_empty_diagnostics},
                        )
                        yield self._create_error_response(error_msg, status_code=502)
                        return

                    # ========== 视频放大处理 ==========
                    if upsample_config and video_media_id:
                        if stream:
                            resolution_name = "4K" if "4K" in upsample_config["resolution"] else "1080P"
                            yield self._create_stream_chunk(f"\n视频生成完成，开始 {resolution_name} 放大处理...（可能需要 30 分钟）\n")
                        
                        try:
                            await self._maybe_update_poll_task(
                                poll_task_id,
                                job_phase="upscale_captcha",
                                captcha_status="pending",
                            )
                            # 提交放大任务
                            async def _submit_video_upsample():
                                return await self.flow_client.upsample_video(
                                    at=token.at,
                                    project_id=project_id,
                                    video_media_id=video_media_id,
                                    aspect_ratio=aspect_ratio,
                                    resolution=upsample_config["resolution"],
                                    model_key=upsample_config["model_key"],
                                    token_id=token.id,
                                    token_video_concurrency=token.video_concurrency,
                                    poll_task_progress=poll_hook,
                                )

                            upsample_result = await self._execute_with_extension_fallback(
                                "submit_video_upsample",
                                _submit_video_upsample,
                                _submit_video_upsample,
                            )
                            
                            upsample_operations = upsample_result.get("operations", [])
                            if upsample_operations:
                                if stream:
                                    yield self._create_stream_chunk("放大任务已提交，继续轮询...\n")
                                
                                # 递归轮询放大结果（不再放大）
                                async for chunk in self._poll_video_result(
                                    token,
                                    project_id,
                                    upsample_operations,
                                    stream,
                                    None,
                                    api_key_id,
                                    generation_result,
                                    response_state,
                                    request_log_state,
                                    poll_task_id=poll_task_id,
                                    extend_source_media_id=extend_source_media_id,
                                ):
                                    yield chunk
                                return
                            else:
                                if stream:
                                    yield self._create_stream_chunk("⚠️ 放大任务创建失败，返回原始视频\n")
                        except Exception as e:
                            debug_logger.log_error(f"Video upsample failed: {str(e)}")
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ 放大失败: {str(e)}，返回原始视频\n")

                    if extend_source_media_id and video_media_id:
                        try:
                            concat_result = await self.flow_client.run_concatenation(
                                at=token.at,
                                original_media_id=extend_source_media_id,
                                extend_media_id=video_media_id,
                            )
                            concat_op = concat_result.get("operation", {}).get("operation", {}).get("name", "")
                            if concat_op:
                                concat_status = await self.flow_client.poll_concatenation_status(
                                    at=token.at,
                                    operation_name=concat_op,
                                    timeout=300,
                                    poll_interval=3,
                                )
                                concat_url = concat_status.get("outputUri", "")
                                if concat_url:
                                    if concat_url.startswith("/tmp/"):
                                        host = "localhost" if (config.server_host or "") == "0.0.0.0" else (config.server_host or "localhost")
                                        concat_url = f"http://{host}:{config.server_port}{concat_url}"
                                    source_video_url = concat_url
                                    video_is_cached = False
                        except Exception as e:
                            debug_logger.log_error(f"[CONCAT] 拼接失败: {str(e)}")

                    if _needs_video_url_resolve(source_video_url, video_media_id):
                        try:
                            source_video_url = await self.flow_client.resolve_media_download_url(
                                media_id=video_media_id,
                                st=token.st,
                                at=token.at,
                                token_id=token.id,
                            )
                        except Exception as resolve_error:
                            media_fetch_error = str(resolved_video.get("media_fetch_error") or "").strip()
                            error_msg = (
                                "Video generated successfully, but resolving the download URL failed: "
                                f"{self._normalize_error_message(resolve_error, max_length=240)}"
                            )
                            if media_fetch_error:
                                error_msg += f"; get_media fallback: {media_fetch_error}"
                            debug_logger.log_error(f"[VIDEO] media redirect resolve failed: {resolve_error}")
                            await self._fail_video_task(checked_operations, error_msg)
                            self._mark_generation_failed(
                                generation_result,
                                error_msg,
                                status_code=502,
                                error_extra={
                                    "video_cache_status": "resolve_failed",
                                    "get_media_error": media_fetch_error or None,
                                },
                            )
                            if stream:
                                yield self._create_stream_chunk(f"Error: {error_msg}\n")
                            yield self._create_error_response(
                                error_msg,
                                status_code=502,
                                extra_fields={
                                    "video_cache_status": "resolve_failed",
                                    "get_media_error": media_fetch_error or None,
                                },
                            )
                            return

                    source_video_url = source_video_url or ""
                    source_video_is_safe = (
                        source_video_url.startswith(("http://", "https://"))
                        and (
                            "flow-content.google" in source_video_url
                            or "Signature=" in source_video_url
                        )
                    )
                    if video_is_cached:
                        local_url = source_video_url
                        if stream:
                            yield self._create_stream_chunk("Video fetched and cached successfully...\n")
                    elif not config.cache_enabled:
                        if not source_video_is_safe:
                            error_msg = (
                                "Video generated successfully, but file cache is disabled "
                                "and this video URL requires backend-authenticated delivery"
                            )
                            await self._fail_video_task(checked_operations, error_msg)
                            self._mark_generation_failed(
                                generation_result,
                                error_msg,
                                status_code=502,
                                error_extra={"video_cache_status": "cache_required"},
                            )
                            if stream:
                                yield self._create_stream_chunk(f"Error: {error_msg}\n")
                            yield self._create_error_response(
                                error_msg,
                                status_code=502,
                                error_code="cache_required",
                                extra_fields={"video_cache_status": "cache_required"},
                            )
                            return
                        local_url = source_video_url
                        if stream:
                            yield self._create_stream_chunk("Cache disabled, returning source video URL...\n")
                    else:
                        # Generated videos are cached when file cache is enabled so
                        # clients receive a stable sub2gen-owned delivery URL.
                        await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="caching_video", progress=92)
                        try:
                            if stream:
                                yield self._create_stream_chunk("Caching generated video file...\n")
                            cache_auth_token = None
                            if not source_video_is_safe:
                                cache_auth_token = token.at
                            cached_filename = await self.file_cache.download_and_cache(
                                source_video_url,
                                "video",
                                api_key_id=api_key_id,
                                token_id=token.id,
                                flow_project_id=project_id,
                                auth_token=cache_auth_token,
                                session_token=token.st if "getMediaUrlRedirect" in source_video_url else None,
                            )
                            local_url = self._build_cache_url(
                                cached_filename, response_state, flow_project_id=project_id
                            )
                            if stream:
                                yield self._create_stream_chunk("Video cached successfully, returning local cache URL...\n")
                        except Exception as e:
                            cache_error = self._normalize_error_message(e, max_length=240)
                            error_msg = f"Video generated successfully, but caching the video failed: {cache_error}"
                            debug_logger.log_error(f"Failed to cache video: {str(e)}")
                            if "flow-content.google" in cache_error:
                                video_cache_status = "cdn_download_rejected"
                            elif "not valid media" in cache_error.lower():
                                video_cache_status = "cdn_invalid_body"
                            else:
                                video_cache_status = "failed"
                            await self._fail_video_task(checked_operations, error_msg)
                            self._mark_generation_failed(
                                generation_result,
                                error_msg,
                                status_code=502,
                                error_extra={"video_cache_status": video_cache_status},
                            )
                            if stream:
                                yield self._create_stream_chunk(f"Error: {error_msg}\n")
                            yield self._create_error_response(
                                error_msg,
                                status_code=502,
                                extra_fields={"video_cache_status": video_cache_status},
                            )
                            return

                    # 更新数据库
                    task_id = operation["operation"]["name"]
                    await self.db.update_task(
                        task_id,
                        status="completed",
                        progress=100,
                        result_urls=[local_url],
                        completed_at=time.time()
                    )

                    # 存储URL用于日志记录
                    response_state["url"] = local_url
                    response_state["generated_assets"] = {
                        "type": "video",
                        "final_video_url": local_url,
                        "mediaGenerationId": video_media_id
                    }

                    # 返回结果
                    self._mark_generation_succeeded(generation_result)

                    if stream:
                        yield self._create_stream_chunk(
                            f"<video src='{local_url}' data-media-id='{video_media_id}' controls style='max-width:100%'></video>",
                            finish_reason="stop",
                            extra_fields={"generated_assets": response_state.get("generated_assets")},
                        )
                    else:
                        yield self._create_completion_response(
                            local_url,  # 直接传URL,让方法内部格式化
                            media_type="video",
                            extra_fields={"generated_assets": response_state.get("generated_assets")},
                        )
                    return

                elif status == "MEDIA_GENERATION_STATUS_FAILED":
                    # 生成失败 - 提取错误信息
                    error_info = operation.get("operation", {}).get("error", {})
                    error_code = error_info.get("code", "unknown")
                    error_message = error_info.get("message", "未知错误")
                    
                    # 更新数据库任务状态
                    await self._fail_video_task(
                        checked_operations,
                        f"{error_message} (code: {error_code})"
                    )
                    
                    # 返回友好的错误消息，提示用户重试
                    friendly_error = f"视频生成失败: {error_message}，请重试"
                    self._mark_generation_failed(generation_result, friendly_error)
                    if stream:
                        yield self._create_stream_chunk(f"❌ {friendly_error}\n")
                    yield self._create_error_response(friendly_error, status_code=502)
                    return
                elif status == "MEDIA_GENERATION_STATUS_ACTIVE" and attempt > 80:
                    # 如果持续4分钟（80次 * 3秒 = 240秒）依然是 ACTIVE 状态，则判定为卡死
                    error_msg = "视频生成超时 (上游卡顿超过4分钟，已自动取消)"
                    await self._fail_video_task(checked_operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    if stream:
                        yield self._create_stream_chunk(f"❌ {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=504)
                    return

                elif status.startswith("MEDIA_GENERATION_STATUS_ERROR"):
                    # ??????
                    error_msg = f"视频生成失败: {status}"
                    await self._fail_video_task(checked_operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=502)
                    return

            except Exception as e:
                last_poll_error = e
                consecutive_poll_errors += 1
                debug_logger.log_error(f"Poll error: {str(e)}")
                if consecutive_poll_errors >= max_consecutive_poll_errors:
                    if self._is_extension_generation_enabled() and self._is_backend_poll_blocked_error(e):
                        try:
                            debug_logger.log_warning("[EXT-GEN] backend poll blocked, trying extension poll fallback")
                            operation_status = await self.flow_client.check_video_status_via_extension_poll(
                                token.at,
                                operations,
                            )
                            checked_operations = operation_status.get("operations", [])
                            if checked_operations:
                                operation = checked_operations[0]
                                status = operation.get("status", "")
                                if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                                    metadata = operation.get("operation", {}).get("metadata", {})
                                    video_url = self._extract_video_url(metadata)
                                    if video_url:
                                        local_url = video_url
                                        task_id = operation["operation"]["name"]
                                        await self.db.update_task(
                                            task_id,
                                            status="completed",
                                            progress=100,
                                            result_urls=[local_url],
                                            completed_at=time.time()
                                        )
                                        response_state["url"] = local_url
                                        response_state["generated_assets"] = {
                                            "type": "video",
                                            "final_video_url": local_url,
                                            "mediaGenerationId": video_media_id
                                        }
                                        self._mark_generation_succeeded(generation_result)
                                        if stream:
                                            yield self._create_stream_chunk(
                                                f"<video src='{local_url}' data-media-id='{video_media_id}' controls style='max-width:100%'></video>",
                                                finish_reason="stop",
                                                extra_fields={"generated_assets": response_state.get("generated_assets")},
                                            )
                                        else:
                                            yield self._create_completion_response(
                                                local_url,
                                                media_type="video",
                                                extra_fields={"generated_assets": response_state.get("generated_assets")},
                                            )
                                        return
                        except Exception as ext_poll_err:
                            debug_logger.log_error(f"[EXT-GEN] extension poll fallback failed: {ext_poll_err}")
                    error_msg = f"视频状态查询失败: {self._normalize_error_message(e)}"
                    await self._fail_video_task(operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    if stream:
                        yield self._create_stream_chunk(f"❌ {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=502)
                    return
                continue

        # 超时
        if last_poll_error is not None:
            error_msg = f"视频状态查询持续失败: {self._normalize_error_message(last_poll_error)}"
        else:
            error_msg = f"视频生成超时 (已轮询 {max_attempts} 次)"
        await self._fail_video_task(operations, error_msg)
        self._mark_generation_failed(generation_result, error_msg)
        yield self._create_error_response(error_msg, status_code=504)

    # ========== 响应格式化 ==========

    @staticmethod
    def _format_stream_reasoning_for_ui(content: str) -> str:
        """Make progress / thought text render as separate steps in chat UIs.

        Many clients display ``reasoning_content`` as HTML without ``pre-wrap``, so a single
        ``\\n`` between lines collapses into one paragraph. Splitting on line breaks and
        joining with blank lines yields Markdown-style paragraph breaks.
        """
        if not content:
            return ""
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if not lines:
            return ""
        return "\n\n".join(lines) + "\n\n"

    def _create_stream_chunk(
        self,
        content: str,
        role: str = None,
        finish_reason: str = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> str:
        """创建流式响应chunk"""
        import json
        import time

        chunk = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "sub2gen",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason
            }]
        }

        if role:
            chunk["choices"][0]["delta"]["role"] = role

        if finish_reason:
            chunk["choices"][0]["delta"]["content"] = content
        else:
            chunk["choices"][0]["delta"]["reasoning_content"] = self._format_stream_reasoning_for_ui(content)

        if extra_fields and finish_reason:
            for key, value in extra_fields.items():
                if value is not None and key not in chunk:
                    chunk[key] = value

        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    def _create_completion_response(
        self,
        content: str,
        media_type: str = "image",
        is_availability_check: bool = False,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> str:
        """创建非流式响应

        Args:
            content: 媒体URL或纯文本消息
            media_type: 媒体类型 ("image" 或 "video")
            is_availability_check: 是否为可用性检查响应 (纯文本消息)

        Returns:
            JSON格式的响应
        """
        import json
        import time

        # 可用性检查: 返回纯文本消息
        if is_availability_check:
            formatted_content = content
        else:
            # 媒体生成: 根据媒体类型格式化内容为Markdown
            if media_type == "video":
                formatted_content = f"```html\n<video src='{content}' controls></video>\n```"
            else:  # image
                formatted_content = f"![Generated Image]({content})"

        response = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "sub2gen",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": formatted_content
                },
                "finish_reason": "stop"
            }]
        }

        if extra_fields:
            for key, value in extra_fields.items():
                if value is not None and key not in response:
                    response[key] = value

        return json.dumps(response, ensure_ascii=False)

    def _create_error_response(
        self,
        error_message: str,
        status_code: int = 500,
        error_code: str = "generation_failed",
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> str:
        """创建错误响应"""
        import json

        error = {
            "error": {
                "message": error_message,
                "type": "server_error" if status_code >= 500 else "invalid_request_error",
                "code": error_code,
                "status_code": status_code,
            }
        }
        if isinstance(extra_fields, dict):
            for key, value in extra_fields.items():
                if value is not None and key not in error:
                    error[key] = value

        return json.dumps(error, ensure_ascii=False)

    def _get_base_url(self, response_state: Optional[Dict[str, Any]] = None) -> str:
        """获取基础URL用于缓存文件访问"""
        # 已配置缓存访问域名时，始终优先使用它，避免被请求 Host/IP 覆盖。
        if config.cache_base_url:
            return config.cache_base_url.rstrip("/")

        request_base_url = ""
        if isinstance(response_state, dict):
            request_base_url = (response_state.get("base_url") or "").strip().rstrip("/")
        if request_base_url:
            return request_base_url

        # 回退到服务地址，避免把监听地址 0.0.0.0 / :: 直接返回给客户端
        server_host = (config.server_host or "").strip()
        if server_host in {"", "0.0.0.0", "::", "[::]"}:
            server_host = "127.0.0.1"

        return f"http://{server_host}:{config.server_port}"

    def _build_cache_url(
        self,
        filename: str,
        response_state: Optional[Dict[str, Any]] = None,
        flow_project_id: Optional[str] = None,
    ) -> str:
        builder = getattr(self.file_cache, "build_url", None)
        if callable(builder):
            result = builder(
                filename,
                self._get_base_url(response_state),
                flow_project_id=flow_project_id,
            )
            if isinstance(result, str):
                return result
        base = f"{self._get_base_url(response_state)}/api/cache/blob/{filename}"
        pid = (flow_project_id or "").strip()
        return f"{base}?project_id={quote(pid, safe='')}" if pid else base

    async def _update_request_log_progress(
        self,
        request_log_state: Optional[Dict[str, Any]],
        *,
        token_id: Optional[int] = None,
        api_key_id: Optional[int] = None,
        status_text: str,
        progress: int,
        response_extra: Optional[Dict[str, Any]] = None,
    ):
        """?????????????"""
        if not isinstance(request_log_state, dict):
            return
        log_id = request_log_state.get("id")
        if not log_id:
            return

        safe_progress = max(0, min(100, int(progress)))
        now = time.time()
        last_status_text = str(request_log_state.get("last_status_text") or "").strip()
        last_progress = int(request_log_state.get("last_progress") or 0)
        last_updated_at = float(request_log_state.get("last_progress_update_at") or 0)

        request_log_state["progress"] = safe_progress
        request_log_state["last_status_text"] = status_text
        request_log_state["last_progress"] = safe_progress
        payload = {
            "status": "processing",
            "status_text": status_text,
            "progress": safe_progress,
        }
        request_id = str(request_log_state.get("request_id") or "").strip()
        if request_id:
            payload["request_id"] = request_id
        if isinstance(response_extra, dict):
            payload.update(response_extra)
        if request_log_state.get("captcha_user_agent_set"):
            payload["captcha_user_agent_set"] = True
            provider = str(request_log_state.get("captcha_provider") or "").strip()
            if provider:
                payload["captcha_provider"] = provider

        should_write = (
            safe_progress in (0, 100)
            or status_text != last_status_text
            or safe_progress >= last_progress + 5
            or (now - last_updated_at) >= 1.0
        )
        if not should_write:
            return

        request_log_state["last_progress_update_at"] = now

        try:
            effective_api_key_id = api_key_id
            if effective_api_key_id is None and isinstance(request_log_state, dict):
                effective_api_key_id = request_log_state.get("api_key_id")
            repository = getattr(self, "request_logs", None) or self.db
            await repository.update_request_log(
                log_id,
                token_id=token_id,
                api_key_id=effective_api_key_id,
                response_body=json.dumps(payload, ensure_ascii=False),
                status_code=102,
                duration=0,
                status_text=status_text,
                progress=safe_progress,
            )
        except Exception as e:
            debug_logger.log_error(f"Failed to update request log progress: {e}")

    async def _log_request(
        self,
        token_id: Optional[int],
        api_key_id: Optional[int],
        operation: str,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        status_code: int,
        duration: float,
        log_id: Optional[int] = None,
        status_text: Optional[str] = None,
        progress: Optional[int] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
    ):
        """???????????? log_id ????????"""
        try:
            effective_status_text = status_text or (
                "completed" if status_code == 200 else "failed" if status_code >= 400 else "processing"
            )
            effective_progress = progress
            if effective_progress is None:
                effective_progress = 100 if status_code == 200 else 0 if status_code >= 400 else 0
            effective_progress = max(0, min(100, int(effective_progress)))

            effective_response_data = dict(response_data)
            if isinstance(request_log_state, dict) and request_log_state.get("captcha_user_agent_set"):
                metadata: Dict[str, Any] = {"captcha_user_agent_set": True}
                provider = str(request_log_state.get("captcha_provider") or "").strip()
                if provider:
                    metadata["captcha_provider"] = provider
                metadata.update(effective_response_data)
                effective_response_data = metadata

            request_body = json.dumps(request_data, ensure_ascii=False)
            response_body = json.dumps(effective_response_data, ensure_ascii=False)

            if log_id:
                repository = getattr(self, "request_logs", None) or self.db
                await repository.update_request_log(
                    log_id,
                    token_id=token_id,
                    api_key_id=api_key_id,
                    operation=operation,
                    request_body=request_body,
                    response_body=response_body,
                    status_code=status_code,
                    duration=duration,
                    status_text=effective_status_text,
                    progress=effective_progress,
                )
                return log_id

            log = RequestLog(
                token_id=token_id,
                api_key_id=api_key_id,
                operation=operation,
                request_body=request_body,
                response_body=response_body,
                status_code=status_code,
                duration=duration,
                status_text=effective_status_text,
                progress=effective_progress,
            )
            repository = getattr(self, "request_logs", None) or self.db
            return await repository.add_request_log(log)
        except Exception as e:
            debug_logger.log_error(f"Failed to log request: {e}")
            return None
