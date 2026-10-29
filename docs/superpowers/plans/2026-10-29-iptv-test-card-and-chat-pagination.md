# IPTV Test Card and Chat Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make IPTV test requests reliable across languages, persistently retry automatic failures until delivery succeeds or automation is disabled, add repeatable manual card generation for five subscription lengths, and preserve loaded chat pages during refresh.

**Architecture:** Keep automatic eligibility and manual generation in separate SQLite records. A structured intent classifier gates automatic requests; AI tool calls remain preferred, with a backend fallback for explicit requests. A persistent IPTV retry worker resumes automatic records and manual send-only records after process restarts. The chat page tracks the loaded window size and refetches that window on realtime refresh.

**Tech Stack:** Python 3.12, `sqlite3`, `threading`, existing `IptvService`/`PanelState`, vanilla JavaScript embedded in `panel/chat_page.py`, unittest, Node syntax and browser acceptance tests.

**Spec:** `docs/superpowers/specs/2026-10-29-iptv-test-card-and-chat-pagination-design.md`

## Global Constraints

- Automatic retries have no attempt limit and stop only after successful delivery, automatic IPTV is disabled, or the session is deleted; use persistent exponential backoff with a capped interval and one in-flight attempt per record.
- Manual supplier creation failures do not auto-create again; a new administrator click creates a new manual record. Manual send failures retry only the already encrypted account.
- Manual request IDs are scoped to the same provider, session, and customer chat so a replay from another chat cannot reuse credentials.
- Context confirmations are accepted only for a recent system-generated IPTV test invitation; a manual card message is never an invitation.
- Fallback language order is current customer message, recent context, English.
- Keep API keys, usernames, passwords, full playlist URLs, and retry payload credentials out of logs, browser responses, and AI context.
- Do not restart or modify WAHA, delete persistent volumes, or overwrite the SQLite database during local implementation or verification.

## Review Focus

- Supplier timeout after account creation: the retry worker may create a duplicate because the upstream API has no idempotency key; test single-flight behavior and make the risk observable without logging credentials.
- Send failure after a successful supplier response: retry the same encrypted account and send exactly once after success; never call supplier creation again.
- Automatic fallback when the model returns ordinary text or an invalid tool: only an explicit multilingual request or a valid system invitation confirmation may open a trial.
- Manual card generation after an automatic trial: allow the manual request regardless of automatic eligibility and keep records separate.
- Realtime refresh after loading page two: refetch the current loaded window and preserve the count, order, selected chat, and scroll anchor.

---

### Task 1: Intent classifier and persistent IPTV records

**Files:**
- Create: `panel/migrations/014_iptv_manual_cards.sql`
- Create: `panel/migrations/016_iptv_manual_request_scope.sql`
- Create: `panel/migrations/015_iptv_trial_retries.sql`
- Modify: `panel/iptv_service.py` (intent helpers, retry records, manual card service, worker lifecycle)
- Test: `tests/test_iptv_service.py`

**Interfaces:**
- Consumes: existing encrypted provider/session settings, `ActivationClient`, `IptvService.create_trial`, `mark_delivery`, and `reply_text`.
- Produces: `classify_trial_intent(text, context) -> dict`, `infer_trial_language(text, context) -> str`, `IptvService.create_manual_card(session, chat_id, message_id, subscription, request_id, language='auto') -> dict`, `IptvService.mark_manual_delivery(card_id, success) -> None`, `IptvService.start()`, `IptvService.stop()`, and a single-attempt retry entry point used by `PanelState` callbacks.

- [x] **Step 1: Write failing classifier and manual-record tests.**

  Add tests for direct Chinese/English/Portuguese/Spanish/French/Arabic/Turkish/German/Russian/Italian requests, Japanese/Korean/Dutch/Vietnamese/Indonesian examples, Unicode-normalized punctuation, explicit price/fault/negative messages, and a short affirmative only when the context contains a system IPTV invitation. Add tests that a manual `99/1/3/6/12` request creates independent records twice even when an automatic trial already exists, while invalid subscriptions are rejected.

