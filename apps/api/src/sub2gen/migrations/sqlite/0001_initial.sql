-- Generated from the validated Flow2API SQLite schema.
-- Do not edit in place; add a new checksummed migration.

CREATE TABLE admin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    username TEXT DEFAULT 'admin',
                    password TEXT DEFAULT 'admin',
                    api_key TEXT DEFAULT 'han1234',
                    error_ban_threshold INTEGER DEFAULT 3,
                    error_ban_enabled INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

CREATE TABLE admin_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    last_used_at INTEGER
                );

CREATE TABLE api_clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

CREATE TABLE api_key_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(api_key_id, account_id),
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                );

CREATE TABLE api_key_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key_id INTEGER,
                    endpoint TEXT NOT NULL,
                    account_id INTEGER,
                    status_code INTEGER NOT NULL,
                    detail TEXT,
                    ip TEXT,
                    user_agent TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                );

CREATE TABLE api_key_rate_limits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key_id INTEGER NOT NULL,
                    endpoint TEXT NOT NULL,
                    rpm INTEGER DEFAULT 0,
                    rph INTEGER DEFAULT 0,
                    burst INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(api_key_id, endpoint),
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                );

CREATE TABLE api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_plaintext TEXT,
                    key_hash TEXT NOT NULL UNIQUE,
                    scopes TEXT DEFAULT '*',
                    is_active BOOLEAN DEFAULT 1,
                    expires_at TIMESTAMP,
                    last_used_at TIMESTAMP,
                    last_presence_at TIMESTAMP,
                    adobe_cloning_enabled INTEGER DEFAULT 1,
                    adobe_metadata_enabled INTEGER DEFAULT 1,
                    adobe_tracker_enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (client_id) REFERENCES api_clients(id)
                );

CREATE TABLE cache_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    cache_enabled BOOLEAN DEFAULT 0,
                    cache_timeout INTEGER DEFAULT 7200,
                    cache_base_url TEXT,
                    cache_provider TEXT DEFAULT 'local',
                    cache_delivery_mode TEXT DEFAULT 'proxy',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

CREATE TABLE cache_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL UNIQUE,
                    api_key_id INTEGER NOT NULL,
                    token_id INTEGER,
                    flow_project_id TEXT,
                    media_type TEXT,
                    source_url TEXT,
                    storage_provider TEXT DEFAULT 'local',
                    object_key TEXT,
                    delivery_mode TEXT DEFAULT 'proxy',
                    size_bytes INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id),
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                );

CREATE TABLE call_logic_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    call_mode TEXT DEFAULT 'default',
                    polling_mode_enabled BOOLEAN DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

CREATE TABLE captcha_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    captcha_method TEXT DEFAULT 'browser',
                    yescaptcha_api_key TEXT DEFAULT '',
                    yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                    yescaptcha_task_type TEXT DEFAULT 'RecaptchaV3TaskProxylessM1S9',
                    capmonster_api_key TEXT DEFAULT '',
                    capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                    capmonster_min_score REAL DEFAULT 0.9,
                    ezcaptcha_api_key TEXT DEFAULT '',
                    ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                    capsolver_api_key TEXT DEFAULT '',
                    capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                    remote_browser_base_url TEXT DEFAULT '',
                    remote_browser_api_key TEXT DEFAULT '',
                    remote_browser_timeout INTEGER DEFAULT 60,
                    extension_queue_wait_timeout_seconds INTEGER DEFAULT 20,
                    browser_fallback_to_remote_browser BOOLEAN DEFAULT 1,
                    website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                    page_action TEXT DEFAULT 'IMAGE_GENERATION',

                    browser_proxy_enabled BOOLEAN DEFAULT 0,
                    browser_proxy_url TEXT,
                    browser_count INTEGER DEFAULT 1,
                    browser_personal_fresh_restart_every_n_solves INTEGER DEFAULT 10,
                    personal_project_pool_size INTEGER DEFAULT 4,
                    personal_max_resident_tabs INTEGER DEFAULT 5,
                    personal_idle_tab_ttl_seconds INTEGER DEFAULT 600,
                    browser_captcha_page_url TEXT DEFAULT 'https://labs.google/fx/api/auth/providers',
                    session_refresh_enabled BOOLEAN DEFAULT 1,
                    session_refresh_browser_first BOOLEAN DEFAULT 1,
                    session_refresh_inject_st_cookie BOOLEAN DEFAULT 1,
                    session_refresh_warmup_urls TEXT DEFAULT 'https://labs.google/fx/tools/flow,https://labs.google/fx',
                    session_refresh_wait_seconds_per_url INTEGER DEFAULT 60,
                    session_refresh_overall_timeout_seconds INTEGER DEFAULT 180,
                    session_refresh_update_st_from_cookie BOOLEAN DEFAULT 1,
                    session_refresh_fail_if_st_refresh_fails BOOLEAN DEFAULT 1,
                    session_refresh_local_only BOOLEAN DEFAULT 1,
                    session_refresh_scheduler_enabled BOOLEAN DEFAULT 0,
                    session_refresh_scheduler_interval_minutes INTEGER DEFAULT 30,
                    session_refresh_scheduler_batch_size INTEGER DEFAULT 10,
                    session_refresh_scheduler_only_expiring_within_minutes INTEGER DEFAULT 60,
                    st_only_refresh_scheduler_enabled BOOLEAN DEFAULT 0,
                    st_only_refresh_scheduler_interval_minutes INTEGER DEFAULT 5,
                    st_only_refresh_scheduler_batch_size INTEGER DEFAULT 20,
                    st_only_refresh_scheduler_expiring_within_minutes INTEGER DEFAULT 5,
                    dedicated_extension_enabled BOOLEAN DEFAULT 0,
                    dedicated_extension_captcha_timeout_seconds INTEGER DEFAULT 25,
                    dedicated_extension_st_refresh_timeout_seconds INTEGER DEFAULT 45,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                , extension_fallback_to_managed_on_dedicated_failure BOOLEAN DEFAULT 0);

