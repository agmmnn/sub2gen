CREATE TABLE operator_audit_events (
    id BIGSERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_operator_audit_events_created
    ON operator_audit_events(created_at DESC, id DESC);