- [x] **Step 2: Run the focused tests and verify they fail for the missing classifier and table/API.**

  Run: `python -m unittest tests.test_iptv_service -v`

  Expected: failures naming the new classifier, manual card, or persistence behavior.

- [x] **Step 3: Add the additive migrations.**

  Create `iptv_manual_cards` without a per-customer unique constraint, storing only HMAC customer identity and encrypted credentials. Create a generic `iptv_trial_retries` table keyed by `(record_type, record_id)` so automatic rows can retry creation or delivery and manual rows can retry delivery only; store phase (`create` or `send`), attempt count, next retry time, last error code, and language. Add indexes for due records. Keep existing `iptv_trials` constraints unchanged.

- [x] **Step 4: Implement normalized intent and language helpers in `panel/iptv_service.py`.**

  Normalize with Unicode NFKC and bounded input. Evaluate negative/price/fault rules before positive rules. Recognize direct requests in the existing and added languages, and recognize short confirmations only when a recent system invitation marker is present. Return the language in the required fallback order; keep `explicit_trial_request` as a compatibility wrapper.

- [x] **Step 5: Implement manual card creation and delivery bookkeeping.**

  Add `create_manual_card(...)` with strict subscription validation, request-ID idempotency, existing provider response validation, and encrypted credentials. Add message builders for `99` (12-hour test) and `1/3/6/12` month labels. Add `mark_manual_delivery` so a send failure schedules only a send retry for the existing encrypted account; supplier creation failures remain terminal until another click.

- [x] **Step 6: Implement persistent retry state and one-at-a-time worker operations.**

  Extend automatic creation and delivery failures to enqueue a retry row. Implement `start/stop` with a daemon worker, capped exponential backoff, due-row scan on startup, single-flight locking, and pause checks supplied by `PanelState`. Automatic creation retries call the supplier again for the same logical record; send retries decrypt and resend the existing account. Keep all retry logs credential-free.

- [x] **Step 7: Run the focused tests and verify they pass.**

  Run: `python -m unittest tests.test_iptv_service -v`

  Expected: all existing IPTV service tests plus the new classifier, manual-card, idempotency, and retry-state tests pass.

### Task 2: Automatic fallback, retry lifecycle, and protected API

**Files:**
- Modify: `panel/app.py` (`PanelState.enable_chat_services`, background lifecycle, `_ai_reply` fallback, `_execute_iptv_trial`, POST routing)
- Test: `tests/test_iptv_integration.py`
- Test: `tests/test_ui_routes.py`

**Interfaces:**
- Consumes: Task 1 classifier, manual-card methods, retry worker, and delivery state methods.
- Produces: protected `POST /api/chat/sessions/<session>/iptv/manual-card` accepting `{chat_ref, subscription, client_request_id, language?}` and returning only delivery status; automatic fallback dispatch through `_execute_iptv_trial`.

- [x] **Step 1: Write failing integration tests.**

  Add a model response with no tool call plus an explicit multilingual request and assert one automatic send. Add supplier rejection, unknown response, process restart, and send-failure cases that show persistent retry and exactly-once successful delivery. Add manual endpoint tests for repeated cards, five subscription values, CSRF/session checks, no credential response, and manual send-failure resend without supplier recreation.

- [x] **Step 2: Run the focused integration tests and verify they fail.**

  Run: `python -m unittest tests.test_iptv_integration tests.test_ui_routes -v`

  Expected: failures for fallback dispatch, retry lifecycle, endpoint routing, or response redaction.

- [x] **Step 3: Wire `IptvService` callbacks and lifecycle into `PanelState`.**

  Pass credential-safe send, label, session-availability, and pause callbacks from `PanelState.enable_chat_services`. Start the IPTV worker from `start_background_services` and stop it from `stop_background_services`, alongside existing memory and automation services.

- [x] **Step 4: Add the AI fallback and invitation marker handling.**

  After `_ai_reply`, when no valid tool call exists, call the structured intent helper. Dispatch the existing `_execute_iptv_trial` only for an explicit request or a system invitation confirmation. Use the Task 1 language fallback and do not send the model’s ordinary reply in that branch.

