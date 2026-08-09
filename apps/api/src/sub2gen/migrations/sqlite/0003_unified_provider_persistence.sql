CREATE TABLE provider_accounts (
    id TEXT PRIMARY KEY,
    provider_key TEXT NOT NULL,
    label TEXT NOT NULL,
    external_account_id TEXT,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    legacy_source TEXT,
    legacy_id TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider_key, legacy_source, legacy_id)
);

CREATE TABLE worker_devices (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    approved_capabilities_json TEXT NOT NULL DEFAULT '[]',
    auth_key_hash TEXT,
    public_key TEXT,
    credential_expires_at TIMESTAMP,
    revoked_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE credential_bindings (
    id TEXT PRIMARY KEY,
    provider_account_id TEXT NOT NULL,
    worker_id TEXT,
    binding_key TEXT NOT NULL,
    credential_type TEXT NOT NULL,
    storage_kind TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    expires_at TIMESTAMP,
    last_validated_at TIMESTAMP,
    last_error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(provider_account_id) REFERENCES provider_accounts(id) ON DELETE CASCADE,
    FOREIGN KEY(worker_id) REFERENCES worker_devices(id) ON DELETE SET NULL,
    UNIQUE(provider_account_id, binding_key)
);

CREATE TABLE generation_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    api_key_id INTEGER,
    request_id TEXT NOT NULL,
    job_kind TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_account_id TEXT,
    worker_id TEXT,
    resolved_execution_json TEXT,
    deadline_at TIMESTAMP,
    terminal_at TIMESTAMP,
    error_code TEXT,
    error_detail TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE SET NULL,
    FOREIGN KEY(provider_account_id) REFERENCES provider_accounts(id) ON DELETE SET NULL,
    FOREIGN KEY(worker_id) REFERENCES worker_devices(id) ON DELETE SET NULL
);

CREATE TABLE generation_attempts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    lease_id TEXT,
    provider_job_id TEXT,
    resolved_execution_json TEXT,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    error_code TEXT,
    error_detail TEXT,
    FOREIGN KEY(job_id) REFERENCES generation_jobs(id) ON DELETE CASCADE,
    UNIQUE(job_id, attempt)
);

CREATE INDEX idx_provider_accounts_provider_enabled
    ON provider_accounts(provider_key, enabled);
CREATE INDEX idx_credential_bindings_account_enabled
    ON credential_bindings(provider_account_id, enabled);
CREATE INDEX idx_credential_bindings_worker
    ON credential_bindings(worker_id);
CREATE INDEX idx_worker_devices_enabled
    ON worker_devices(enabled);
CREATE INDEX idx_generation_jobs_status_created
    ON generation_jobs(status, created_at);
CREATE INDEX idx_generation_jobs_worker_status
    ON generation_jobs(worker_id, status);
CREATE INDEX idx_generation_attempts_job
    ON generation_attempts(job_id, attempt);
CREATE UNIQUE INDEX idx_generation_attempts_active_lease
    ON generation_attempts(lease_id) WHERE lease_id IS NOT NULL;
