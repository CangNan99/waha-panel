CREATE TABLE IF NOT EXISTS customer_first_seen (
    session_name TEXT NOT NULL,
    customer_key_hmac TEXT NOT NULL,
    first_inbound_at INTEGER NOT NULL,
    last_inbound_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_name, customer_key_hmac)
);

CREATE INDEX IF NOT EXISTS idx_customer_first_seen_first
    ON customer_first_seen(session_name, first_inbound_at);
