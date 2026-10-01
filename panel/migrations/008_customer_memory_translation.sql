CREATE TABLE IF NOT EXISTS customer_memories (
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    chat_id_ciphertext TEXT NOT NULL DEFAULT '',
    memory_ciphertext TEXT NOT NULL DEFAULT '',
    message_cursor INTEGER NOT NULL DEFAULT 0,
    message_fingerprint TEXT NOT NULL DEFAULT '',
    model_fingerprint TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'EMPTY'
        CHECK (status IN ('EMPTY', 'PENDING', 'READY', 'FAILED')),
    last_error_code TEXT,
    generated_at INTEGER,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_name, chat_key_hmac)
);
CREATE INDEX IF NOT EXISTS idx_customer_memories_status
    ON customer_memories(session_name, status, updated_at);

CREATE TABLE IF NOT EXISTS conversation_memory_jobs (
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    chat_id_ciphertext TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (state IN ('PENDING', 'RUNNING', 'FAILED', 'DONE')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_run_at INTEGER NOT NULL,
    claimed_at INTEGER,
    last_error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_name, chat_key_hmac)
);
CREATE INDEX IF NOT EXISTS idx_conversation_memory_jobs_due
    ON conversation_memory_jobs(state, next_run_at);

CREATE TABLE IF NOT EXISTS machine_message_translations (
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    message_ref TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    source_language TEXT NOT NULL DEFAULT 'auto',
    target_language TEXT NOT NULL DEFAULT 'zh',
    translation_ciphertext TEXT,
    detected_language TEXT,
    status TEXT NOT NULL CHECK (status IN ('READY', 'SKIPPED', 'FAILED')),
    last_error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (
        session_name, chat_key_hmac, message_ref,
        source_fingerprint, target_language
    )
);
CREATE INDEX IF NOT EXISTS idx_machine_message_translations_chat
    ON machine_message_translations(session_name, chat_key_hmac, message_ref, updated_at);

CREATE TABLE IF NOT EXISTS aliyun_translation_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    endpoint TEXT NOT NULL DEFAULT 'mt.cn-hangzhou.aliyuncs.com',
    region_id TEXT NOT NULL DEFAULT 'cn-hangzhou',
    access_key_id TEXT NOT NULL DEFAULT '',
    access_key_secret_ciphertext TEXT NOT NULL DEFAULT '',
    last_test_status TEXT NOT NULL DEFAULT 'never',
    last_test_at INTEGER,
    updated_at INTEGER NOT NULL
);
INSERT OR IGNORE INTO aliyun_translation_settings
    (id, endpoint, region_id, access_key_id, access_key_secret_ciphertext,
     last_test_status, last_test_at, updated_at)
VALUES (1, 'mt.cn-hangzhou.aliyuncs.com', 'cn-hangzhou', '', '', 'never', NULL, strftime('%s', 'now'));
