CREATE TABLE provider_account_api_keys (
    provider_account_id TEXT NOT NULL REFERENCES provider_accounts(id) ON DELETE CASCADE,
    api_key_id BIGINT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(provider_account_id, api_key_id)
);

CREATE INDEX idx_provider_account_api_keys_key
    ON provider_account_api_keys(api_key_id, provider_account_id);
