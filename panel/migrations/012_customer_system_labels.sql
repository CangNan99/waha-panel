CREATE TABLE IF NOT EXISTS customer_system_labels (
    id INTEGER PRIMARY KEY,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_name, chat_key_hmac, label)
);

CREATE INDEX IF NOT EXISTS idx_customer_system_labels_chat
    ON customer_system_labels(session_name, chat_key_hmac, updated_at);
