CREATE TABLE provider_account_api_keys (
    provider_account_id TEXT NOT NULL,
    api_key_id INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(provider_account_id, api_key_id),
    FOREIGN KEY(provider_account_id) REFERENCES provider_accounts(id) ON DELETE CASCADE,
    FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
);

CREATE INDEX idx_provider_account_api_keys_key
    ON provider_account_api_keys(api_key_id, provider_account_id);
