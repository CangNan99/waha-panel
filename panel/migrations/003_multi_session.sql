-- Multi-session schema reference.
-- Runtime migration is applied by ensure_multi_session_schema() in app.py so
-- existing SQLite files can be upgraded idempotently (including composite-key
-- table rebuilds) without executing non-repeatable ALTER statements twice.
CREATE TABLE IF NOT EXISTS managed_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS session_settings (
    session_name TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_name, key),
    FOREIGN KEY (session_name) REFERENCES managed_sessions(session_name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_knowledge_session ON knowledge_base(session_name, id);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_session_chat
    ON conversation_messages(session_name, chat_id, id);
CREATE INDEX IF NOT EXISTS idx_system_logs_session ON system_logs(session_name, id);