- [x] **Step 5: Add the manual-card route.**

  Require admin authentication, CSRF, a valid managed WAHA session, and a decodable chat reference. Call `create_manual_card`, send immediately through `_send_text(origin='iptv_manual')`, mark delivery, and publish a chat refresh. Return `{status, card_id}` without credentials; queue send-only retry if the send call fails.

- [x] **Step 6: Run integration tests and verify they pass.**

  Run: `python -m unittest tests.test_iptv_integration tests.test_ui_routes -v`

  Expected: fallback, retry, manual route, auth, idempotency, and redaction tests pass.

### Task 3: Chat controls and stable pagination

**Files:**
- Modify: `panel/chat_page.py` (state, composer markup, `loadChats`, realtime refresh, manual-card action)
- Modify: `tests/ui_fixture_server.py` (manual-card fixture and paged overview behavior)
- Test: `tests/frontend_syntax.test.mjs`
- Test: `tests/ui_acceptance.mjs` or a focused new `tests/iptv_manual_card_acceptance.mjs`

**Interfaces:**
- Consumes: Task 2 manual-card endpoint and current `/overview` pagination contract.
- Produces: a selected-chat control with labels `测试`, `1个月`, `3个月`, `6个月`, `12个月`; refresh behavior that retains the loaded chat window.

- [x] **Step 1: Write failing browser/JavaScript behavior tests.**

  Assert the control is disabled without a selected connected chat, sends the chosen subscription with a unique request ID, shows a success status without rendering credentials, and refreshes messages. Assert overview requests of 30 then 60 items, then a realtime refresh that requests 60 and keeps 60; search and unread filters reset to 30.

- [x] **Step 2: Run syntax and focused UI tests to verify failure.**

  Run: `node scripts/check_frontend_syntax.mjs` and the focused acceptance command used by the repository for the new fixture.

  Expected: missing control or shrinking-list assertions fail before implementation.

- [x] **Step 3: Add the manual-card selector/button near the composer.**

  Keep the operator-facing labels in Chinese, disable while no chat or a request is in flight, call the protected route with `client_request_id`, show only status text, and reload the selected conversation after success.

- [x] **Step 4: Preserve the loaded chat window on refresh.**

  Add `chatLoadedLimit` (minimum 30), use it for non-append refreshes, increase it after each append, and fetch consecutive pages when the target exceeds the backend per-request cap. Keep `chatOffset` for append requests, pass `{preserveLoaded:true}` from event and polling refresh paths, and reset the loaded limit for search and unread-filter changes; retain merge, selection, and scroll-anchor logic.

- [x] **Step 5: Run syntax and UI tests and verify they pass.**

  Run: `node scripts/check_frontend_syntax.mjs` plus the focused browser acceptance tests and the existing `tests/ui_acceptance.mjs` suite.

  Expected: syntax, control, send-status, pagination, selection, and scroll tests pass.

### Task 4: Full verification and delivery evidence

**Files:**
- Modify: `docs/iptv-trials.md` (automatic retry and manual-card behavior)
- Modify: `docs/releases/` only if a release note is requested after local verification

- [x] **Step 1: Run the full Python test suite.**

  Run the repository’s complete unittest command and record the exact pass/fail counts. If a failure occurs, stop at that failure and diagnose before changing unrelated code.

- [x] **Step 2: Run frontend syntax and browser acceptance checks.**

  Run `node scripts/check_frontend_syntax.mjs` and the relevant UI acceptance suites with the fixture server.

- [x] **Step 3: Review the diff and credential-safety checks.**

  Confirm no key, password, token, full playlist URL, or customer identifier was added to logs, fixtures, test output, or the browser response. Confirm existing untracked plan files remain untouched.

- [x] **Step 4: Present local evidence and a separate deployment gate.**

  Report changed files, test commands and results, migration behavior, retry risk, and the exact backup/rollback steps required for a remote update. Do not update the running container until the user explicitly approves that deployment scope.
