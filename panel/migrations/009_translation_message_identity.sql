ALTER TABLE machine_message_translations
    ADD COLUMN message_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_machine_message_translations_identity
    ON machine_message_translations(session_name, chat_key_hmac, message_id, source_fingerprint, updated_at);
