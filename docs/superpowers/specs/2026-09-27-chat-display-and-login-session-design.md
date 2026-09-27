# Chat display and login session design

## User-visible behavior

- Blank records returned by WAHA do not create chat bubbles when they contain no text, caption, media, or supported display data.
- Opening or switching to a chat keeps the full history but places the viewport at the newest message.
- A new message follows the viewport only when the operator is already near the bottom. If the operator is reading older messages, the viewport stays stable and a return-to-latest action is available.
- The admin login remains valid for a fixed seven days in the same browser profile, including browser and panel restarts. Logout, password changes, and disabled admin access invalidate sessions.

## Boundaries

- The blank-message rule is display/data normalization only; it does not delete WAHA messages.
- The seven-day session is a panel authentication feature; it does not change WAHA session credentials.
- The session is not tied to IP address or user-agent, so ordinary network changes do not unexpectedly log the operator out.
- Basic Auth remains a compatibility fallback and CSRF protection remains required for mutations.

## Data and security

- Use an opaque random browser token with `Max-Age=604800`, `HttpOnly`, `SameSite=Lax`, and conditional `Secure` for HTTPS requests.
- Persist only a token hash, admin identity, created time, expiry time, and revocation metadata in an additive `admin_sessions` SQLite table.
- Never place passwords or plaintext session tokens in logs, HTML, local storage, or the database.

## Scroll invariants

- Initial chat open and post-load refresh target the newest message.
- Older-history prepend preserves the prior viewport anchor.
- Request-generation and selected-chat guards prevent stale asynchronous responses from changing another chat's DOM or scroll position.