CREATE TABLE captcha_worker_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_prefix TEXT NOT NULL UNIQUE,
                    key_hash TEXT NOT NULL UNIQUE,
                    label TEXT DEFAULT '',
                    key_plaintext TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    last_instance_id TEXT,
                    last_seen_at TIMESTAMP,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

CREATE TABLE debug_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    log_requests BOOLEAN DEFAULT 1,
                    log_responses BOOLEAN DEFAULT 1,
                    mask_token BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

CREATE TABLE extension_worker_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_key TEXT NOT NULL UNIQUE,
                    api_key_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                );

CREATE TABLE geminigen_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT DEFAULT '',
                raw_cookie TEXT DEFAULT '',
                bearer_token TEXT DEFAULT '',
                refresh_token TEXT DEFAULT '',
                guard_id TEXT DEFAULT '',
                turnstile_token TEXT DEFAULT '',
                is_active BOOLEAN DEFAULT 1,
                image_concurrency INTEGER DEFAULT 5,
                video_concurrency INTEGER DEFAULT 5,
                image_in_flight INTEGER DEFAULT 0,
                video_in_flight INTEGER DEFAULT 0,
                image_gen_daily_limit_reset_at TIMESTAMP,
                grok_image_daily_limit_reset_at TIMESTAMP,
                video_daily_limit_reset_at TIMESTAMP,
                last_status TEXT,
                last_error TEXT,
                last_used_at TIMESTAMP,
                profile_user_id INTEGER,
                profile_uuid TEXT,
                profile_email TEXT,
                profile_full_name TEXT,
                profile_is_active BOOLEAN,
                available_credit INTEGER,
                plan_credit INTEGER,
                purchased_credit INTEGER,
                locked_credit INTEGER,
                subscription_credit INTEGER,
                plan_name TEXT,
                plan_expire_at TIMESTAMP,
                active_benefits_json TEXT,
                is_image_gen_max BOOLEAN,
                is_image_premium BOOLEAN,
                image_gen_concurrent_streams INTEGER,
                remaining_image_gen_max_daily_images INTEGER,
                remaining_image_gen_free_daily_images INTEGER,
                image_gen_quota_synced_at TIMESTAMP,
                is_grok_image_max BOOLEAN,
                grok_image_concurrent_streams INTEGER,
                remaining_grok_image_max_daily_images INTEGER,
                remaining_grok_image_free_daily_images INTEGER,
                grok_image_quota_synced_at TIMESTAMP,
                is_grok_max BOOLEAN,
                grok_max_concurrent_streams INTEGER,
                grok_max_quota_synced_at TIMESTAMP,
                remaining_bulk_videos INTEGER,
                remaining_daily_videos INTEGER,
                remaining_grok_max_daily_videos INTEGER,
                remaining_grok_max_daily_720p_videos INTEGER,
                remaining_grok_max_daily_10s_videos INTEGER,
                remaining_grok_max_daily_15s_videos INTEGER,
                quota_synced_at TIMESTAMP,
                quota_sync_status TEXT,
                quota_sync_error TEXT,
                profile_synced_at TIMESTAMP,
                profile_sync_status TEXT,
                profile_sync_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE geminigen_config (
                id INTEGER PRIMARY KEY DEFAULT 1,
                enabled BOOLEAN DEFAULT 0,
                video_enabled BOOLEAN DEFAULT 1,
                base_url TEXT DEFAULT 'https://api.geminigen.ai',
                poll_interval_image_sec REAL DEFAULT 3.0,
                poll_interval_video_sec REAL DEFAULT 12.0,
                timeout_image_sec REAL DEFAULT 600.0,
                timeout_video_sec REAL DEFAULT 1800.0,
                global_image_concurrency INTEGER DEFAULT 5,
                global_video_concurrency INTEGER DEFAULT 5,
                cache_outputs BOOLEAN DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE geminigen_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                upstream_uuid TEXT,
                account_id INTEGER,
                api_key_id INTEGER,
                request_log_id INTEGER,
                public_model_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'image',
                endpoint_type TEXT NOT NULL DEFAULT 'imagen',
                prompt TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER DEFAULT 0,
                raw_artifact_urls TEXT,
                cached_artifact_urls TEXT,
                request_payload TEXT,
                response_payload TEXT,
                error_message TEXT,
                error_code TEXT,
                retry_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES geminigen_accounts(id),
                FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
            );

