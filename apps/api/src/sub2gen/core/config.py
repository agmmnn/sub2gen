"""Configuration management for sub2gen"""
import os
import tomli
from pathlib import Path
from typing import Dict, Any, Optional, List

from .settings import DeploymentSettings, OperationalSettings

DEFAULT_YESCAPTCHA_TASK_TYPE = "RecaptchaV3TaskProxylessM1S9"
DEFAULT_CAPMONSTER_MIN_SCORE = 0.9
YESCAPTCHA_TASK_TYPE_OPTIONS = {
    "RecaptchaV3TaskProxyless": None,
    "RecaptchaV3TaskProxylessM1": None,
    "RecaptchaV3TaskProxylessM1S7": 0.7,
    "RecaptchaV3TaskProxylessM1S9": 0.9,
}


def normalize_yescaptcha_task_type(task_type: Optional[str]) -> str:
    normalized = (task_type or "").strip()
    if normalized in YESCAPTCHA_TASK_TYPE_OPTIONS:
        return normalized
    return DEFAULT_YESCAPTCHA_TASK_TYPE


def get_yescaptcha_min_score(task_type: Optional[str]) -> Optional[float]:
    return YESCAPTCHA_TASK_TYPE_OPTIONS.get(normalize_yescaptcha_task_type(task_type))


def normalize_capmonster_min_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return DEFAULT_CAPMONSTER_MIN_SCORE
    return max(0.1, min(0.9, score))


def _find_repo_root() -> Path:
    explicit_root = (os.environ.get("SUB2GEN_ROOT") or "").strip()
    if explicit_root:
        return Path(explicit_root).expanduser().resolve()

    candidates = [Path.cwd(), *Path(__file__).resolve().parents]
    for candidate in candidates:
        if (candidate / "config" / "setting_example.toml").is_file():
            return candidate
    return Path.cwd()


REPO_ROOT = _find_repo_root()
SUPPORTED_CAPTCHA_METHODS = {
    "extension",
    "browser",
    "personal",
    "remote_browser",
    "yescaptcha",
    "capmonster",
    "ezcaptcha",
    "capsolver",
}


def _env_value(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _env_bool(name: str) -> Optional[bool]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None

    value = raw.strip().lower()
    if value in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "f", "no", "n", "off"}:
        return False

    raise ValueError(
        f"{name} must be a boolean: true/false, 1/0, yes/no, or on/off"
    )


def _coerce_port(value: Any, fallback: int) -> int:
    try:
        port = int(str(value).strip())
    except Exception:
        return fallback
    if 1 <= port <= 65535:
        return port
    return fallback


def _apply_text_override(
    config_dict: Dict[str, Any],
    section: str,
    key: str,
    env_name: str,
) -> None:
    value = _env_value(env_name)
    if not value:
        return
    config_dict.setdefault(section, {})[key] = value


def _apply_bool_override(
    config_dict: Dict[str, Any],
    section: str,
    key: str,
    env_name: str,
) -> None:
    value = _env_bool(env_name)
    if value is None:
        return
    config_dict.setdefault(section, {})[key] = value


