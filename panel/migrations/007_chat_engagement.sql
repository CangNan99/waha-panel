CREATE TABLE IF NOT EXISTS follow_up_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    chat_id_ciphertext TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    due_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    delay_code TEXT NOT NULL CHECK (delay_code IN ('24h', '3d', '7d', '15d')),
    mode TEXT NOT NULL CHECK (mode IN ('AI', 'FIXED')),
    fixed_copy_ciphertext TEXT,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'RUNNING', 'SENT', 'SKIPPED', 'FAILED', 'UNKNOWN', 'CANCELLED')),
    skip_reason TEXT,
    error_code TEXT,
    error_message TEXT,
    waha_message_id TEXT,
    client_request_id TEXT NOT NULL UNIQUE,
    claimed_at INTEGER,
    completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_follow_up_due ON follow_up_tasks(state, due_at);
CREATE INDEX IF NOT EXISTS idx_follow_up_chat ON follow_up_tasks(session_name, chat_key_hmac, created_at);

CREATE TABLE IF NOT EXISTS customer_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('MANUAL', 'AI')),
    label TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_name, chat_key_hmac, source, label)
);
CREATE INDEX IF NOT EXISTS idx_customer_labels_chat
    ON customer_labels(session_name, chat_key_hmac, source, updated_at);

-- Task-generated sends have their own idempotency ledger.  They must never
-- share the human takeover/manual-send state machine.
CREATE TABLE IF NOT EXISTS automated_send_requests (
    client_request_id TEXT PRIMARY KEY,
    session_name TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'SENT', 'FAILED', 'UNKNOWN')),
    waha_message_id TEXT,
    error_code TEXT,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automated_send_session_chat
    ON automated_send_requests(session_name, chat_id, updated_at);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    summary_ciphertext TEXT NOT NULL,
    message_fingerprint TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(session_name, chat_key_hmac)
);