CREATE TABLE generation_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    image_timeout INTEGER DEFAULT 300,
                    video_timeout INTEGER DEFAULT 1500,
                    max_retries INTEGER DEFAULT 3,
                    extension_generation_enabled BOOLEAN DEFAULT 0,
                    extension_generation_fallback_mode TEXT DEFAULT 'local_http_on_recaptcha',
                    flow2api_gemini_api_keys TEXT DEFAULT '',
                    flow2api_openai_api_keys TEXT DEFAULT '',
                    flow2api_third_party_gemini_api_keys TEXT DEFAULT '',
                    flow2api_third_party_gemini_base_url TEXT DEFAULT '',
                    cloudflare_account_id TEXT DEFAULT '',
                    cloudflare_api_token TEXT DEFAULT '',
                    flow2api_csvgen_cookie TEXT DEFAULT '',
                    flow2api_csvgen_api_keys TEXT DEFAULT '',
                    flow2api_cloning_model TEXT DEFAULT 'gemini-2.5-flash',
                    flow2api_metadata_backend TEXT DEFAULT 'gemini_native',
                    flow2api_metadata_provider_order TEXT DEFAULT '',
                    flow2api_metadata_enabled_providers TEXT DEFAULT '',
                    flow2api_metadata_provider_retry_count INTEGER DEFAULT 1,
                    flow2api_metadata_model TEXT DEFAULT 'gemini-2.5-flash',
                    flow2api_metadata_enabled_models TEXT DEFAULT '',
                    flow2api_metadata_primary_model TEXT DEFAULT '',
                    flow2api_metadata_fallback_models TEXT DEFAULT '',
                    metadata_system_prompt TEXT DEFAULT '',
                    flow2api_cloning_backend TEXT DEFAULT 'gemini_native',
                    flow2api_cloning_provider_order TEXT DEFAULT '',
                    flow2api_cloning_enabled_providers TEXT DEFAULT '',
                    flow2api_cloning_provider_retry_count INTEGER DEFAULT 1,
                    cloning_image_system_prompt TEXT DEFAULT '',
                    cloning_video_system_prompt TEXT DEFAULT '',
                    task_tracker_device_id TEXT DEFAULT '',
                    task_tracker_device_name TEXT DEFAULT '',
                    task_tracker_cookies TEXT DEFAULT '',
                    task_tracker_device_token TEXT DEFAULT '',
                    task_tracker_turnstile_token TEXT DEFAULT '',
                    task_tracker_tls_profile TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                , flow2api_openrouter_api_keys TEXT DEFAULT '', flow2api_cloning_gemini_api_keys TEXT DEFAULT '', flow2api_cloning_openai_api_keys TEXT DEFAULT '', flow2api_cloning_openrouter_api_keys TEXT DEFAULT '', flow2api_cloning_third_party_gemini_api_keys TEXT DEFAULT '', flow2api_cloning_third_party_gemini_base_url TEXT DEFAULT '', flow2api_cloning_cloudflare_account_id TEXT DEFAULT '', flow2api_cloning_cloudflare_api_token TEXT DEFAULT '');

