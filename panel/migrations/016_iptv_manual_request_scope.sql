DROP INDEX IF EXISTS idx_iptv_manual_request;

CREATE UNIQUE INDEX IF NOT EXISTS idx_iptv_manual_request
    ON iptv_manual_cards(provider, session_name, customer_key, request_key);
