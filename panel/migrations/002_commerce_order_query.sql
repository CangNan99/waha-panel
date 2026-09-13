CREATE TABLE IF NOT EXISTS commerce_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS order_verification_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_hash TEXT NOT NULL UNIQUE,
    email_failure_count INTEGER NOT NULL DEFAULT 0 CHECK (email_failure_count >= 0),
    email_locked INTEGER NOT NULL DEFAULT 0 CHECK (email_locked IN (0, 1)),
    last_attempt_at INTEGER,
    unlocked_at INTEGER,
    unlocked_by TEXT
);

CREATE TABLE IF NOT EXISTS order_query_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    session_hash TEXT NOT NULL,
    verification_method TEXT NOT NULL DEFAULT '',
    result_code TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    safe_summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS manual_order_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    customer_hash TEXT NOT NULL,
    query_type TEXT NOT NULL,
    safe_clue TEXT NOT NULL DEFAULT '',
    failure_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'answered', 'closed')),
    admin_answer TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    answered_at INTEGER,
    closed_at INTEGER,
    customer_reply_sent_at INTEGER
);

CREATE TABLE IF NOT EXISTS order_notification_dedupe (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    notification_type TEXT NOT NULL,
    case_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    message_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    sent_at INTEGER,
    FOREIGN KEY(case_id) REFERENCES manual_order_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_order_verification_locked
    ON order_verification_states(email_locked, last_attempt_at);
CREATE INDEX IF NOT EXISTS idx_order_query_audit_created
    ON order_query_audit(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_manual_order_cases_status
    ON manual_order_cases(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_order_notification_case
    ON order_notification_dedupe(case_id, notification_type);
