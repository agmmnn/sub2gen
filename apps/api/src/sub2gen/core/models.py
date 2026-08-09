"""Data models for sub2gen"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Optional, List, Union, Any, Literal, Dict
from datetime import datetime


class Token(BaseModel):
    """Token model for sub2gen"""

    id: Optional[int] = None

    # 认证信息 (核心)
    st: str  # Session Token (__Secure-next-auth.session-token)
    at: Optional[str] = None  # Access Token (从ST转换而来)
    at_expires: Optional[datetime] = None  # AT过期时间
    auth_mode: str = "session_token"  # session_token | browser_profile
    browser_profile_path: Optional[str] = None
    browser_profile_status: str = "not_created"
    browser_profile_email: Optional[str] = None
    browser_profile_name: Optional[str] = None
    browser_profile_login_state: str = "unknown"
    browser_profile_cookie_status: str = "unknown"
    browser_profile_st_status: str = "unknown"
    browser_profile_at_status: str = "unknown"
    browser_profile_last_opened_at: Optional[datetime] = None
    browser_profile_last_sync_at: Optional[datetime] = None
    browser_profile_last_refresh_at: Optional[datetime] = None
    browser_profile_last_error: Optional[str] = None

    # 基础信息
    email: str
    name: Optional[str] = ""
    remark: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    use_count: int = 0

    # VideoFX特有字段
    credits: int = 0  # 剩余credits
    user_paygate_tier: Optional[str] = None  # PAYGATE_TIER_ONE

    # 项目管理
    current_project_id: Optional[str] = None  # 当前使用的项目UUID
    current_project_name: Optional[str] = None  # 项目名称

    # 功能开关
    image_enabled: bool = True
    video_enabled: bool = True

    # 并发限制
    image_concurrency: int = -1  # -1表示无限制
    video_concurrency: int = -1  # -1表示无限制

    # 打码代理（token 级，可覆盖全局浏览器打码代理）
    captcha_proxy_url: Optional[str] = None
    # 插件路由键（extension 模式用于将请求路由到指定浏览器插件连接）
    extension_route_key: Optional[str] = None
    # When False, Flow image/video generation HTTP uses server curl path; extension still used for captcha (if captcha_method is extension).
    use_extension_for_generation: bool = True

    # Optional protocol-based ST refresh. Exported cookies are never returned by
    # the admin list API; only their presence is exposed.
    protocol_mode: Literal["session", "protocol"] = "session"
    google_cookies: str = Field(default="", repr=False)
    login_account: str = ""
    login_password: str = Field(default="", repr=False)
    proxy_url: str = Field(default="", repr=False)
    auto_refresh_enabled: bool = True
    refresh_interval_minutes: int = Field(default=120, ge=1, le=10080)
    last_st_refresh_at: Optional[datetime] = None
    last_st_refresh_result: str = ""

    # 429禁用相关
    ban_reason: Optional[str] = None  # 禁用原因: "429_rate_limit" 或 None
    banned_at: Optional[datetime] = None  # 禁用时间


class Project(BaseModel):
    """Project model for VideoFX"""

    id: Optional[int] = None
    project_id: str  # VideoFX项目UUID
    token_id: int  # 关联的Token ID
    api_key_id: Optional[int] = None  # 创建该项目的 API key
    project_name: str  # 项目名称
    tool_name: str = "PINHOLE"  # 工具名称,固定为PINHOLE
    is_active: bool = True
    created_at: Optional[datetime] = None


class TokenStats(BaseModel):
    """Token statistics"""

    token_id: int
    image_count: int = 0
    video_count: int = 0
    success_count: int = 0
    error_count: int = 0  # Historical total errors (never reset)
    last_success_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None
    # 今日统计
    today_image_count: int = 0
    today_video_count: int = 0
    today_error_count: int = 0
    today_date: Optional[str] = None
    # 连续错误计数 (用于自动禁用判断)
    consecutive_error_count: int = 0


class Task(BaseModel):
    """Generation task"""

    id: Optional[int] = None
    task_id: str  # Flow API返回的operation name
    token_id: int
    api_key_id: Optional[int] = None
    project_id: Optional[str] = None
    model: str
    prompt: str
    status: str  # processing, completed, failed
    progress: int = 0  # 0-100
    result_urls: Optional[List[str]] = None
    base_result_urls: Optional[List[str]] = None
    delivery_urls: Optional[List[str]] = None
    requested_resolution: Optional[str] = None
    output_resolution: Optional[str] = None
    upscale_status: Optional[str] = None  # not_requested, pending, processing, completed, failed
    upscale_error_message: Optional[str] = None
    error_message: Optional[str] = None
    scene_id: Optional[str] = None  # Flow API的sceneId
    job_phase: Optional[str] = None  # queued, generation_*, upscale_*, finalizing, completed, failed
    captcha_status: Optional[str] = None  # not_applicable, idle, pending, token_acquired, token_failed, upstream_rejected, unknown
    captcha_detail: Optional[str] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class RequestLog(BaseModel):
    """API request log"""

    id: Optional[int] = None
    token_id: Optional[int] = None
    api_key_id: Optional[int] = None
    operation: str
    request_body: Optional[str] = None
    response_body: Optional[str] = None
    status_code: int
    duration: float
    status_text: Optional[str] = None
    progress: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AdminConfig(BaseModel):
    """Admin configuration"""

    id: int = 1
    username: str
    password: str
    api_key: str
    error_ban_threshold: int = 3  # Auto-disable token after N consecutive errors
    error_ban_enabled: bool = True  # When False, consecutive errors do not auto-disable tokens

    @field_validator("error_ban_enabled", mode="before")
    @classmethod
    def coerce_error_ban_enabled(cls, v):
        if v is None:
            return True
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, str)):
            return bool(int(v))
        return bool(v)


class ProxyConfig(BaseModel):
    """Proxy configuration"""

    id: int = 1
    enabled: bool = False  # 请求代理开关
    proxy_url: Optional[str] = None  # 请求代理地址
    media_proxy_enabled: bool = False  # 图片上传/下载代理开关
    media_proxy_url: Optional[str] = None  # 图片上传/下载代理地址


class GenerationConfig(BaseModel):
    """Generation timeout configuration"""

    id: int = 1
    image_timeout: int = 300  # seconds
    video_timeout: int = 1500  # seconds
    max_retries: int = 3  # 请求最大重试次数
    extension_generation_enabled: bool = False
    extension_generation_fallback_mode: str = "local_http_on_recaptcha"
    sub2gen_gemini_api_keys: str = ""
    sub2gen_openai_api_keys: str = ""
    sub2gen_openrouter_api_keys: str = ""
    sub2gen_third_party_gemini_api_keys: str = ""
    sub2gen_third_party_gemini_base_url: str = ""
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    sub2gen_csvgen_cookie: str = ""
    sub2gen_csvgen_api_keys: str = ""
    sub2gen_cloning_model: str = "gemini-2.5-flash"
    sub2gen_metadata_backend: str = "gemini_native"
    sub2gen_metadata_provider_order: str = ""
    sub2gen_metadata_enabled_providers: str = ""
    sub2gen_metadata_provider_retry_count: int = 1
    sub2gen_metadata_model: str = "gemini-2.5-flash"
    sub2gen_metadata_enabled_models: str = ""
    sub2gen_metadata_primary_model: str = ""
    sub2gen_metadata_fallback_models: str = ""
    metadata_system_prompt: str = ""
    sub2gen_cloning_backend: str = "gemini_native"
    sub2gen_cloning_provider_order: str = ""
    sub2gen_cloning_enabled_providers: str = ""
    sub2gen_cloning_provider_retry_count: int = 1
    sub2gen_cloning_gemini_api_keys: str = ""
    sub2gen_cloning_openai_api_keys: str = ""
    sub2gen_cloning_openrouter_api_keys: str = ""
    sub2gen_cloning_third_party_gemini_api_keys: str = ""
    sub2gen_cloning_third_party_gemini_base_url: str = ""
    sub2gen_cloning_cloudflare_account_id: str = ""
    sub2gen_cloning_cloudflare_api_token: str = ""
    cloning_image_system_prompt: str = ""
    cloning_video_system_prompt: str = ""
    task_tracker_device_id: str = ""
    task_tracker_device_name: str = ""
    task_tracker_cookies: str = ""
    task_tracker_device_token: str = ""
    task_tracker_turnstile_token: str = ""
    task_tracker_tls_profile: str = ""


class CallLogicConfig(BaseModel):
    """Token selection call logic configuration"""

    id: int = 1
    call_mode: str = "default"
    polling_mode_enabled: bool = False
    updated_at: Optional[datetime] = None


class CacheConfig(BaseModel):
    """Cache configuration"""

    id: int = 1
    cache_enabled: bool = False
    cache_timeout: int = 7200  # seconds; UI uses days (max 7d = 604800s), 0 = never expire
    cache_base_url: Optional[str] = None
    cache_provider: str = "local"
    cache_delivery_mode: str = "proxy"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DebugConfig(BaseModel):
    """Debug configuration"""

    id: int = 1
    enabled: bool = False
    log_requests: bool = True
    log_responses: bool = True
    mask_token: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CaptchaConfig(BaseModel):
    """Captcha configuration"""

    id: int = 1
    captcha_method: str = "browser"  # yescaptcha/capmonster/ezcaptcha/capsolver/browser/personal/remote_browser
    yescaptcha_api_key: str = ""
    yescaptcha_base_url: str = "https://api.yescaptcha.com"
    yescaptcha_task_type: str = "RecaptchaV3TaskProxylessM1S9"
    capmonster_api_key: str = ""
    capmonster_base_url: str = "https://api.capmonster.cloud"
    capmonster_min_score: float = 0.9
    ezcaptcha_api_key: str = ""
    ezcaptcha_base_url: str = "https://api.ez-captcha.com"
    capsolver_api_key: str = ""
    capsolver_base_url: str = "https://api.capsolver.com"
    remote_browser_base_url: str = ""
    remote_browser_api_key: str = ""
    remote_browser_timeout: int = 60
    website_key: str = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
    page_action: str = "IMAGE_GENERATION"
    browser_proxy_enabled: bool = False  # 浏览器打码是否启用代理
    browser_proxy_url: Optional[str] = None  # 浏览器打码代理URL
    browser_captcha_page_url: str = "https://labs.google/fx/api/auth/providers"  # browser 模式打开的首页（可改为 Flow 工具页）
    browser_fallback_to_remote_browser: bool = True  # browser 模式失败时是否自动回退到 remote_browser
    browser_count: int = 1  # 浏览器打码实例数量
    browser_personal_fresh_restart_every_n_solves: int = 10  # personal 模式 fresh profile 轮换阈值，0 表示禁用
    personal_project_pool_size: int = 4  # 单个 Token 默认维护的项目池数量（仅影响项目轮换）
    personal_max_resident_tabs: int = 5  # 内置浏览器单实例共享打码标签页数量上限
    personal_idle_tab_ttl_seconds: int = 600  # 内置浏览器标签页空闲超时(秒)
    session_refresh_enabled: bool = True
    session_refresh_browser_first: bool = True
    session_refresh_inject_st_cookie: bool = True
    session_refresh_warmup_urls: str = "https://labs.google/fx/tools/flow,https://labs.google/fx"
    session_refresh_wait_seconds_per_url: int = 60
    session_refresh_overall_timeout_seconds: int = 180
    session_refresh_update_st_from_cookie: bool = True
    session_refresh_fail_if_st_refresh_fails: bool = True
    session_refresh_local_only: bool = True
    session_refresh_scheduler_enabled: bool = False
    session_refresh_scheduler_interval_minutes: int = 30
    session_refresh_scheduler_batch_size: int = 10
    session_refresh_scheduler_only_expiring_within_minutes: int = 60
    st_only_refresh_scheduler_enabled: bool = False
    st_only_refresh_scheduler_interval_minutes: int = 5
    st_only_refresh_scheduler_batch_size: int = 20
    st_only_refresh_scheduler_expiring_within_minutes: int = 5
    extension_queue_wait_timeout_seconds: int = 20
    extension_fallback_to_managed_on_dedicated_failure: bool = False
    dedicated_extension_enabled: bool = False
    dedicated_extension_captcha_timeout_seconds: int = 25
    dedicated_extension_st_refresh_timeout_seconds: int = 45
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PluginConfig(BaseModel):
    """Plugin connection configuration"""

    id: int = 1
    connection_token: str = ""  # 插件连接token
    auto_enable_on_update: bool = True  # 更新token时自动启用（默认开启）
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ExtensionWorkerBinding(BaseModel):
    """Route key to managed API key binding for extension workers."""

    id: Optional[int] = None
    route_key: str
    api_key_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CaptchaWorkerKey(BaseModel):
    """General captcha worker key for the shared reCAPTCHA worker pool."""

    id: Optional[int] = None
    key_prefix: str
    key_hash: str
    label: str = ""
    key_plaintext: Optional[str] = None
    is_active: bool = True
    last_instance_id: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RunwayConfig(BaseModel):
    """Global Runway integration configuration."""

    id: int = 1
    enabled: bool = False
    base_url: str = "https://api.runwayml.com/v1"
    poll_interval_sec: float = 3.0
    timeout_sec: float = 600.0
    cache_outputs: bool = True
    updated_at: Optional[datetime] = None


class RunwayAccount(BaseModel):
    """Runway web-account credential entry."""

    id: Optional[int] = None
    label: str = ""
    raw_credential: str = ""
    is_active: bool = True
    workspace_id: Optional[str] = None
    team_id: Optional[str] = None
    concurrency_limit: int = 1
    in_flight: int = 0
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    last_used_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RunwayModel(BaseModel):
    """Admin-editable Runway model registry entry."""

    id: Optional[int] = None
    public_model_id: str
    display_name: str = ""
    kind: Literal["image", "video", "audio", "upscale"] = "image"
    task_type: str
    builder_key: str = ""
    default_options: str = "{}"
    request_mapping: str = "{}"
    capability_schema: str = "{}"
    media_roles: str = "[]"
    supported_modes: str = "[]"
    limits: str = "{}"
    feature_flags: str = "[]"
    cost_feature: str = ""
    source_version: str = ""
    live_available: bool = True
    disabled_reason: str = ""
    last_synced_at: Optional[datetime] = None
    is_enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RunwayTask(BaseModel):
    """Runway task/job state tracked by sub2gen."""

    id: Optional[int] = None
    job_id: str
    upstream_task_id: Optional[str] = None
    account_id: Optional[int] = None
    api_key_id: Optional[int] = None
    public_model_id: str
    prompt: str = ""
    status: str = "processing"
    progress: int = 0
    raw_artifact_urls: Optional[List[str]] = None
    cached_artifact_urls: Optional[List[str]] = None
    request_payload: Optional[str] = None
    response_payload: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class GeminiGenConfig(BaseModel):
    """Global GeminiGen web-session integration configuration."""

    id: int = 1
    enabled: bool = False
    video_enabled: bool = True
    base_url: str = "https://api.geminigen.ai"
    poll_interval_image_sec: float = 3.0
    poll_interval_video_sec: float = 12.0
    timeout_image_sec: float = 600.0
    timeout_video_sec: float = 1800.0
    global_image_concurrency: int = 5
    global_video_concurrency: int = 5
    cache_outputs: bool = True
    updated_at: Optional[datetime] = None


class GeminiGenAccount(BaseModel):
    """GeminiGen Max web-account credential entry."""

    id: Optional[int] = None
    label: str = ""
    raw_cookie: str = ""
    bearer_token: str = ""
    refresh_token: str = ""
    guard_id: str = ""
    turnstile_token: str = ""
    is_active: bool = True
    image_concurrency: int = 5
    video_concurrency: int = 5
    image_in_flight: int = 0
    video_in_flight: int = 0
    image_gen_daily_limit_reset_at: Optional[datetime] = None
    grok_image_daily_limit_reset_at: Optional[datetime] = None
    video_daily_limit_reset_at: Optional[datetime] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    last_used_at: Optional[datetime] = None
    profile_user_id: Optional[int] = None
    profile_uuid: Optional[str] = None
    profile_email: Optional[str] = None
    profile_full_name: Optional[str] = None
    profile_is_active: Optional[bool] = None
    available_credit: Optional[int] = None
    plan_credit: Optional[int] = None
    purchased_credit: Optional[int] = None
    locked_credit: Optional[int] = None
    subscription_credit: Optional[int] = None
    plan_name: Optional[str] = None
    plan_expire_at: Optional[datetime] = None
    active_benefits_json: Optional[str] = None
    is_image_gen_max: Optional[bool] = None
    is_image_premium: Optional[bool] = None
    image_gen_concurrent_streams: Optional[int] = None
    remaining_image_gen_max_daily_images: Optional[int] = None
    remaining_image_gen_free_daily_images: Optional[int] = None
    image_gen_quota_synced_at: Optional[datetime] = None
    is_grok_image_max: Optional[bool] = None
    grok_image_concurrent_streams: Optional[int] = None
    remaining_grok_image_max_daily_images: Optional[int] = None
    remaining_grok_image_free_daily_images: Optional[int] = None
    grok_image_quota_synced_at: Optional[datetime] = None
    is_grok_max: Optional[bool] = None
    grok_max_concurrent_streams: Optional[int] = None
    grok_max_quota_synced_at: Optional[datetime] = None
    remaining_bulk_videos: Optional[int] = None
    remaining_daily_videos: Optional[int] = None
    remaining_grok_max_daily_videos: Optional[int] = None
    remaining_grok_max_daily_720p_videos: Optional[int] = None
    remaining_grok_max_daily_10s_videos: Optional[int] = None
    remaining_grok_max_daily_15s_videos: Optional[int] = None
    quota_synced_at: Optional[datetime] = None
    quota_sync_status: Optional[str] = None
    quota_sync_error: Optional[str] = None
    profile_synced_at: Optional[datetime] = None
    profile_sync_status: Optional[str] = None
    profile_sync_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TokenRefreshConfig(BaseModel):
    """Global protocol ST refresh settings."""

    id: int = 1
    enabled: bool = True
    refresh_interval_minutes: int = Field(default=120, ge=1, le=10080)
    updated_at: Optional[datetime] = None


class GeminiGenTask(BaseModel):
    """GeminiGen queued/upstream job state."""

    id: Optional[int] = None
    job_id: str
    upstream_uuid: Optional[str] = None
    account_id: Optional[int] = None
    api_key_id: Optional[int] = None
    request_log_id: Optional[int] = None
    public_model_id: str
    kind: str = "image"
    endpoint_type: str = "imagen"
    prompt: str = ""
    status: str = "queued"
    progress: int = 0
    raw_artifact_urls: Optional[List[str]] = None
    cached_artifact_urls: Optional[List[str]] = None
    request_payload: Optional[str] = None
    response_payload: Optional[str] = None
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    retry_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# OpenAI Compatible Request Models
class ChatMessage(BaseModel):
    """Chat message"""

    role: str
    content: Union[str, List[dict]]  # string or multimodal array


class ImageConfig(BaseModel):
    """Gemini imageConfig parameters"""

    aspectRatio: Optional[str] = None  # "16:9", "9:16", "1:1", "4:3", "3:4"
    imageSize: Optional[str] = None  # "2k", "4k"

    # 兼容 OpenAI/NewAPI 等上游可能透传的 size/quality 或 snake_case 字段
    model_config = ConfigDict(extra="allow")


class GenerationConfigParam(BaseModel):
    """Gemini generationConfig parameters (for model name resolution)"""

    responseModalities: Optional[List[str]] = None  # ["IMAGE", "TEXT"]
    imageConfig: Optional[ImageConfig] = None

    model_config = ConfigDict(extra="allow")


class GeminiInlineData(BaseModel):
    """Gemini inline binary data."""

    mimeType: str
    data: str


class GeminiFileData(BaseModel):
    """Gemini file reference."""

    fileUri: str
    mimeType: Optional[str] = None


class GeminiPart(BaseModel):
    """Gemini content part."""

    text: Optional[str] = None
    inlineData: Optional[GeminiInlineData] = None
    fileData: Optional[GeminiFileData] = None

    model_config = ConfigDict(extra="allow")


class GeminiContent(BaseModel):
    """Gemini content block."""

    role: Optional[Literal["user", "model"]] = None
    parts: List[GeminiPart]


class GeminiGenerateContentRequest(BaseModel):
    """Gemini official generateContent request."""

    contents: List[GeminiContent]
    generationConfig: Optional[GenerationConfigParam] = None
    systemInstruction: Optional[GeminiContent] = None
    # sub2gen: pin image/video generation to this VideoFX project (must belong to the API key).
    project_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class FlowProjectCreateRequest(BaseModel):
    """Create a VideoFX (Flow) project using a managed API key."""

    # If omitted, the server creates one project for each assigned account.
    account_id: Optional[int] = None
    # If omitted, API key label + current date is used (for example: "default 2026-04-28").
    title: Optional[str] = None
    set_as_current: bool = True


class ChatCompletionRequest(BaseModel):
    """Chat completion request (OpenAI compatible + Gemini extension)"""

    model: str
    messages: Optional[List[ChatMessage]] = None
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # sub2gen specific parameters
    image: Optional[str] = None  # Base64 encoded image (deprecated, use messages)
    video: Optional[str] = None  # Base64 encoded video (deprecated)
    # Gemini extension parameters (from extra_body or top-level)
    generationConfig: Optional[GenerationConfigParam] = None
    contents: Optional[List[Any]] = None  # Gemini native contents
    # sub2gen: pin image/video generation to this VideoFX project (must belong to the API key).
    project_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")  # Allow extra fields like extra_body passthrough


class KeywordTypesConfig(BaseModel):
    singleWord: bool = False
    doubleWord: bool = False
    mixed: bool = True


class CustomPromptConfig(BaseModel):
    enabled: bool = False
    text: str = ""


class MetadataSettingsRequest(BaseModel):
    titleMin: int = 50
    titleMax: int = 80
    keywordMin: int = 32
    keywordMax: int = 50
    descriptionMin: int = 0
    descriptionMax: int = 0
    platforms: List[str] = Field(default_factory=lambda: ["adobe-stock"])
    includeCategory: bool = False
    includeReleases: bool = False
    titleStyle: str = "seo-optimized"
    keywordTypes: KeywordTypesConfig = Field(default_factory=KeywordTypesConfig)
    transparentBackground: bool = False
    language: Literal["en", "fr", "de", "es", "it", "pt", "ja", "pl", "ko"] = "en"
    assetType: Optional[str] = None
    customPrompt: CustomPromptConfig = Field(default_factory=CustomPromptConfig)


class CloneImageItemRequest(BaseModel):
    id: Optional[str] = None
    title: Optional[str] = None
    image_url: Optional[str] = None
    image_base64: Optional[str] = None
    mimeType: Optional[str] = None

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _validate_image_source(self):
        if bool(self.image_url) == bool(self.image_base64):
            raise ValueError("Each image item must include exactly one source: image_url or image_base64")
        return self


class GenerateCloningPromptsRequest(BaseModel):
    images: List[CloneImageItemRequest]
    provider: Optional[str] = None
    model: Optional[str] = None
    fallbackModels: Optional[List[str]] = None

    model_config = ConfigDict(extra="allow")


class GenerateCloningVideoPromptRequest(BaseModel):
    imageClonePrompt: str
    cameraMotion: str
    duration: str
    negativePrompt: Optional[str] = ""
    title: Optional[str] = ""
    image_base64: Optional[str] = None
    mimeType: Optional[str] = None

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _validate_image_pair(self):
        has_base64 = bool((self.image_base64 or "").strip())
        has_mime = bool((self.mimeType or "").strip())
        if has_base64 != has_mime:
            raise ValueError("image_base64 and mimeType must both be provided or both omitted")
        return self


class GenerateMetadataRequest(BaseModel):
    image_url: Optional[str] = None
    image_base64: Optional[str] = None
    metadataSettings: MetadataSettingsRequest
    dnaNoBgWorkflowActive: bool = False
    backend: Optional[Literal["gemini_native", "openai", "third_party_gemini", "cloudflare"]] = None
    model: Optional[str] = None
    fallbackModels: Optional[List[str]] = None

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _validate_image_source(self):
        if bool(self.image_url) == bool(self.image_base64):
            raise ValueError("Request must include exactly one image source: image_url or image_base64")
        return self


class TaskTrackerContributorFetchRequest(BaseModel):
    search_id: str
    order: Optional[str] = "creation"
    content_type: Optional[str] = "all"
    pages: Optional[List[int]] = None
    title_filter: Optional[str] = ""
    generative_ai: Optional[str] = "all"


# Backwards-compatible name for contributor fetch body schema.
TaskTrackerFetchRequest = TaskTrackerContributorFetchRequest


class TaskTrackerKeywordSearchRequest(BaseModel):
    q: str
    order: Optional[str] = "relevance"
    content_type: Optional[str] = "all"
    pages: Optional[List[int]] = None
    generative_ai: Optional[str] = "all"
