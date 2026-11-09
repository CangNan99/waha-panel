CREATE TABLE IF NOT EXISTS session_ai_profiles (
    session_name TEXT NOT NULL,
    capability TEXT NOT NULL CHECK (
        capability IN ('language', 'vision_understanding', 'vision_reasoning', 'image_generation')
    ),
    provider TEXT NOT NULL DEFAULT 'glm' CHECK (provider IN ('glm', 'openai_compatible')),
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    api_key_ciphertext TEXT NOT NULL DEFAULT '',
    key_fingerprint TEXT NOT NULL DEFAULT '',
    reasoning_mode TEXT NOT NULL DEFAULT 'disabled' CHECK (
        reasoning_mode IN ('disabled', 'enabled', 'low', 'medium', 'high', 'max')
    ),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_name, capability),
    FOREIGN KEY (session_name) REFERENCES managed_sessions(session_name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_session_ai_profiles_session
    ON session_ai_profiles(session_name, capability);
