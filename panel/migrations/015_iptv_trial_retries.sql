CREATE TABLE IF NOT EXISTS iptv_trial_retries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL CHECK(record_type IN ('automatic','manual')),
    record_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('create','send')),
    chat_id_ciphertext TEXT NOT NULL,
    session_name TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER NOT NULL,
    last_error_code TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(record_type, record_id)
);

CREATE INDEX IF NOT EXISTS idx_iptv_trial_retries_due
    ON iptv_trial_retries(next_retry_at, updated_at);
