CREATE TABLE IF NOT EXISTS iptv_manual_cards (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    customer_key TEXT NOT NULL,
    session_name TEXT NOT NULL,
    chat_id_ciphertext TEXT NOT NULL,
    message_key TEXT NOT NULL,
    request_key TEXT NOT NULL,
    subscription TEXT NOT NULL CHECK(subscription IN ('99','1','3','6','12')),
    pack TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    state TEXT NOT NULL CHECK(state IN ('creating','created','sent','unknown','failed')),
    credentials_ciphertext TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_iptv_manual_request
    ON iptv_manual_cards(provider, session_name, request_key);
CREATE INDEX IF NOT EXISTS idx_iptv_manual_created
    ON iptv_manual_cards(provider, created_at);
