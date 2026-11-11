-- Per-customer AI marketing delivery ledger.
-- The chat service keeps the raw chat id out of this table; the HMAC key is
-- enough to enforce idempotence while preserving the database privacy model.
CREATE TABLE IF NOT EXISTS ai_marketing_send_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    campaign_key TEXT NOT NULL,
    client_request_id TEXT NOT NULL UNIQUE,
    message_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'SENT', 'FAILED', 'UNKNOWN')),
    waha_message_id TEXT,
    error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_name, chat_key_hmac, campaign_key)
);

CREATE INDEX IF NOT EXISTS idx_ai_marketing_daily
    ON ai_marketing_send_requests(session_name, state, updated_at);

CREATE INDEX IF NOT EXISTS idx_ai_marketing_chat
    ON ai_marketing_send_requests(session_name, chat_key_hmac, updated_at);
