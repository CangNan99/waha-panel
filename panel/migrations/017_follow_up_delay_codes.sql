CREATE TABLE IF NOT EXISTS follow_up_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    chat_id_ciphertext TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    due_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    delay_code TEXT NOT NULL CHECK (delay_code IN ('1h', '2h', '3h', '6h', '12h', '24h', '3d', '7d', '15d')),
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
