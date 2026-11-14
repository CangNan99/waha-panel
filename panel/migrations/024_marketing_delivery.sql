-- Unified text + product-image marketing delivery ledger.
-- Only the HMAC chat identity is retained here.  Raw chat identifiers stay in
-- the existing automated_send_requests table owned by ChatService.
CREATE TABLE IF NOT EXISTS marketing_deliveries (
    delivery_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    text_request_id TEXT NOT NULL UNIQUE,
    text_state TEXT NOT NULL DEFAULT 'PENDING',
    text_message_ref TEXT,
    image_request_id TEXT,
    image_state TEXT NOT NULL DEFAULT 'NOT_REQUESTED',
    image_message_ref TEXT,
    product_id TEXT,
    variant TEXT NOT NULL DEFAULT '',
    asset_id TEXT,
    product_scope_json TEXT NOT NULL DEFAULT '[]',
    allow_image_send INTEGER NOT NULL DEFAULT 0 CHECK (allow_image_send IN (0, 1)),
    touch_number INTEGER NOT NULL DEFAULT 0 CHECK (touch_number >= 0),
    image_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (image_retry_count >= 0),
    state TEXT NOT NULL DEFAULT 'PENDING',
    error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_name, chat_key_hmac, campaign_id, touch_number)
);

CREATE INDEX IF NOT EXISTS idx_marketing_deliveries_contact
    ON marketing_deliveries(session_name, chat_key_hmac, updated_at);
CREATE INDEX IF NOT EXISTS idx_marketing_deliveries_campaign
    ON marketing_deliveries(campaign_id, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_marketing_deliveries_daily
    ON marketing_deliveries(session_name, state, updated_at);
