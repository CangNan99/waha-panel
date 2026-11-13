-- Product asset roles, matching diagnostics and audit trail (additive).
CREATE INDEX IF NOT EXISTS idx_product_image_assets_product_variant
    ON product_image_assets(product_id, variant, reference_role, active, sort_order, created_at);
CREATE TABLE IF NOT EXISTS product_image_match_audits (
    audit_id TEXT PRIMARY KEY,
    candidate_product_id TEXT,
    variant TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'primary',
    mime_type TEXT NOT NULL DEFAULT '',
    image_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_image_match_audits_created
    ON product_image_match_audits(created_at);
