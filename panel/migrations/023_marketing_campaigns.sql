-- Additive marketing campaign, per-contact state and redacted event history.
-- Marketing tables deliberately store only chat_key_hmac.  Raw WhatsApp chat
-- identifiers remain inside the ChatService boundary.
CREATE TABLE IF NOT EXISTS marketing_campaigns (
    campaign_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    target_rules_json TEXT NOT NULL DEFAULT '{}',
    product_scope_json TEXT NOT NULL DEFAULT '[]',
    allow_image_send INTEGER NOT NULL DEFAULT 0 CHECK (allow_image_send IN (0, 1)),
    cadence_json TEXT NOT NULL DEFAULT '{}',
    max_touches INTEGER NOT NULL DEFAULT 1 CHECK (max_touches BETWEEN 1 AND 100),
    quiet_hours_json TEXT NOT NULL DEFAULT '{}',
    language_policy TEXT NOT NULL DEFAULT 'customer',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    paused_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_marketing_campaigns_active
    ON marketing_campaigns(active, updated_at);

CREATE TABLE IF NOT EXISTS marketing_contact_states (
    contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES marketing_campaigns(campaign_id) ON DELETE CASCADE,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'ELIGIBLE' CHECK (
        stage IN ('ELIGIBLE', 'CONTACTED', 'FOLLOW_UP_DUE', 'REPLIED', 'QUALIFIED',
                  'HUMAN_HANDOFF', 'PAUSED', 'OPTED_OUT', 'COMPLETED')
    ),
    interest_product_id TEXT,
    last_contact_at INTEGER,
    last_reply_at INTEGER,
    next_action_at INTEGER,
    touch_count INTEGER NOT NULL DEFAULT 0 CHECK (touch_count >= 0),
    paused_reason TEXT,
    opt_out INTEGER NOT NULL DEFAULT 0 CHECK (opt_out IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(campaign_id, session_name, chat_key_hmac)
);

CREATE INDEX IF NOT EXISTS idx_marketing_contact_states_due
    ON marketing_contact_states(campaign_id, session_name, stage, next_action_at);
CREATE INDEX IF NOT EXISTS idx_marketing_contact_states_chat
    ON marketing_contact_states(session_name, chat_key_hmac, updated_at);

CREATE TABLE IF NOT EXISTS marketing_events (
    event_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES marketing_campaigns(campaign_id) ON DELETE CASCADE,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    event_type TEXT NOT NULL,
    product_id TEXT,
    asset_id TEXT,
    message_ref TEXT,
    reason_code TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_marketing_events_campaign
    ON marketing_events(campaign_id, created_at);
CREATE INDEX IF NOT EXISTS idx_marketing_events_contact
    ON marketing_events(session_name, chat_key_hmac, created_at);
