CREATE TABLE IF NOT EXISTS iptv_trials_scoped (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    customer_key TEXT NOT NULL,
    session_name TEXT NOT NULL,
    message_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('creating','created','sent','unknown','failed','cancelled')),
    credentials_ciphertext TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(provider, session_name, customer_key)
);

CREATE INDEX IF NOT EXISTS idx_iptv_trials_scoped_created
    ON iptv_trials_scoped(provider, created_at);
