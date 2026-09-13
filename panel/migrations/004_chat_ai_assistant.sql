CREATE TABLE IF NOT EXISTS translation_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    api_key_ciphertext TEXT NOT NULL DEFAULT '',
    key_fingerprint TEXT NOT NULL DEFAULT '',
    last_test_status TEXT NOT NULL DEFAULT 'never'
        CHECK (last_test_status IN ('never', 'success', 'failed')),
    last_test_at INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS message_translation_cache (
    source_hash TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    target_language TEXT NOT NULL,
    result_ciphertext TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_accessed_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (source_hash, model_fingerprint, prompt_version, target_language)
);

CREATE INDEX IF NOT EXISTS idx_message_translation_cache_expiry
    ON message_translation_cache(expires_at);

CREATE TABLE IF NOT EXISTS chat_takeovers (
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('AI_ELIGIBLE', 'HUMAN_TAKEOVER')),
    paused_at INTEGER,
    resumed_at INTEGER,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_name, chat_key_hmac)
);

CREATE INDEX IF NOT EXISTS idx_chat_takeovers_state
    ON chat_takeovers(session_name, state);

CREATE TABLE IF NOT EXISTS manual_send_requests (
    client_request_id TEXT PRIMARY KEY,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'SENT', 'FAILED', 'UNKNOWN')),
    waha_message_id TEXT,
    error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_manual_send_session_chat
    ON manual_send_requests(session_name, chat_key_hmac, created_at);
