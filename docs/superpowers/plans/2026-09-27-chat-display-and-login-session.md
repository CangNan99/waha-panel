# Chat display and seven-day login session

## Approved scope

Implement the approved v2 update in the isolated `feature/chat-engagement-1.0.3` worktree:

1. Filter history items that have no displayable text, caption, media, or supported message content while preserving media-only messages.
2. When a chat is opened or switched, retain the full history but position the message viewport at the newest message. Follow new messages when the operator is already near the bottom; preserve manual history reading and expose a return-to-latest affordance when new content arrives above the viewport.
3. Add a fixed seven-day persistent browser session, scoped to one browser profile, while preserving HTTP Basic Auth fallback. Store only a hashed session token and expiry in the existing persistent SQLite database. Invalidate sessions on logout, password change, and disabled admin access.

## Constraints

- Do not change WAHA configuration or restart/deploy healthy remote containers in this plan.
- Keep CSRF protection for mutating requests.
- Do not log or expose passwords, API keys, cookies, session tokens, QR payloads, or customer message content.
- Tests must be written and observed failing before production code for each behavior.

## Tasks

### Task 1 — Empty message filtering

- Add focused fixtures and tests for empty records, text records, captioned media, and media-only records.
- Verify the new tests fail against the current implementation.
- Filter empty non-media rows in the message normalization path and add a render-layer guard.
- Run focused chat tests and the full Python chat test group.

### Task 2 — Latest-message positioning and live scroll behavior

- Add tests for opening with and without cache, real-time/poll refresh while at bottom, manual history reading, return-to-latest, older-history prepend, and stale chat responses.
- Verify the tests fail against the current page source/behavior.
- Update the chat page state machine without regressing keyed row reuse or older-history scroll restoration.
- Run frontend syntax/page-source tests and the focused chat suite.

### Task 3 — Persistent seven-day admin sessions

- Add tests for session creation, cookie attributes, valid/expired sessions, restart persistence, logout, password-change invalidation, disabled admin rejection, Basic Auth fallback, and CSRF continuity.
- Verify the tests fail against the current Basic Auth-only implementation.
- Add an idempotent `admin_sessions` schema, opaque token hashing, fixed `604800` second expiry, request authentication, logout route, and response cookie handling.
- Run admin/auth tests and the full Python suite.

### Task 4 — Documentation and verification

- Update Chinese and English README/install documentation with the approved behavior.
- Record the design and verification plan in this file and its matching spec document.
- Run the complete available test suite, inspect the diff, and perform a self-review.
- Keep deployment and remote service changes out of scope until separately approved.

## Acceptance evidence

- No empty non-media bubble is rendered or archived.
- Opening a chat lands at its newest message; live updates follow the bottom without interrupting deliberate history reading.
- A valid session survives browser restart and panel restart for seven fixed days, then expires; logout/password changes/disabled accounts invalidate it.
- Existing Basic Auth and CSRF behavior remain functional.
- Tests pass with no secret-bearing output.

## Rollback

Before any runtime migration or deployment, snapshot the panel SQLite database and record the current image/code revision. Reverting the worktree or image leaves the additive session table unused by the old code; restore the database snapshot if a data rollback is required. No remote deployment is part of this plan.
