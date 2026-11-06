CREATE TABLE IF NOT EXISTS product_image_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    auto_send_enabled INTEGER NOT NULL DEFAULT 1 CHECK (auto_send_enabled IN (0, 1)),
    daily_limit INTEGER NOT NULL DEFAULT 20 CHECK (daily_limit BETWEEN 1 AND 1000),
    updated_at INTEGER NOT NULL
);

INSERT OR IGNORE INTO product_image_settings(id, auto_send_enabled, daily_limit, updated_at)
VALUES (1, 1, 20, CAST(strftime('%s', 'now') AS INTEGER));

CREATE TABLE IF NOT EXISTS product_image_assets (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    caption_template TEXT NOT NULL DEFAULT '',
    relative_path TEXT NOT NULL UNIQUE,
    original_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL CHECK (mime_type IN ('image/jpeg', 'image/png', 'image/webp')),
    byte_size INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_image_assets_active_order
    ON product_image_assets(active, sort_order, created_at);

CREATE TABLE IF NOT EXISTS product_image_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS product_image_asset_tags (
    asset_id TEXT NOT NULL REFERENCES product_image_assets(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES product_image_tags(id) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 100,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    created_at INTEGER NOT NULL,
    UNIQUE(asset_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_product_image_asset_tags_tag
    ON product_image_asset_tags(tag_id, is_primary, priority, asset_id);
CREATE INDEX IF NOT EXISTS idx_product_image_asset_tags_asset
    ON product_image_asset_tags(asset_id, priority, is_primary);

CREATE TABLE IF NOT EXISTS product_image_send_requests (
    request_id TEXT PRIMARY KEY,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    message_key TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES product_image_assets(id),
    tag_key TEXT NOT NULL,
    caption_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'SENT', 'FAILED', 'UNKNOWN')),
    waha_message_id TEXT,
    error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_name, chat_key_hmac, message_key, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_product_image_send_requests_state
    ON product_image_send_requests(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_product_image_send_requests_chat
    ON product_image_send_requests(session_name, chat_key_hmac, created_at);
