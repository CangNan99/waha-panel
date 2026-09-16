-- Per-session customer notes for the chat management workspace.
CREATE TABLE IF NOT EXISTS chat_notes (
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_name, chat_key_hmac),
    FOREIGN KEY (session_name) REFERENCES managed_sessions(session_name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_notes_session
    ON chat_notes(session_name, updated_at DESC);