def _apply_env_overrides(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Apply deployment-time seed values before first-run DB initialization."""
    _apply_text_override(config_dict, "global", "api_key", "SUB2GEN_API_KEY")
    _apply_text_override(config_dict, "global", "admin_username", "SUB2GEN_ADMIN_USERNAME")
    _apply_text_override(config_dict, "global", "admin_password", "SUB2GEN_ADMIN_PASSWORD")
    _apply_text_override(config_dict, "cache", "base_url", "SUB2GEN_CACHE_BASE_URL")
    _apply_bool_override(config_dict, "debug", "enabled", "SUB2GEN_DEBUG_ENABLED")
    _apply_bool_override(config_dict, "debug", "log_requests", "SUB2GEN_DEBUG_LOG_REQUESTS")
    _apply_bool_override(config_dict, "debug", "log_responses", "SUB2GEN_DEBUG_LOG_RESPONSES")
    _apply_bool_override(config_dict, "debug", "mask_token", "SUB2GEN_DEBUG_MASK_TOKEN")
    _apply_bool_override(config_dict, "debug", "recaptcha_trace", "SUB2GEN_DEBUG_RECAPTCHA_TRACE")
    _apply_bool_override(config_dict, "debug", "recaptcha_console", "SUB2GEN_DEBUG_RECAPTCHA_CONSOLE")

    captcha_method = _env_value("SUB2GEN_CAPTCHA_METHOD").lower()
    if captcha_method:
        if captcha_method not in SUPPORTED_CAPTCHA_METHODS:
            raise ValueError(
                "SUB2GEN_CAPTCHA_METHOD must be one of: "
                + ", ".join(sorted(SUPPORTED_CAPTCHA_METHODS))
            )
        config_dict.setdefault("captcha", {})["captcha_method"] = captcha_method

    return config_dict


def get_runtime_data_dir() -> Path:
    """Return the directory that should hold the SQLite database."""
    volume_mount = _env_value("RAILWAY_VOLUME_MOUNT_PATH")
    if volume_mount:
        return Path(volume_mount).expanduser() / "data"
    return REPO_ROOT / ".runtime" / "data"


def get_runtime_tmp_dir() -> Path:
    """Return the directory that should hold generated/cache files."""
    explicit_tmp = _env_value("SUB2GEN_TMP_DIR")
    if explicit_tmp:
        return Path(explicit_tmp).expanduser()
    volume_mount = _env_value("RAILWAY_VOLUME_MOUNT_PATH")
    if volume_mount:
        return Path(volume_mount).expanduser() / "tmp"
    return REPO_ROOT / ".runtime" / "tmp"


class Config:
    """Application configuration"""

    def __init__(self):
        self._replace_deployment_settings(self._load_config())
        self._admin_username: Optional[str] = None
        self._admin_password: Optional[str] = None

    def _replace_deployment_settings(self, values: Dict[str, Any]) -> None:
        self.deployment_settings = DeploymentSettings.from_mapping(values)
        self.operational_settings = OperationalSettings(self.deployment_settings)
        self._config = self.operational_settings.effective

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from setting.toml, falling back to the example file."""
        config_dir = REPO_ROOT / "config"
        config_path = config_dir / "setting.toml"
        fallback_path = config_dir / "setting_example.toml"

        if config_path.exists() and not config_path.is_file():
            print(
                f"[Config] {config_path} is not a regular file; "
                f"falling back to {fallback_path.name}. Check the Docker mount or local path."
            )
            config_path = fallback_path
        elif not config_path.exists():
            config_path = fallback_path

        if not config_path.is_file():
            raise FileNotFoundError(
                f"Configuration file is missing or unreadable: {config_path}. "
                "Provide config/setting.toml or config/setting_example.toml."
            )

        try:
            with open(config_path, "rb") as f:
                loaded = tomli.load(f)
        except OSError as exc:
            if config_path != fallback_path and fallback_path.is_file():
                print(
                    f"[Config] Unable to read {config_path}; "
                    f"falling back to {fallback_path.name}."
                )
                try:
                    with open(fallback_path, "rb") as f:
                        loaded = tomli.load(f)
                except OSError as fallback_exc:
                    raise FileNotFoundError(
                        f"Configuration files are unreadable: {config_path} and {fallback_path}."
                    ) from fallback_exc
            else:
                raise FileNotFoundError(
                    f"Configuration file is unreadable: {config_path}."
                ) from exc
        return _apply_env_overrides(loaded)

    def reload_config(self):
        """Reload configuration from file"""
        self._replace_deployment_settings(self._load_config())

    def get_raw_config(self) -> Dict[str, Any]:
        """Get raw configuration dictionary"""
        return self._config

    def get_deployment_config(self):
        """Return the immutable TOML/environment deployment seed."""
        return self.deployment_settings.values

    def get_operational_overrides(self) -> Dict[str, Any]:
        """Return database/runtime values that differ from the deployment seed."""
        return self.operational_settings.overrides_snapshot()

    @property
    def admin_username(self) -> str:
        override = _env_value("SUB2GEN_ADMIN_USERNAME")
        if override:
            return override
        # If admin_username is set from database, use it; otherwise fall back to config file
        if self._admin_username is not None:
            return self._admin_username
        return self._config["global"]["admin_username"]

    @admin_username.setter
    def admin_username(self, value: str):
        self._admin_username = value
        self._config["global"]["admin_username"] = value

    def set_admin_username_from_db(self, username: str):
        """Set admin username from database"""
        self._admin_username = username
        self.operational_settings.set("global", "admin_username", username)

    # sub2gen specific properties
    @property
    def flow_labs_base_url(self) -> str:
        """Google Labs base URL for project management"""
        return self._config["flow"]["labs_base_url"]

    @property
    def flow_api_base_url(self) -> str:
        """Google AI Sandbox API base URL for generation"""
        return self._config["flow"]["api_base_url"]

    @property
    def flow_timeout(self) -> int:
        timeout = self._config.get("flow", {}).get("timeout", 120)
        try:
            return max(5, int(timeout))
        except Exception:
            return 120

    @property
    def flow_max_retries(self) -> int:
        retries = self._config.get("flow", {}).get("max_retries", 3)
        try:
            normalized = max(1, min(20, int(retries)))
            if str(self._config.get("captcha", {}).get("captcha_method", "")).strip().lower() == "browser":
                normalized = max(normalized, self.browser_captcha_generation_retries)
            return normalized
        except Exception:
            return 3

    def set_flow_max_retries(self, retries: int):
        """Set flow max retries"""
        if "flow" not in self._config:
            self._config["flow"] = {}
        try:
            normalized = max(1, int(retries))
        except Exception:
            normalized = 3
        self._config["flow"]["max_retries"] = normalized

    @property
    def flow_image_request_timeout(self) -> int:
        """图片生成单次 HTTP 请求超时(秒)。"""
        default_timeout = min(self.flow_timeout, 40)
        timeout = self._config.get("flow", {}).get(
            "image_request_timeout",
            default_timeout
        )
        try:
            return max(5, int(timeout))
        except Exception:
            return self.flow_timeout

    @property
    def flow_image_timeout_retry_count(self) -> int:
        """图片生成遇到网络超时时的快速重试次数。"""
        retry_count = self._config.get("flow", {}).get("image_timeout_retry_count", 1)
        try:
            return max(0, min(3, int(retry_count)))
        except Exception:
            return 1

    @property
    def flow_image_timeout_retry_delay(self) -> float:
        """图片生成网络超时重试前等待秒数。"""
        delay = self._config.get("flow", {}).get("image_timeout_retry_delay", 0.8)
        try:
            return max(0.0, min(5.0, float(delay)))
        except Exception:
            return 0.8

    @property
    def flow_image_timeout_use_media_proxy_fallback(self) -> bool:
        """网络超时时是否切换媒体代理重试。"""
        return bool(
            self._config.get("flow", {}).get(
                "image_timeout_use_media_proxy_fallback",
                True
            )
        )

    @property
    def flow_image_prefer_media_proxy(self) -> bool:
        """图片生成是否优先走媒体代理链路。"""
        return bool(
            self._config.get("flow", {}).get(
                "image_prefer_media_proxy",
                False
            )
        )

    @property
    def flow_image_slot_wait_timeout(self) -> float:
        """图片硬并发槽位等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("image_slot_wait_timeout", 120)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 120.0

    @property
    def flow_image_launch_soft_limit(self) -> int:
        """图片生成前置发车软并发上限(0 表示关闭软整形，仅使用硬并发)。"""
        value = self._config.get("flow", {}).get("image_launch_soft_limit", 0)
        try:
            return max(0, min(200, int(value)))
        except Exception:
            return 0

    @property
    def flow_image_launch_wait_timeout(self) -> float:
        """图片前置发车软并发等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("image_launch_wait_timeout", 180)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 180.0

    @property
    def flow_image_launch_stagger_ms(self) -> int:
        """图片请求前置发车间隔(毫秒)，用于平滑同批突发。"""
        value = self._config.get("flow", {}).get("image_launch_stagger_ms", 0)
        try:
            return max(0, min(5000, int(value)))
        except Exception:
            return 0

    @property
    def flow_video_slot_wait_timeout(self) -> float:
        """视频硬并发槽位等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("video_slot_wait_timeout", 120)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 120.0

    @property
    def flow_video_launch_soft_limit(self) -> int:
        """视频生成前置发车软并发上限(0 表示关闭软整形，仅使用硬并发)。"""
        value = self._config.get("flow", {}).get("video_launch_soft_limit", 0)
        try:
            return max(0, min(200, int(value)))
        except Exception:
            return 0

    @property
    def flow_video_launch_wait_timeout(self) -> float:
        """视频前置发车软并发等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("video_launch_wait_timeout", 180)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 180.0

    @property
    def flow_video_launch_stagger_ms(self) -> int:
        """视频请求前置发车间隔(毫秒)，用于平滑同批突发。"""
        value = self._config.get("flow", {}).get("video_launch_stagger_ms", 0)
        try:
            return max(0, min(5000, int(value)))
        except Exception:
            return 0

    @property
    def poll_interval(self) -> float:
        return self._config["flow"]["poll_interval"]

    @property
    def max_poll_attempts(self) -> int:
        return self._config["flow"]["max_poll_attempts"]

    @property
    def server_host(self) -> str:
        return self._config["server"]["host"]

    @property
    def server_port(self) -> int:
        return _coerce_port(os.environ.get("PORT"), self._config["server"]["port"])

    @property
    def debug_enabled(self) -> bool:
        override = _env_bool("SUB2GEN_DEBUG_ENABLED")
        if override is not None:
            return override
        return bool(self._config.get("debug", {}).get("enabled", False))

    @property
    def debug_log_requests(self) -> bool:
        override = _env_bool("SUB2GEN_DEBUG_LOG_REQUESTS")
        if override is not None:
            return override
        return bool(self._config.get("debug", {}).get("log_requests", True))

    @property
    def debug_log_responses(self) -> bool:
        override = _env_bool("SUB2GEN_DEBUG_LOG_RESPONSES")
        if override is not None:
            return override
        return bool(self._config.get("debug", {}).get("log_responses", True))

    @property
    def debug_mask_token(self) -> bool:
        override = _env_bool("SUB2GEN_DEBUG_MASK_TOKEN")
        if override is not None:
            return override
        return bool(self._config.get("debug", {}).get("mask_token", True))

    @property
    def debug_recaptcha_trace(self) -> bool:
        override = _env_bool("SUB2GEN_DEBUG_RECAPTCHA_TRACE")
        if override is not None:
            return override
        return bool(self._config.get("debug", {}).get("recaptcha_trace", False))

    @property
    def debug_recaptcha_console(self) -> bool:
        override = _env_bool("SUB2GEN_DEBUG_RECAPTCHA_CONSOLE")
        if override is not None:
            return override
        return bool(self._config.get("debug", {}).get("recaptcha_console", False))

    # Mutable properties for runtime updates
    @property
    def api_key(self) -> str:
        override = _env_value("SUB2GEN_API_KEY")
        if override:
            return override
        return self._config["global"]["api_key"]

    @api_key.setter
    def api_key(self, value: str):
        self._config["global"]["api_key"] = value

    @property
    def admin_password(self) -> str:
        override = _env_value("SUB2GEN_ADMIN_PASSWORD")
        if override:
            return override
        # If admin_password is set from database, use it; otherwise fall back to config file
        if self._admin_password is not None:
            return self._admin_password
        return self._config["global"]["admin_password"]

    @admin_password.setter
    def admin_password(self, value: str):
        self._admin_password = value
        self._config["global"]["admin_password"] = value

    def set_admin_password_from_db(self, password: str):
        """Set admin password from database"""
        self._admin_password = password
        self.operational_settings.set("global", "admin_password", password)

    def set_debug_enabled(self, enabled: bool):
        """Set debug mode enabled/disabled"""
        if "debug" not in self._config:
            self._config["debug"] = {}
        self._config["debug"]["enabled"] = enabled

    @property
    def image_timeout(self) -> int:
        """Get image generation timeout in seconds"""
        return self._config.get("generation", {}).get("image_timeout", 300)

    def set_image_timeout(self, timeout: int):
        """Set image generation timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["image_timeout"] = timeout

    @property
    def video_timeout(self) -> int:
        """Get video generation timeout in seconds"""
        return self._config.get("generation", {}).get("video_timeout", 1500)

    def set_video_timeout(self, timeout: int):
        """Set video generation timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["video_timeout"] = timeout

    @property
    def polling_mode_enabled(self) -> bool:
        """Get polling mode enabled status."""
        return self.call_logic_mode == "polling"

    @property
    def call_logic_mode(self) -> str:
        """Get call logic mode (default or polling)."""
        call_logic = self._config.get("call_logic", {})
        mode = call_logic.get("call_mode")
        if mode in ("default", "polling"):
            return mode
        if call_logic.get("polling_mode_enabled", False):
            return "polling"
        return "default"

    def set_polling_mode_enabled(self, enabled: bool):
        """Set polling mode enabled/disabled."""
        self.set_call_logic_mode("polling" if enabled else "default")

    def set_call_logic_mode(self, mode: str):
        """Set call logic mode (default or polling)."""
        normalized = "polling" if mode == "polling" else "default"
        if "call_logic" not in self._config:
            self._config["call_logic"] = {}
        self._config["call_logic"]["call_mode"] = normalized
        self._config["call_logic"]["polling_mode_enabled"] = normalized == "polling"

    @property
    def upsample_timeout(self) -> int:
        """Get upsample (4K/2K) timeout in seconds"""
        return self._config.get("generation", {}).get("upsample_timeout", 300)

    def set_upsample_timeout(self, timeout: int):
        """Set upsample (4K/2K) timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["upsample_timeout"] = timeout

    @property
    def extension_generation_enabled(self) -> bool:
        return bool(self._config.get("generation_routing", {}).get("extension_generation_enabled", False))

    def set_extension_generation_enabled(self, enabled: bool):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["extension_generation_enabled"] = bool(enabled)

    @property
    def extension_generation_fallback_mode(self) -> str:
        mode = str(
            self._config.get("generation_routing", {}).get(
                "extension_generation_fallback_mode",
                "local_http_on_recaptcha",
            )
            or ""
        ).strip().lower()
        return mode if mode in {"none", "local_http_on_recaptcha"} else "local_http_on_recaptcha"

    def set_extension_generation_fallback_mode(self, mode: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        normalized = str(mode or "").strip().lower()
        if normalized not in {"none", "local_http_on_recaptcha"}:
            normalized = "local_http_on_recaptcha"
        self._config["generation_routing"]["extension_generation_fallback_mode"] = normalized

    @staticmethod
    def _routing_set(value: Any, defaults: tuple[str, ...]) -> frozenset[str]:
        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set, frozenset)):
            items = value
        else:
            items = defaults
        normalized = frozenset(str(item).strip() for item in items if str(item).strip())
        return normalized or frozenset(defaults)

    @property
    def allowed_generation_providers(self) -> frozenset[str]:
        return self._routing_set(
            self._config.get("generation_routing", {}).get("allowed_providers"),
            ("google-flow", "chatgpt-web", "chatgpt-codex"),
        )

    @property
    def allowed_generation_billing_pools(self) -> frozenset[str]:
        return self._routing_set(
            self._config.get("generation_routing", {}).get("allowed_billing_pools"),
            ("google-flow:subscription", "chatgpt:web-subscription", "chatgpt:codex-subscription"),
        )

    @property
    def allowed_generation_credential_kinds(self) -> frozenset[str]:
        return self._routing_set(
            self._config.get("generation_routing", {}).get("allowed_credential_kinds"),
            ("session_token", "browser_session", "oauth"),
        )

    @property
    def allow_cross_billing_fallback(self) -> bool:
        return bool(self._config.get("generation_routing", {}).get("allow_cross_billing_fallback", False))

    @property
    def sub2gen_gemini_api_keys(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_gemini_api_keys", "") or "")

    def set_sub2gen_gemini_api_keys(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_gemini_api_keys"] = str(value or "")

    @property
    def sub2gen_openai_api_keys(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_openai_api_keys", "") or "")

    def set_sub2gen_openai_api_keys(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_openai_api_keys"] = str(value or "")

    @property
    def sub2gen_openrouter_api_keys(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_openrouter_api_keys", "") or "")

    def set_sub2gen_openrouter_api_keys(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_openrouter_api_keys"] = str(value or "")

    @property
    def sub2gen_third_party_gemini_api_keys(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_third_party_gemini_api_keys", "") or "")

    def set_sub2gen_third_party_gemini_api_keys(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_third_party_gemini_api_keys"] = str(value or "")

    @property
    def sub2gen_third_party_gemini_base_url(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_third_party_gemini_base_url", "") or "")

    def set_sub2gen_third_party_gemini_base_url(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_third_party_gemini_base_url"] = str(value or "")

    @property
    def cloudflare_account_id(self) -> str:
        return str(self._config.get("generation_routing", {}).get("cloudflare_account_id", "") or "")

    def set_cloudflare_account_id(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["cloudflare_account_id"] = str(value or "")

    @property
    def cloudflare_api_token(self) -> str:
        return str(self._config.get("generation_routing", {}).get("cloudflare_api_token", "") or "")

    def set_cloudflare_api_token(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["cloudflare_api_token"] = str(value or "")

    @property
    def sub2gen_csvgen_cookie(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_csvgen_cookie", "") or "")

    def set_sub2gen_csvgen_cookie(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_csvgen_cookie"] = str(value or "")

    @property
    def sub2gen_csvgen_api_keys(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_csvgen_api_keys", "") or "")

    def set_sub2gen_csvgen_api_keys(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_csvgen_api_keys"] = str(value or "")

    @property
    def sub2gen_cloning_model(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_model", "gemini-2.5-flash") or "gemini-2.5-flash")

    def set_sub2gen_cloning_model(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_model"] = str(value or "gemini-2.5-flash")

    @property
    def cliproxy_base_url(self) -> str:
        """Private CLIProxy inference/management origin (environment only)."""
        return _env_value("SUB2GEN_CLIPROXY_BASE_URL").rstrip("/")

    @property
    def cliproxy_public_url(self) -> str:
        """Public CLIProxy origin used for OAuth callbacks and break-glass UI."""
        return _env_value("SUB2GEN_CLIPROXY_PUBLIC_URL").rstrip("/")

    @property
    def cliproxy_api_key(self) -> str:
        """CLIProxy inference key. This value is never database-backed."""
        return _env_value("SUB2GEN_CLIPROXY_API_KEY")

    @property
    def cliproxy_management_key(self) -> str:
        """CLIProxy management key. This value is never database-backed."""
        return _env_value("SUB2GEN_CLIPROXY_MANAGEMENT_KEY")

    @property
    def cliproxy_version(self) -> str:
        return _env_value("SUB2GEN_CLIPROXY_VERSION") or "v7.2.120"

    @property
    def sub2gen_cloning_backend(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_backend", "gemini_native") or "gemini_native")

    def set_sub2gen_cloning_backend(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_backend"] = str(value or "gemini_native")

    @property
    def sub2gen_cloning_provider_order(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_provider_order", "") or "")

    def set_sub2gen_cloning_provider_order(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_provider_order"] = str(value or "")

    @property
    def sub2gen_cloning_enabled_providers(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_enabled_providers", "") or "")

    def set_sub2gen_cloning_enabled_providers(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_enabled_providers"] = str(value or "")

    @property
    def sub2gen_cloning_provider_retry_count(self) -> int:
        value = self._config.get("generation_routing", {}).get("sub2gen_cloning_provider_retry_count", 1)
        try:
            return max(0, min(5, int(value)))
        except Exception:
            return 1

    def set_sub2gen_cloning_provider_retry_count(self, value: int):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        try:
            normalized = max(0, min(5, int(value)))
        except Exception:
            normalized = 1
        self._config["generation_routing"]["sub2gen_cloning_provider_retry_count"] = normalized

    @property
    def sub2gen_cloning_gemini_api_keys(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_gemini_api_keys", "") or "")

    def set_sub2gen_cloning_gemini_api_keys(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_gemini_api_keys"] = str(value or "")

    @property
    def sub2gen_cloning_openai_api_keys(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_openai_api_keys", "") or "")

    def set_sub2gen_cloning_openai_api_keys(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_openai_api_keys"] = str(value or "")

    @property
    def sub2gen_cloning_openrouter_api_keys(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_openrouter_api_keys", "") or "")

    def set_sub2gen_cloning_openrouter_api_keys(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_openrouter_api_keys"] = str(value or "")

    @property
    def sub2gen_cloning_third_party_gemini_api_keys(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_third_party_gemini_api_keys", "") or "")

    def set_sub2gen_cloning_third_party_gemini_api_keys(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_third_party_gemini_api_keys"] = str(value or "")

    @property
    def sub2gen_cloning_third_party_gemini_base_url(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_third_party_gemini_base_url", "") or "")

    def set_sub2gen_cloning_third_party_gemini_base_url(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_third_party_gemini_base_url"] = str(value or "")

    @property
    def sub2gen_cloning_cloudflare_account_id(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_cloudflare_account_id", "") or "")

    def set_sub2gen_cloning_cloudflare_account_id(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_cloudflare_account_id"] = str(value or "")

    @property
    def sub2gen_cloning_cloudflare_api_token(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_cloning_cloudflare_api_token", "") or "")

    def set_sub2gen_cloning_cloudflare_api_token(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_cloning_cloudflare_api_token"] = str(value or "")

    @property
    def sub2gen_metadata_backend(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_metadata_backend", "gemini_native") or "gemini_native")

    def set_sub2gen_metadata_backend(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_metadata_backend"] = str(value or "gemini_native")

    @property
    def sub2gen_metadata_provider_order(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_metadata_provider_order", "") or "")

    def set_sub2gen_metadata_provider_order(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_metadata_provider_order"] = str(value or "")

    @property
    def sub2gen_metadata_enabled_providers(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_metadata_enabled_providers", "") or "")

    def set_sub2gen_metadata_enabled_providers(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_metadata_enabled_providers"] = str(value or "")

    @property
    def sub2gen_metadata_provider_retry_count(self) -> int:
        value = self._config.get("generation_routing", {}).get("sub2gen_metadata_provider_retry_count", 1)
        try:
            return max(0, min(5, int(value)))
        except Exception:
            return 1

    def set_sub2gen_metadata_provider_retry_count(self, value: int):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        try:
            normalized = max(0, min(5, int(value)))
        except Exception:
            normalized = 1
        self._config["generation_routing"]["sub2gen_metadata_provider_retry_count"] = normalized

    @property
    def sub2gen_metadata_model(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_metadata_model", "gemini-2.5-flash") or "gemini-2.5-flash")

    def set_sub2gen_metadata_model(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_metadata_model"] = str(value or "gemini-2.5-flash")

    @property
    def sub2gen_metadata_enabled_models(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_metadata_enabled_models", "") or "")

    def set_sub2gen_metadata_enabled_models(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_metadata_enabled_models"] = str(value or "")

    @property
    def sub2gen_metadata_primary_model(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_metadata_primary_model", "") or "")

    def set_sub2gen_metadata_primary_model(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_metadata_primary_model"] = str(value or "")

    @property
    def sub2gen_metadata_fallback_models(self) -> str:
        return str(self._config.get("generation_routing", {}).get("sub2gen_metadata_fallback_models", "") or "")

    def set_sub2gen_metadata_fallback_models(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["sub2gen_metadata_fallback_models"] = str(value or "")

    @property
    def metadata_system_prompt(self) -> str:
        return str(self._config.get("generation_routing", {}).get("metadata_system_prompt", "") or "")

    def set_metadata_system_prompt(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["metadata_system_prompt"] = str(value or "")

    @property
    def cloning_image_system_prompt(self) -> str:
        return str(self._config.get("generation_routing", {}).get("cloning_image_system_prompt", "") or "")

    def set_cloning_image_system_prompt(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["cloning_image_system_prompt"] = str(value or "")

    @property
    def cloning_video_system_prompt(self) -> str:
        return str(self._config.get("generation_routing", {}).get("cloning_video_system_prompt", "") or "")

    def set_cloning_video_system_prompt(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["cloning_video_system_prompt"] = str(value or "")

    @property
    def extension_generation_large_upload_enabled(self) -> bool:
        """POST large generation responses to sub2gen HTTP instead of embedding in a worker frame."""
        return bool(
            self._config.get("generation_routing", {}).get("extension_generation_large_upload_enabled", True)
        )

    @property
    def extension_generation_upload_threshold_bytes(self) -> int:
        v = int(self._config.get("generation_routing", {}).get("extension_generation_upload_threshold_bytes", 524288) or 524288)
        return max(0, min(v, 256 * 1024 * 1024))

    @property
    def extension_generation_upload_max_bytes(self) -> int:
        v = int(self._config.get("generation_routing", {}).get("extension_generation_upload_max_bytes", 67108864) or 67108864)
        return max(1024 * 1024, min(v, 256 * 1024 * 1024))

    @property
    def extension_generation_upload_ttl_seconds(self) -> int:
        v = int(self._config.get("generation_routing", {}).get("extension_generation_upload_ttl_seconds", 600) or 600)
        return max(30, min(v, 3600))

    @property
    def extension_generation_upload_force_upsample_image(self) -> bool:
        return bool(
            self._config.get("generation_routing", {}).get(
                "extension_generation_upload_force_upsample_image", True
            )
        )

    @property
    def task_tracker_device_id(self) -> str:
        return str(self._config.get("generation_routing", {}).get("task_tracker_device_id", "") or "")

    def set_task_tracker_device_id(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["task_tracker_device_id"] = str(value or "")

    @property
    def task_tracker_device_name(self) -> str:
        return str(self._config.get("generation_routing", {}).get("task_tracker_device_name", "") or "")

    def set_task_tracker_device_name(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["task_tracker_device_name"] = str(value or "")

    @property
    def task_tracker_cookies(self) -> str:
        return str(self._config.get("generation_routing", {}).get("task_tracker_cookies", "") or "")

    def set_task_tracker_cookies(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["task_tracker_cookies"] = str(value or "")

    @property
    def task_tracker_device_token(self) -> str:
        return str(self._config.get("generation_routing", {}).get("task_tracker_device_token", "") or "")

    def set_task_tracker_device_token(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["task_tracker_device_token"] = str(value or "")

    @property
    def task_tracker_turnstile_token(self) -> str:
        return str(self._config.get("generation_routing", {}).get("task_tracker_turnstile_token", "") or "")

    def set_task_tracker_turnstile_token(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["task_tracker_turnstile_token"] = str(value or "")

    @property
    def task_tracker_tls_profile(self) -> str:
        return str(self._config.get("generation_routing", {}).get("task_tracker_tls_profile", "") or "")

    def set_task_tracker_tls_profile(self, value: str):
        if "generation_routing" not in self._config:
            self._config["generation_routing"] = {}
        self._config["generation_routing"]["task_tracker_tls_profile"] = str(value or "")

    # Cache configuration
    @property
    def cache_enabled(self) -> bool:
        """Get cache enabled status"""
        return self._config.get("cache", {}).get("enabled", False)

    def set_cache_enabled(self, enabled: bool):
        """Set cache enabled status"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["enabled"] = enabled

    @property
    def cache_timeout(self) -> int:
        """Get cache timeout in seconds"""
        return self._config.get("cache", {}).get("timeout", 7200)

    def set_cache_timeout(self, timeout: int):
        """Set cache timeout in seconds"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["timeout"] = timeout

    @property
    def cache_base_url(self) -> str:
        """Get cache base URL"""
        override = _env_value("SUB2GEN_CACHE_BASE_URL")
        if override:
            return override.rstrip("/")
        return self._config.get("cache", {}).get("base_url", "")

    def set_cache_base_url(self, base_url: str):
        """Set cache base URL"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["base_url"] = base_url

    @property
    def cache_provider(self) -> str:
        value = str(self._config.get("cache", {}).get("provider", "local") or "local").lower()
        return value if value in {"local", "digitalocean"} else "local"

    def set_cache_provider(self, provider: str):
        value = str(provider or "local").lower()
        if value not in {"local", "digitalocean"}:
            raise ValueError("cache provider must be local or digitalocean")
        self._config.setdefault("cache", {})["provider"] = value

    @property
    def cache_delivery_mode(self) -> str:
        value = str(self._config.get("cache", {}).get("delivery_mode", "proxy") or "proxy").lower()
        return value if value in {"proxy", "cdn"} else "proxy"

    def set_cache_delivery_mode(self, mode: str):
        value = str(mode or "proxy").lower()
        if value not in {"proxy", "cdn"}:
            raise ValueError("cache delivery mode must be proxy or cdn")
        self._config.setdefault("cache", {})["delivery_mode"] = value

    # Captcha configuration
    @property
    def captcha_method(self) -> str:
        """Get captcha method"""
        return self._config.get("captcha", {}).get("captcha_method", "yescaptcha")

    def set_captcha_method(self, method: str):
        """Set captcha method"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["captcha_method"] = method

    @property
    def browser_launch_background(self) -> bool:
        """有头浏览器打码是否默认后台启动，避免抢占前台窗口。"""
        return self._config.get("captcha", {}).get("browser_launch_background", True)

    def set_browser_launch_background(self, enabled: bool):
        """设置有头浏览器打码是否后台启动。"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["browser_launch_background"] = bool(enabled)

    @property
    def browser_captcha_page_url(self) -> str:
        """browser 模式 Playwright 打码时打开的首页 URL（默认轻量 auth/providers，可改为 Flow 等真实页面）。"""
        default = "https://labs.google/fx/api/auth/providers"
        value = self._config.get("captcha", {}).get("browser_captcha_page_url", default)
        value = (value or "").strip()
        return value if value else default

    def set_browser_captcha_page_url(self, page_url: str):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["browser_captcha_page_url"] = (page_url or "").strip()

    @property
    def browser_fallback_to_remote_browser(self) -> bool:
        """browser 模式失败时是否自动回退到 remote_browser。"""
        return bool(
            self._config.get("captcha", {}).get(
                "browser_fallback_to_remote_browser",
                True,
            )
        )

    def set_browser_fallback_to_remote_browser(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["browser_fallback_to_remote_browser"] = bool(enabled)

    @property
    def browser_recaptcha_settle_seconds(self) -> float:
        """有头打码在 reload/clr 就绪后的额外等待秒数。"""
        value = self._config.get("captcha", {}).get("browser_recaptcha_settle_seconds", 3.0)
        try:
            return max(0.0, min(10.0, float(value)))
        except Exception:
            return 3.0

    @property
    def browser_idle_ttl_seconds(self) -> int:
        value = self._config.get("captcha", {}).get("browser_idle_ttl_seconds", 600)
        try:
            return max(60, int(value))
        except Exception:
            return 600

    @property
    def browser_count(self) -> int:
        value = self._config.get("captcha", {}).get("browser_count", 1)
        try:
            return max(1, min(20, int(value)))
        except Exception:
            return 1

    def set_browser_count(self, value: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        try:
            normalized = max(1, min(20, int(value)))
        except Exception:
            normalized = 1
        self._config["captcha"]["browser_count"] = normalized

    @property
    def browser_personal_fresh_restart_every_n_solves(self) -> int:
        value = self._config.get("captcha", {}).get(
            "browser_personal_fresh_restart_every_n_solves",
            10,
        )
        try:
            return max(0, int(value))
        except Exception:
            return 10

    def set_browser_personal_fresh_restart_every_n_solves(self, value: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        try:
            normalized = max(0, int(value))
        except Exception:
            normalized = 10
        self._config["captcha"]["browser_personal_fresh_restart_every_n_solves"] = normalized

    @property
    def personal_max_resident_tabs(self) -> int:
        """内置浏览器打码单实例共享标签页上限"""
        value = self._config.get("captcha", {}).get("personal_max_resident_tabs", 5)
        try:
            return max(1, min(50, int(value)))  # 限制在1-50之间
        except Exception:
            return 5

    @property
    def browser_captcha_max_retries(self) -> int:
        """Maximum attempts made by browser mode for one captcha solve."""
        value = self._config.get("captcha", {}).get("browser_captcha_max_retries", 5)
        try:
            return max(1, min(20, int(value)))
        except Exception:
            return 5

    @property
    def browser_captcha_generation_retries(self) -> int:
        """Generation attempts allowed after upstream reCAPTCHA rejection."""
        value = self._config.get("captcha", {}).get("browser_captcha_generation_retries", 6)
        try:
            return max(1, min(20, int(value)))
        except Exception:
            return 6

    @property
    def personal_project_pool_size(self) -> int:
        """单个 Token 默认维护的项目池数量，仅影响项目轮换。"""
        value = self._config.get("captcha", {}).get("personal_project_pool_size", 4)
        try:
            return max(1, min(50, int(value)))
        except Exception:
            return 4

    @property
    def personal_idle_tab_ttl_seconds(self) -> int:
        """内置浏览器打码标签页空闲超时(秒)"""
        value = self._config.get("captcha", {}).get("personal_idle_tab_ttl_seconds", 600)
        try:
            return max(60, int(value))
        except Exception:
            return 600

    @property
    def personal_headless(self) -> bool:
        """Whether the built-in Personal browser should run headless."""
        env_value = os.getenv("PERSONAL_BROWSER_HEADLESS")
        if env_value is not None:
            return str(env_value).strip().lower() in {"1", "true", "yes", "on"}
        return bool(self._config.get("captcha", {}).get("personal_headless", False))

    def set_personal_max_resident_tabs(self, value: int):
        """设置内置浏览器打码单实例共享标签页上限"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_max_resident_tabs"] = max(1, min(50, int(value)))

    def set_personal_project_pool_size(self, value: int):
        """设置单个 Token 默认维护的项目池数量，仅影响项目轮换"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_project_pool_size"] = max(1, min(50, int(value)))

    def set_personal_idle_tab_ttl_seconds(self, value: int):
        """设置内置浏览器打码标签页空闲超时(秒)"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_idle_tab_ttl_seconds"] = max(60, int(value))

    @property
    def yescaptcha_api_key(self) -> str:
        """Get YesCaptcha API key"""
        return self._config.get("captcha", {}).get("yescaptcha_api_key", "")

    def set_yescaptcha_api_key(self, api_key: str):
        """Set YesCaptcha API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["yescaptcha_api_key"] = api_key

    @property
    def yescaptcha_base_url(self) -> str:
        """Get YesCaptcha base URL"""
        return self._config.get("captcha", {}).get("yescaptcha_base_url", "https://api.yescaptcha.com")

    def set_yescaptcha_base_url(self, base_url: str):
        """Set YesCaptcha base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["yescaptcha_base_url"] = base_url

    @property
    def yescaptcha_task_type(self) -> str:
        """Get YesCaptcha reCAPTCHA V3 task type"""
        return normalize_yescaptcha_task_type(
            self._config.get("captcha", {}).get(
                "yescaptcha_task_type",
                DEFAULT_YESCAPTCHA_TASK_TYPE,
            )
        )

    def set_yescaptcha_task_type(self, task_type: str):
        """Set YesCaptcha reCAPTCHA V3 task type"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["yescaptcha_task_type"] = normalize_yescaptcha_task_type(task_type)

    @property
    def capmonster_api_key(self) -> str:
        """Get CapMonster API key"""
        return self._config.get("captcha", {}).get("capmonster_api_key", "")

    def set_capmonster_api_key(self, api_key: str):
        """Set CapMonster API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capmonster_api_key"] = api_key

    @property
    def capmonster_base_url(self) -> str:
        """Get CapMonster base URL"""
        return self._config.get("captcha", {}).get("capmonster_base_url", "https://api.capmonster.cloud")

    def set_capmonster_base_url(self, base_url: str):
        """Set CapMonster base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capmonster_base_url"] = base_url

    @property
    def capmonster_min_score(self) -> float:
        """Get the required CapMonster reCAPTCHA v3 Enterprise score."""
        return normalize_capmonster_min_score(
            self._config.get("captcha", {}).get(
                "capmonster_min_score",
                DEFAULT_CAPMONSTER_MIN_SCORE,
            )
        )

    def set_capmonster_min_score(self, value: float):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capmonster_min_score"] = normalize_capmonster_min_score(value)

    @property
    def ezcaptcha_api_key(self) -> str:
        """Get EzCaptcha API key"""
        return self._config.get("captcha", {}).get("ezcaptcha_api_key", "")

    def set_ezcaptcha_api_key(self, api_key: str):
        """Set EzCaptcha API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["ezcaptcha_api_key"] = api_key

    @property
    def ezcaptcha_base_url(self) -> str:
        """Get EzCaptcha base URL"""
        return self._config.get("captcha", {}).get("ezcaptcha_base_url", "https://api.ez-captcha.com")

    def set_ezcaptcha_base_url(self, base_url: str):
        """Set EzCaptcha base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["ezcaptcha_base_url"] = base_url

    @property
    def capsolver_api_key(self) -> str:
        """Get CapSolver API key"""
        return self._config.get("captcha", {}).get("capsolver_api_key", "")

    def set_capsolver_api_key(self, api_key: str):
        """Set CapSolver API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capsolver_api_key"] = api_key

    @property
    def capsolver_base_url(self) -> str:
        """Get CapSolver base URL"""
        return self._config.get("captcha", {}).get("capsolver_base_url", "https://api.capsolver.com")

    def set_capsolver_base_url(self, base_url: str):
        """Set CapSolver base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capsolver_base_url"] = base_url

    @property
    def remote_browser_base_url(self) -> str:
        """Get remote browser captcha service base URL"""
        return self._config.get("captcha", {}).get("remote_browser_base_url", "")

    def set_remote_browser_base_url(self, base_url: str):
        """Set remote browser captcha service base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["remote_browser_base_url"] = (base_url or "").strip()

    @property
    def remote_browser_api_key(self) -> str:
        """Get remote browser captcha service API key"""
        return self._config.get("captcha", {}).get("remote_browser_api_key", "")

    def set_remote_browser_api_key(self, api_key: str):
        """Set remote browser captcha service API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["remote_browser_api_key"] = (api_key or "").strip()

    @property
    def remote_browser_timeout(self) -> int:
        """Get remote browser captcha request timeout (seconds)"""
        timeout = self._config.get("captcha", {}).get("remote_browser_timeout", 60)
        try:
            return max(5, int(timeout))
        except Exception:
            return 60

    def set_remote_browser_timeout(self, timeout: int):
        """Set remote browser captcha request timeout (seconds)"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        try:
            normalized = max(5, int(timeout))
        except Exception:
            normalized = 60
        self._config["captcha"]["remote_browser_timeout"] = normalized

    @property
    def session_refresh_enabled(self) -> bool:
        return bool(self._config.get("captcha", {}).get("session_refresh_enabled", True))

    def set_session_refresh_enabled(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_enabled"] = bool(enabled)

    @property
    def session_refresh_browser_first(self) -> bool:
        return bool(self._config.get("captcha", {}).get("session_refresh_browser_first", True))

    def set_session_refresh_browser_first(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_browser_first"] = bool(enabled)

    @property
    def session_refresh_inject_st_cookie(self) -> bool:
        return bool(self._config.get("captcha", {}).get("session_refresh_inject_st_cookie", True))

    def set_session_refresh_inject_st_cookie(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_inject_st_cookie"] = bool(enabled)

    @property
    def session_refresh_warmup_urls(self) -> List[str]:
        raw = self._config.get("captcha", {}).get(
            "session_refresh_warmup_urls",
            "https://labs.google/fx/tools/flow,https://labs.google/fx",
        )
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw if str(item).strip()]
        else:
            values = [item.strip() for item in str(raw or "").split(",") if item.strip()]
        return values or ["https://labs.google/fx/tools/flow", "https://labs.google/fx"]

    def set_session_refresh_warmup_urls(self, urls: List[str] | str):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        if isinstance(urls, list):
            cleaned = [str(item).strip() for item in urls if str(item).strip()]
        else:
            cleaned = [item.strip() for item in str(urls or "").split(",") if item.strip()]
        self._config["captcha"]["session_refresh_warmup_urls"] = ",".join(cleaned)

    @property
    def session_refresh_wait_seconds_per_url(self) -> int:
        value = self._config.get("captcha", {}).get("session_refresh_wait_seconds_per_url", 60)
        try:
            return max(0, min(600, int(value)))
        except Exception:
            return 60

    def set_session_refresh_wait_seconds_per_url(self, seconds: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_wait_seconds_per_url"] = max(0, min(600, int(seconds)))

    @property
    def session_refresh_overall_timeout_seconds(self) -> int:
        value = self._config.get("captcha", {}).get("session_refresh_overall_timeout_seconds", 180)
        try:
            return max(10, min(1800, int(value)))
        except Exception:
            return 180

    def set_session_refresh_overall_timeout_seconds(self, seconds: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_overall_timeout_seconds"] = max(10, min(1800, int(seconds)))

    @property
    def session_refresh_update_st_from_cookie(self) -> bool:
        return bool(self._config.get("captcha", {}).get("session_refresh_update_st_from_cookie", True))

    def set_session_refresh_update_st_from_cookie(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_update_st_from_cookie"] = bool(enabled)

    @property
    def session_refresh_fail_if_st_refresh_fails(self) -> bool:
        return bool(self._config.get("captcha", {}).get("session_refresh_fail_if_st_refresh_fails", True))

    def set_session_refresh_fail_if_st_refresh_fails(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_fail_if_st_refresh_fails"] = bool(enabled)

    @property
    def session_refresh_local_only(self) -> bool:
        return bool(self._config.get("captcha", {}).get("session_refresh_local_only", True))

    def set_session_refresh_local_only(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_local_only"] = bool(enabled)

    @property
    def session_refresh_scheduler_enabled(self) -> bool:
        return bool(self._config.get("captcha", {}).get("session_refresh_scheduler_enabled", False))

    def set_session_refresh_scheduler_enabled(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_scheduler_enabled"] = bool(enabled)

    @property
    def session_refresh_scheduler_interval_minutes(self) -> int:
        value = self._config.get("captcha", {}).get("session_refresh_scheduler_interval_minutes", 30)
        try:
            return max(1, min(1440, int(value)))
        except Exception:
            return 30

    def set_session_refresh_scheduler_interval_minutes(self, minutes: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_scheduler_interval_minutes"] = max(1, min(1440, int(minutes)))

    @property
    def session_refresh_scheduler_batch_size(self) -> int:
        value = self._config.get("captcha", {}).get("session_refresh_scheduler_batch_size", 10)
        try:
            return max(1, min(200, int(value)))
        except Exception:
            return 10

    def set_session_refresh_scheduler_batch_size(self, size: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_scheduler_batch_size"] = max(1, min(200, int(size)))

    @property
    def session_refresh_scheduler_only_expiring_within_minutes(self) -> int:
        value = self._config.get("captcha", {}).get("session_refresh_scheduler_only_expiring_within_minutes", 60)
        try:
            return max(1, min(10080, int(value)))
        except Exception:
            return 60

    def set_session_refresh_scheduler_only_expiring_within_minutes(self, minutes: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["session_refresh_scheduler_only_expiring_within_minutes"] = max(1, min(10080, int(minutes)))

    @property
    def st_only_refresh_scheduler_enabled(self) -> bool:
        return bool(self._config.get("captcha", {}).get("st_only_refresh_scheduler_enabled", False))

    def set_st_only_refresh_scheduler_enabled(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["st_only_refresh_scheduler_enabled"] = bool(enabled)

    @property
    def st_only_refresh_scheduler_interval_minutes(self) -> int:
        value = self._config.get("captcha", {}).get("st_only_refresh_scheduler_interval_minutes", 5)
        try:
            return max(1, min(1440, int(value)))
        except Exception:
            return 5

    def set_st_only_refresh_scheduler_interval_minutes(self, minutes: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["st_only_refresh_scheduler_interval_minutes"] = max(1, min(1440, int(minutes)))

    @property
    def st_only_refresh_scheduler_batch_size(self) -> int:
        value = self._config.get("captcha", {}).get("st_only_refresh_scheduler_batch_size", 20)
        try:
            return max(1, min(200, int(value)))
        except Exception:
            return 20

    def set_st_only_refresh_scheduler_batch_size(self, size: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["st_only_refresh_scheduler_batch_size"] = max(1, min(200, int(size)))

    @property
    def st_only_refresh_scheduler_expiring_within_minutes(self) -> int:
        value = self._config.get("captcha", {}).get("st_only_refresh_scheduler_expiring_within_minutes", 5)
        try:
            return max(1, min(10080, int(value)))
        except Exception:
            return 5

    def set_st_only_refresh_scheduler_expiring_within_minutes(self, minutes: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["st_only_refresh_scheduler_expiring_within_minutes"] = max(1, min(10080, int(minutes)))

    @property
    def dedicated_extension_enabled(self) -> bool:
        return bool(self._config.get("captcha", {}).get("dedicated_extension_enabled", False))

    def set_dedicated_extension_enabled(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["dedicated_extension_enabled"] = bool(enabled)

    @property
    def dedicated_extension_captcha_timeout_seconds(self) -> int:
        value = self._config.get("captcha", {}).get("dedicated_extension_captcha_timeout_seconds", 25)
        try:
            return max(5, min(180, int(value)))
        except Exception:
            return 25

    def set_dedicated_extension_captcha_timeout_seconds(self, seconds: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["dedicated_extension_captcha_timeout_seconds"] = max(5, min(180, int(seconds)))

    @property
    def dedicated_extension_st_refresh_timeout_seconds(self) -> int:
        value = self._config.get("captcha", {}).get("dedicated_extension_st_refresh_timeout_seconds", 45)
        try:
            return max(10, min(300, int(value)))
        except Exception:
            return 45

    def set_dedicated_extension_st_refresh_timeout_seconds(self, seconds: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["dedicated_extension_st_refresh_timeout_seconds"] = max(10, min(300, int(seconds)))

    @property
    def extension_fallback_to_managed_on_dedicated_failure(self) -> bool:
        return bool(
            self._config.get("captcha", {}).get("extension_fallback_to_managed_on_dedicated_failure", False)
        )

    def set_extension_fallback_to_managed_on_dedicated_failure(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["extension_fallback_to_managed_on_dedicated_failure"] = bool(enabled)


# Global config instance
config = Config()
