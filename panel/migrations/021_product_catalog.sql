CREATE TABLE IF NOT EXISTS product_catalog (
    product_id TEXT PRIMARY KEY,
    product_key TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    sku TEXT NOT NULL DEFAULT '',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    category TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    selling_points TEXT NOT NULL DEFAULT '',
    usage_scenarios TEXT NOT NULL DEFAULT '',
    exclusion_scenarios TEXT NOT NULL DEFAULT '',
    language_content_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    allow_ai_image_send INTEGER NOT NULL DEFAULT 1 CHECK (allow_ai_image_send IN (0,1)),
    allow_marketing_image_send INTEGER NOT NULL DEFAULT 1 CHECK (allow_marketing_image_send IN (0,1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_catalog_active ON product_catalog(active, product_name);
CREATE INDEX IF NOT EXISTS idx_product_catalog_sku ON product_catalog(sku);
SELECT 1;