CREATE TABLE operation_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT NOT NULL UNIQUE,
                success_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                today_success_count INTEGER DEFAULT 0,
                today_error_count INTEGER DEFAULT 0,
                today_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE plugin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    connection_token TEXT DEFAULT '',
                    auto_enable_on_update BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

CREATE TABLE projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    api_key_id INTEGER,
                    project_name TEXT NOT NULL,
                    tool_name TEXT DEFAULT 'PINHOLE',
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id),
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                );

CREATE TABLE proxy_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    proxy_url TEXT,
                    media_proxy_enabled BOOLEAN DEFAULT 0,
                    media_proxy_url TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

CREATE TABLE redis_persisted_events (
                    cursor TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    persisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

CREATE TABLE request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    api_key_id INTEGER,
                    operation TEXT NOT NULL,
                    request_body TEXT,
                    response_body TEXT,
                    status_code INTEGER NOT NULL,
                    duration FLOAT NOT NULL,
                    status_text TEXT DEFAULT '',
                    progress INTEGER DEFAULT 0,
                    request_excerpt TEXT DEFAULT '',
                    response_excerpt TEXT DEFAULT '',
                    payload_available BOOLEAN DEFAULT 0,
                    payload_object_key TEXT,
                    payload_storage_error TEXT,
                    request_size_bytes INTEGER DEFAULT 0,
                    response_size_bytes INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id),
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                );

CREATE TABLE runway_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT DEFAULT '',
                raw_credential TEXT DEFAULT '',
                is_active BOOLEAN DEFAULT 1,
                workspace_id TEXT,
                team_id TEXT,
                concurrency_limit INTEGER DEFAULT 1,
                in_flight INTEGER DEFAULT 0,
                last_status TEXT,
                last_error TEXT,
                last_used_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE runway_config (
                id INTEGER PRIMARY KEY DEFAULT 1,
                enabled BOOLEAN DEFAULT 0,
                base_url TEXT DEFAULT 'https://api.runwayml.com/v1',
                poll_interval_sec REAL DEFAULT 3.0,
                timeout_sec REAL DEFAULT 600.0,
                cache_outputs BOOLEAN DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE runway_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_model_id TEXT UNIQUE NOT NULL,
                display_name TEXT DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'image',
                task_type TEXT NOT NULL,
                builder_key TEXT DEFAULT '',
                default_options TEXT DEFAULT '{}',
                request_mapping TEXT DEFAULT '{}',
                capability_schema TEXT DEFAULT '{}',
                media_roles TEXT DEFAULT '[]',
                supported_modes TEXT DEFAULT '[]',
                limits TEXT DEFAULT '{}',
                feature_flags TEXT DEFAULT '[]',
                cost_feature TEXT DEFAULT '',
                source_version TEXT DEFAULT '',
                live_available BOOLEAN DEFAULT 1,
                disabled_reason TEXT DEFAULT '',
                last_synced_at TIMESTAMP,
                is_enabled BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE runway_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                upstream_task_id TEXT,
                account_id INTEGER,
                api_key_id INTEGER,
                public_model_id TEXT NOT NULL,
                prompt TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'processing',
                progress INTEGER DEFAULT 0,
                raw_artifact_urls TEXT,
                cached_artifact_urls TEXT,
                request_payload TEXT,
                response_payload TEXT,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES runway_accounts(id) ON DELETE SET NULL,
                FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
            );

CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    api_key_id INTEGER,
                    project_id TEXT,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    progress INTEGER DEFAULT 0,
                    result_urls TEXT,
                    base_result_urls TEXT,
                    delivery_urls TEXT,
                    requested_resolution TEXT,
                    output_resolution TEXT,
                    upscale_status TEXT,
                    upscale_error_message TEXT,
                    error_message TEXT,
                    scene_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP, job_phase TEXT, captcha_status TEXT, captcha_detail TEXT,
                    FOREIGN KEY (token_id) REFERENCES tokens(id),
                    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
                );

CREATE TABLE token_refresh_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    refresh_interval_minutes INTEGER NOT NULL DEFAULT 120,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

CREATE TABLE token_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER NOT NULL,
                    image_count INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    last_success_at TIMESTAMP,
                    last_error_at TIMESTAMP,
                    today_image_count INTEGER DEFAULT 0,
                    today_video_count INTEGER DEFAULT 0,
                    today_error_count INTEGER DEFAULT 0,
                    today_date DATE,
                    consecutive_error_count INTEGER DEFAULT 0,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                );

CREATE TABLE tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    st TEXT UNIQUE NOT NULL,
                    at TEXT,
                    at_expires TIMESTAMP,
                    email TEXT NOT NULL,
                    name TEXT,
                    remark TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP,
                    use_count INTEGER DEFAULT 0,
                    credits INTEGER DEFAULT 0,
                    user_paygate_tier TEXT,
                    current_project_id TEXT,
                    current_project_name TEXT,
                    image_enabled BOOLEAN DEFAULT 1,
                    video_enabled BOOLEAN DEFAULT 1,
                    image_concurrency INTEGER DEFAULT -1,
                    video_concurrency INTEGER DEFAULT -1,
                    captcha_proxy_url TEXT,
                    extension_route_key TEXT,
                    use_extension_for_generation INTEGER NOT NULL DEFAULT 1,
                    auth_mode TEXT NOT NULL DEFAULT 'session_token',
                    browser_profile_path TEXT,
                    browser_profile_status TEXT DEFAULT 'not_created',
                    browser_profile_email TEXT,
                    browser_profile_name TEXT,
                    browser_profile_login_state TEXT DEFAULT 'unknown',
                    browser_profile_cookie_status TEXT DEFAULT 'unknown',
                    browser_profile_st_status TEXT DEFAULT 'unknown',
                    browser_profile_at_status TEXT DEFAULT 'unknown',
                    browser_profile_last_opened_at TIMESTAMP,
                    browser_profile_last_sync_at TIMESTAMP,
                    browser_profile_last_refresh_at TIMESTAMP,
                    browser_profile_last_error TEXT,
                    protocol_mode TEXT NOT NULL DEFAULT 'session',
                    google_cookies TEXT NOT NULL DEFAULT '',
                    login_account TEXT NOT NULL DEFAULT '',
                    login_password TEXT NOT NULL DEFAULT '',
                    proxy_url TEXT NOT NULL DEFAULT '',
                    auto_refresh_enabled BOOLEAN NOT NULL DEFAULT 1,
                    refresh_interval_minutes INTEGER NOT NULL DEFAULT 120,
                    last_st_refresh_at TIMESTAMP,
                    last_st_refresh_result TEXT NOT NULL DEFAULT '',
                    ban_reason TEXT,
                    banned_at TIMESTAMP
                );

CREATE INDEX idx_api_key_accounts_key_id ON api_key_accounts(api_key_id);

CREATE INDEX idx_api_key_audit_created_at ON api_key_audit_logs(created_at DESC);

CREATE INDEX idx_api_key_audit_logs_created_at ON api_key_audit_logs(created_at);

CREATE INDEX idx_api_key_rl_key_endpoint ON api_key_rate_limits(api_key_id, endpoint);

CREATE INDEX idx_api_keys_client_id ON api_keys(client_id);

CREATE INDEX idx_cache_files_api_key_filename ON cache_files(api_key_id, filename);

CREATE INDEX idx_cache_files_api_key_project ON cache_files(api_key_id, flow_project_id);

CREATE INDEX idx_captcha_worker_keys_active ON captcha_worker_keys(is_active);

CREATE INDEX idx_extension_worker_bindings_api_key_id ON extension_worker_bindings(api_key_id);

CREATE INDEX idx_geminigen_tasks_retention ON geminigen_tasks(status, completed_at, updated_at);

CREATE INDEX idx_operation_stats_operation ON operation_stats(operation);

CREATE INDEX idx_project_id ON projects(project_id);

CREATE INDEX idx_projects_api_key_created_at ON projects(api_key_id, created_at DESC);

CREATE INDEX idx_request_logs_api_key_id_created_at ON request_logs(api_key_id, created_at DESC);

CREATE INDEX idx_request_logs_api_key_op_created ON request_logs(api_key_id, operation, created_at DESC);

CREATE INDEX idx_request_logs_created_at ON request_logs(created_at DESC);

CREATE INDEX idx_request_logs_operation_created_at ON request_logs(operation, created_at DESC);

CREATE INDEX idx_request_logs_status_created_at ON request_logs(status_code, status_text, created_at DESC);

CREATE INDEX idx_request_logs_token_id_created_at ON request_logs(token_id, created_at DESC);

CREATE INDEX idx_task_id ON tasks(task_id);

CREATE INDEX idx_token_st ON tokens(st);

CREATE INDEX idx_token_stats_token_id ON token_stats(token_id);

CREATE INDEX idx_tokens_email ON tokens(email);

CREATE INDEX idx_tokens_is_active_last_used_at ON tokens(is_active, last_used_at);
