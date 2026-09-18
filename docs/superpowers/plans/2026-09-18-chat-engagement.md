# Chat Engagement Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为聊天管理界面实现一次性客户跟进、手动/AI 客户标签、当前对话总结、人工接管五小时自动恢复和二维码磨砂占位，并发布为 `1.0.3`。

**Architecture:** 在现有 Python 面板进程内增加一个基于 SQLite 持久化状态的调度线程。`ChatService` 负责安全聊天身份、消息读取、标签、当前总结和接管时间；`TranslationService` 负责结构化 AI 调用；新增 `ChatAutomationService` 负责任务状态、到期检查、幂等发送和自动恢复；HTTP 路由和现有事件总线连接前端与服务。

**Tech Stack:** Python 3.12、SQLite、现有 `ChatService`/`TranslationService`、原生 HTML/CSS/JavaScript、Node.js `vm.Script` 前端语法检查、Python `unittest`。

**Spec:** `docs/superpowers/specs/2026-09-18-chat-engagement-design.md`

## Global Constraints

- 总结报告只保留当前一份，每次成功生成覆盖旧报告，不保留历史记录。
- AI 标签每次成功生成总结时整体替换；手动标签始终保留，两类标签必须颜色区分。
- 跟进任务是从创建时计时的一次性任务，只允许 `24h`、`3d`、`7d`、`15d`。
- 跟进任务支持 AI 生成发送和固定文案翻译发送两种模式；客户语言未知或翻译失败时不得发送原文。
- 到期前后发现创建后入站消息或人工接管时标记“已跳过”；发送结果未知时禁止自动重试。
- 人工接管五小时从最后一条已确认人工出站消息计算；没有人工消息时从点击接管计算。
- 不修改 WAHA API、消息数据结构、Docker Compose 网络或 Nginx 配置。
- 固定文案、总结内容、原始 chat id 和任何密钥必须使用现有数据加密机制；普通日志不得写入敏感文本。
- 发布使用新标签 `cangnan88/waha-panel:1.0.3`，不覆盖 `1.0.2`。

### Task 1: Add the persistent engagement schema

**Files:**
- Create: `panel/migrations/007_chat_engagement.sql`
- Modify: `panel/app.py:ensure_multi_session_schema/init_db`
- Create: `tests/test_chat_engagement.py`

**Interfaces:**
- Produces `follow_up_tasks`, `customer_labels`, and single-row-per-customer `conversation_summaries` tables.
- Extends `chat_takeovers` with `last_manual_sent_at` and `auto_resume_at` through the existing idempotent column helper.

- [ ] **Step 1: Write the failing migration test**

```python
def test_engagement_schema_is_created_and_idempotent(self):
    init_db(self.database, seed_business=False)
    init_db(self.database, seed_business=False)
    connection = sqlite3.connect(self.database)
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    takeover_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(chat_takeovers)")
    }
    connection.close()
    self.assertTrue({
        "follow_up_tasks", "customer_labels", "conversation_summaries"
    } <= tables)
    self.assertTrue({"last_manual_sent_at", "auto_resume_at"} <= takeover_columns)
```

- [ ] **Step 2: Run the migration test to verify it fails**

Run: `python -m unittest -v tests.test_chat_engagement.EngagementSchemaTests.test_engagement_schema_is_created_and_idempotent`

Expected: FAIL because the new tables and takeover columns do not yet exist.

- [ ] **Step 3: Write the migration and register it**

Create `007_chat_engagement.sql` with these exact state constraints and keys:

```sql
CREATE TABLE IF NOT EXISTS follow_up_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    chat_id_ciphertext TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    due_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    delay_code TEXT NOT NULL CHECK (delay_code IN ('24h', '3d', '7d', '15d')),
    mode TEXT NOT NULL CHECK (mode IN ('AI', 'FIXED')),
    fixed_copy_ciphertext TEXT,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'RUNNING', 'SENT', 'SKIPPED', 'FAILED', 'UNKNOWN', 'CANCELLED')),
    skip_reason TEXT,
    error_code TEXT,
    error_message TEXT,
    waha_message_id TEXT,
    client_request_id TEXT NOT NULL UNIQUE,
    claimed_at INTEGER,
    completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_follow_up_due ON follow_up_tasks(state, due_at);
CREATE INDEX IF NOT EXISTS idx_follow_up_chat ON follow_up_tasks(session_name, chat_key_hmac, created_at);

CREATE TABLE IF NOT EXISTS customer_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('MANUAL', 'AI')),
    label TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_name, chat_key_hmac, source, label)
);
CREATE INDEX IF NOT EXISTS idx_customer_labels_chat
    ON customer_labels(session_name, chat_key_hmac, source, updated_at);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    session_name TEXT NOT NULL,
    chat_key_hmac TEXT NOT NULL,
    summary_ciphertext TEXT NOT NULL,
    message_fingerprint TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(session_name, chat_key_hmac)
);
```

In `init_db`, execute this migration after `006_chat_metadata.sql`; call `_add_column_if_missing` for the two `chat_takeovers` columns before committing. Do not use a destructive table rebuild.

- [ ] **Step 4: Run the migration test to verify it passes**

Run: `python -m unittest -v tests.test_chat_engagement.EngagementSchemaTests`

Expected: PASS, including the second `init_db` call.

- [ ] **Step 5: Commit the schema change**

```bash
git add panel/migrations/007_chat_engagement.sql panel/app.py tests/test_chat_engagement.py
git commit -m "feat: add chat engagement persistence schema"
```

### Task 2: Extend ChatService for internal history, takeover timing, and automated sends

**Files:**
- Modify: `panel/chat_service.py`
- Modify: `tests/test_chat_engagement.py`
- Modify: `tests/test_chat_features.py` only when a shared fake client needs a new method

**Interfaces:**
- Produces `ChatService.chat_identity(session, chat_ref) -> dict` with `chat_id` and `chat_key_hmac` for trusted internal callers only.
- Produces `ChatService.history_for_id(session, chat_id, limit=20) -> list[dict]` with `timestamp`, `from_me`, `body`, `caption`, `message_id`.
- Produces `ChatService.has_inbound_since(session, chat_id, since_timestamp) -> bool`.
- Produces `ChatService.send_automated_text(session, chat_id, text, client_request_id) -> dict` without changing human takeover state.
- Produces `customer_labels`, `add_manual_label`, `update_manual_label`, `delete_manual_label`, `current_summary`, and `save_summary_and_ai_labels` methods.

- [ ] **Step 1: Write failing service tests**

Add a fake client with deterministic messages and send responses, then cover the three timing rules:

```python
def test_takeover_without_manual_message_uses_click_time(self):
    state = self.service.takeover("default", self.item["chat_ref"])
    self.assertEqual(state["auto_resume_at"], 1_005 * 1 + 18_000)

def test_successful_manual_send_moves_auto_resume_from_last_manual_send(self):
    result = self.service.send_text("default", self.item["chat_ref"], "人工回复", "00000000-0000-0000-0000-000000000001")
    self.assertEqual(result["state"], "SENT")
    state = self.service.takeover_state("default", self.item["chat_ref"])
    self.assertEqual(state["last_manual_sent_at"], 1_005)
    self.assertEqual(state["auto_resume_at"], 19_005)

def test_summary_upsert_replaces_current_report_but_preserves_manual_labels(self):
    self.service.add_manual_label("default", self.item["chat_ref"], "VIP")
    self.service.save_summary_and_ai_labels(
        "default", self.item["chat_ref"], {"summary": "第一份"}, ["高意向"]
    )
    self.service.save_summary_and_ai_labels(
        "default", self.item["chat_ref"], {"summary": "第二份"}, ["待报价"]
    )
    self.assertEqual(self.service.current_summary("default", self.item["chat_ref"])["summary"], "第二份")
    self.assertEqual(self.service.customer_labels("default", self.item["chat_ref"])["manual"], ["VIP"])
    self.assertEqual(self.service.customer_labels("default", self.item["chat_ref"])["ai"], ["待报价"])
```

Use a clock object starting at `1005` and set the fake send response to `{"id": "wamid.manual"}`. Store only encrypted summary payloads and decrypt only inside the service response.

- [ ] **Step 2: Run the service tests to verify they fail**

Run: `python -m unittest -v tests.test_chat_engagement.ChatServiceEngagementTests`

Expected: FAIL with missing service methods or missing takeover columns.

- [ ] **Step 3: Implement identity/history and inbound checks**

Refactor the normalization loop currently inside `messages()` into `history_for_id()`. `history_for_id()` must request at most 20 messages for AI context, sort by `(timestamp, message_id)`, and never return a chat reference. `has_inbound_since()` must first check `conversation_activity.last_incoming_at`; if that value is not newer than the task creation time, fetch the latest WAHA messages in bounded pages until timestamps are at or before the cutoff, returning `True` only for an item with `fromMe == False` and a newer timestamp.

- [ ] **Step 4: Implement takeover timestamp transitions**

Update `pause()` to set `paused_at=now`, `auto_resume_at=now+18_000`, and clear `last_manual_sent_at` only when starting a new takeover. Update `_finish_send()` callers so a confirmed `SENT` human message sets `last_manual_sent_at=now` and `auto_resume_at=now+18_000`; `FAILED` and `UNKNOWN` sends do not move the timer. Update `resume_ai()` to set `state='AI_ELIGIBLE'` and clear `auto_resume_at`.

- [ ] **Step 5: Implement automated text sending and labels/summary transactions**

`send_automated_text()` must validate the task-generated text, call the existing Waha client, and use the task's unique `client_request_id` as the idempotency key without inserting a `manual_send_requests` row or changing `chat_takeovers`. `save_summary_and_ai_labels()` must upsert the encrypted current summary, delete only `source='AI'` labels for the customer, insert the new AI labels, and commit both operations in one transaction. Manual label methods must reject `source='AI'` mutations and enforce a 100-character normalized label limit.

- [ ] **Step 6: Run service tests and the existing regression suite**

Run: `python -m unittest -v tests.test_chat_engagement.ChatServiceEngagementTests tests.test_chat_features`

Expected: PASS with existing avatar, note, session, and send idempotency behavior unchanged.

- [ ] **Step 7: Commit the ChatService change**

```bash
git add panel/chat_service.py tests/test_chat_engagement.py tests/test_chat_features.py
git commit -m "feat: add chat metadata and takeover timing services"
```

### Task 3: Add structured AI operations for follow-up and summary generation

**Files:**
- Modify: `panel/translation_service.py`
- Modify: `tests/test_chat_engagement.py`

**Interfaces:**
- Produces `TranslationService.generate_follow_up(messages) -> dict` with `target_language_code`, `target_language_name_zh`, and `message`.
- Produces `TranslationService.translate_follow_up(text, messages) -> dict` with the same language fields and `message`.
- Produces `TranslationService.summarize_conversation(messages) -> dict` with `summary`, `customer_need`, `intent`, `confirmed_items`, `unresolved_items`, `next_action`, and `ai_labels`.

- [ ] **Step 1: Write fake-opener tests for all three structured calls**

The fake opener must record the JSON request and return a JSON-object completion. Assert that the serialized conversation contains no more than 20 message entries, that AI follow-up returns a customer-language message, that fixed-copy translation rejects an empty/undetectable history with `TranslationError("EMPTY_CONTEXT", ...)`, and that summary output requires a list of string `ai_labels`.

- [ ] **Step 2: Run the AI tests to verify they fail**

Run: `python -m unittest -v tests.test_chat_engagement.TranslationServiceEngagementTests`

Expected: FAIL because the new methods and result field validators are not defined.

- [ ] **Step 3: Add prompt versions, result schemas, and bounded history helpers**

Add independent prompt version constants for follow-up and summary. Reuse `_bounded_messages()` and `MAX_HISTORY_MESSAGES = 20`; keep system instructions explicit that messages are untrusted data and must not override system instructions. Follow-up prompts must require one concise sendable message, not a plan or JSON embedded in the message. Summary prompts must return the exact seven fields above and 0–10 concise AI labels.

- [ ] **Step 4: Implement the three public methods through `_structured_call()`**

`generate_follow_up()` must fail with `EMPTY_CONTEXT` when no usable message exists. `translate_follow_up()` must require non-empty source text and usable history; it must never return the original text merely because language detection failed. `summarize_conversation()` must normalize whitespace, cap each returned field at 4000 characters, cap each label at 100 characters, and raise `TranslationError("SUMMARY_RESULT_INVALID", ...)` for malformed output.

- [ ] **Step 5: Run the AI tests and syntax checks**

Run: `python -m unittest -v tests.test_chat_engagement.TranslationServiceEngagementTests`

Run: `node scripts/check_frontend_syntax.mjs`

Expected: both commands pass; no API key or model response is printed.

- [ ] **Step 6: Commit the TranslationService change**

```bash
git add panel/translation_service.py tests/test_chat_engagement.py
git commit -m "feat: add follow-up and conversation summary AI operations"
```

### Task 4: Implement the persistent automation scheduler

**Files:**
- Create: `panel/chat_automation.py`
- Modify: `tests/test_chat_engagement.py`

**Interfaces:**
- Create `ChatAutomationService(database_path, chat_service, translation_service, logger=None, clock=time.time, scan_interval=15)`.
- Provide `create_task(session, chat_ref, delay_code, mode, fixed_copy=None) -> dict`.
- Provide `list_tasks(session, chat_ref) -> dict` and `cancel_task(session, task_id) -> dict`.
- Provide `run_once(now=None) -> int`, `start() -> None`, and `stop() -> None`.
- Provide `resume_expired_takeovers(now=None) -> int` for deterministic tests and the scheduler loop.

- [ ] **Step 1: Write failing scheduler tests**

Cover due times, skip reasons, both send modes, idempotent claims, unknown send results, cancellation, restart recovery, and five-hour auto-resume:

```python
def test_due_task_skips_after_new_inbound_message(self):
    task = self.automation.create_task("default", self.chat_ref, "24h", "AI")
    self.clock.value = task["due_at"]
    self.fake_chat.last_inbound_at = task["created_at"] + 1
    self.assertEqual(self.automation.run_once(), 1)
    self.assertEqual(self.automation.list_tasks("default", self.chat_ref)["items"][0]["state"], "SKIPPED")
```

Also assert a `UNKNOWN` send is never selected by a second `run_once()` call and that two concurrent `run_once()` calls produce one WAHA send.

- [ ] **Step 2: Run scheduler tests to verify they fail**

Run: `python -m unittest -v tests.test_chat_engagement.AutomationSchedulerTests`

Expected: FAIL because `panel/chat_automation.py` does not exist.

- [ ] **Step 3: Implement task creation and state transitions**

Map delay codes exactly to seconds `{24h: 86400, 3d: 259200, 7d: 604800, 15d: 1296000}`. Resolve the chat reference through `chat_identity()`, encrypt the raw chat ID with `DataCipher`, and insert `PENDING` with a UUID `client_request_id`. Reject `FIXED` tasks without non-empty text and reject unknown delay or mode values with public validation errors.

- [ ] **Step 4: Implement the atomic claim and due-task worker**

In one `BEGIN IMMEDIATE` transaction, select one due `PENDING` task and update it to `RUNNING`; commit before network/AI calls. Read `history_for_id(..., 20)`, call `has_inbound_since()` and `is_human_takeover()` again, and mark `SKIPPED` before any AI or send call when either rule matches. Use `generate_follow_up()` for `AI` and `translate_follow_up()` for `FIXED`; call `send_automated_text()` only after a valid non-empty result. Persist `SENT`, `FAILED`, or `UNKNOWN` with `completed_at`, `error_code`, and `waha_message_id` in a separate transaction.

- [ ] **Step 5: Implement restart recovery and takeover resume**

At `ChatAutomationService` construction, convert stale `RUNNING` tasks to `UNKNOWN` unless a stored Waha message ID already exists. `resume_expired_takeovers()` updates only rows where `state='HUMAN_TAKEOVER'` and `auto_resume_at <= now`, sets `AI_ELIGIBLE`, `resumed_at=now`, and clears `auto_resume_at`. The thread loop calls `run_once()` and `resume_expired_takeovers()` every 15 seconds and exits cleanly on `stop()`.

- [ ] **Step 6: Run scheduler tests and commit**

Run: `python -m unittest -v tests.test_chat_engagement.AutomationSchedulerTests`

Expected: PASS, including concurrent claim and restart tests.

```bash
git add panel/chat_automation.py tests/test_chat_engagement.py
git commit -m "feat: add persistent one-time follow-up scheduler"
```

### Task 5: Wire the scheduler, APIs, and webhook/manual-send events into PanelState

**Files:**
- Modify: `panel/app.py:PanelState.enable_chat_services`, `build_state`, `main`, `PanelHandler.chat_route`, `send_chat_get`, `send_chat_post`, `do_GET`, `do_POST`, `do_PUT`, `do_DELETE`
- Modify: `tests/test_chat_engagement.py`

**Interfaces:**
- `PanelState.enable_chat_services()` creates `self.automation` after `ChatService` and `TranslationService`.
- `PanelState.start_background_services()` and `stop_background_services()` own the scheduler thread.
- Chat routes accept `follow-ups`, `follow-ups/{id}/cancel`, `labels`, and `summary` while preserving existing two-segment routes.

- [ ] **Step 1: Write failing HTTP-level state tests**

Test that `enable_chat_services()` creates the automation service, `start_background_services()`/`stop_background_services()` are idempotent, and the route parser accepts:

```text
/api/chat/sessions/default/follow-ups
/api/chat/sessions/default/follow-ups/7/cancel
/api/chat/sessions/default/labels
/api/chat/sessions/default/summary
```

Assert every new handler still calls the existing admin/CSRF/session checks and never returns `fixed_copy` or raw chat IDs.

- [ ] **Step 2: Run the route tests to verify they fail**

Run: `python -m unittest -v tests.test_chat_engagement.PanelEngagementRouteTests`

Expected: FAIL because the routes and lifecycle methods are not registered.

- [ ] **Step 3: Wire service construction and lifecycle**

Import `ChatAutomationService`, set `self.automation = None` when chat capability is unavailable, and instantiate it with the existing logger and clock. In `main()`, call `state.start_background_services()` before `serve_forever()` and call `state.stop_background_services()` from a `finally` block after `server.shutdown()`/`server.server_close()`.

- [ ] **Step 4: Add GET/POST/PUT/DELETE route handling**

Extend the route parser without changing existing action names. Implement:

- `GET follow-ups` and `POST follow-ups` through `automation.list_tasks()`/`create_task()`;
- `POST follow-ups/{id}/cancel` through `automation.cancel_task()`;
- `GET labels`, `POST labels`, `PUT labels/{id}`, `DELETE labels/{id}` through `ChatService`;
- `GET summary` and `POST summary` by reading 20 messages, calling `summarize_conversation()`, then calling `save_summary_and_ai_labels()` atomically.

Wrap AI calls in the existing `begin_translation_request()`/`end_translation_request()` limiter. Return `TranslationError`, `ChatServiceError`, and validation errors through existing redacted service-error handling.

- [ ] **Step 5: Connect confirmed manual sends to takeover timing**

Ensure `send_chat_post()` continues publishing the existing refresh event after manual sends, and that `ChatService.send_text()`/`send_image()` update the confirmed-send timestamp only after a `SENT` result. The webhook path must remain unchanged except for using the extended takeover state when deciding whether AI may reply.

- [ ] **Step 6: Run route and existing tests**

Run: `python -m unittest -v tests.test_chat_engagement.PanelEngagementRouteTests tests.test_chat_features`

Expected: PASS with existing `/api/status`, multi-session, note, image, and manual send behavior unchanged.

- [ ] **Step 7: Commit the application wiring**

```bash
git add panel/app.py tests/test_chat_engagement.py
git commit -m "feat: expose engagement APIs and scheduler lifecycle"
```

### Task 6: Add chat-management UI for follow-ups, labels, summaries, and state preservation

**Files:**
- Modify: `panel/chat_page.py`
- Modify: `tests/test_chat_features.py`
- Modify: `tests/frontend_syntax.test.mjs` only for assertions specific to the new controls

**Interfaces:**
- Adds dialog controls with IDs `followUpButton`, `labelButton`, `summaryButton`, `followUpDialog`, `labelDialog`, and `summaryDialog`.
- Adds frontend functions `loadLabels()`, `saveManualLabel()`, `loadSummary()`, `generateSummary()`, `loadFollowUps()`, `createFollowUp()`, and `cancelFollowUp()`.

- [ ] **Step 1: Write failing page and syntax assertions**

Add assertions that `chat_management_page("default")` contains the three buttons, the two label source markers (`手动` and `AI`), the four delay options, the two follow-up modes, and the current-summary-only wording. Add an inline-script assertion that the new functions are present and that message reload code does not remove the dialogs.

- [ ] **Step 2: Run frontend tests to verify they fail**

Run: `python -m unittest -v tests.test_chat_features.ChatPageRegressionTests`

Run: `node --test tests/frontend_syntax.test.mjs`

Expected: the page-content assertions fail before the controls are added; syntax remains valid for the unchanged page.

- [ ] **Step 3: Add the controls and accessible dialogs**

Add compact header buttons for labels, summary, and follow-up. The label dialog allows free-form manual label entry and edit/delete actions; AI labels are read-only with a distinct color and `AI` marker. The summary dialog shows only the current report fields and a regenerate button; it contains no history list. The follow-up dialog contains the exact four delay choices, `AI`/`FIXED` mode choices, fixed-copy textarea, current task list, and cancel buttons.

- [ ] **Step 4: Implement state-aware API calls**

Use the existing `request()` helper and `state.selected.chat_ref`. On customer selection, load labels, current summary, and follow-up tasks. Disable only the relevant dialog action while a request is pending. Preserve dialog open state and current form values when `loadMessages()` or SSE/poll refresh runs; never include these dialogs in `messageStack.replaceChildren()`.

- [ ] **Step 5: Implement current-summary overwrite and tag rendering**

Render summary sections with `textContent`/safe DOM nodes, show the generation timestamp, and replace the displayed data only after a successful response. Render manual labels with the manual color and edit/delete actions; render AI labels with the AI color and no edit action. On a failed generation, keep the previous visible summary and labels and show a retryable error.

- [ ] **Step 6: Run syntax and page tests**

Run: `node scripts/check_frontend_syntax.mjs`

Run: `node --test tests/frontend_syntax.test.mjs`

Run: `python -m unittest -v tests.test_chat_features.ChatPageRegressionTests`

Expected: all inline JavaScript parses, the dialogs remain independent of message rendering, and all control-content assertions pass.

- [ ] **Step 7: Commit the chat-management UI**

```bash
git add panel/chat_page.py tests/test_chat_features.py tests/frontend_syntax.test.mjs
git commit -m "feat: add follow-up labels and current summary controls"
```

### Task 7: Add the frosted QR placeholder and release metadata

**Files:**
- Modify: `panel/app.py:multi_session_html_page` QR styles, markup, and `loadQr()`
- Modify: `panel-release.json`
- Modify: `docker-compose.yml`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `tests/test_chat_features.py`

**Interfaces:**
- The QR UI uses a non-scannable `.qr-frosted` placeholder before a real image is available.
- Release metadata reports panel version `1.0.3` while keeping WAHA tag and network configuration unchanged.

- [ ] **Step 1: Write failing QR and release metadata assertions**

Assert the multi-session page contains `.qr-frosted`, a blur/backdrop rule, a refresh label, and no embedded data URL. Assert `panel-release.json`, the Compose default image, and both README image references use `1.0.3`.

- [ ] **Step 2: Run the assertions to verify they fail**

Run: `python -m unittest -v tests.test_chat_features.QrAndReleaseTests`

Expected: FAIL because the page and release metadata still target the 1.0.2 behavior.

- [ ] **Step 3: Implement the placeholder without real QR data**

Use CSS-generated blocks or a static decorative pattern with `filter: blur(...)`/`backdrop-filter`, an explicit “刷新二维码” label, and no encoded session content. `loadQr()` must show the placeholder while loading and after errors, and replace it only after the response content type starts with `image/`.

- [ ] **Step 4: Update release metadata only**

Set the panel release tag/version, Compose panel image default, `PANEL_VERSION` default, and documentation examples to `1.0.3`. Do not change `WAHA_IMAGE`, `internal` network, ports, or Nginx-related instructions.

- [ ] **Step 5: Run QR, syntax, and release tests; commit**

Run: `python -m unittest -v tests.test_chat_features.QrAndReleaseTests`

Run: `node scripts/check_frontend_syntax.mjs`

```bash
git add panel/app.py panel-release.json docker-compose.yml README.md README.zh-CN.md tests/test_chat_features.py
git commit -m "feat: add frosted QR state and release 1.0.3 metadata"
```

### Task 8: Run the complete regression suite and isolated Docker acceptance tests

**Files:**
- Modify: `tests/test_chat_engagement.py` only if a failed acceptance assertion identifies a requirement gap
- Modify: `tests/test_chat_features.py` only if an existing regression needs a precise fixture update

**Interfaces:**
- No new production interface; this task validates the complete feature contract from the spec.

- [ ] **Step 1: Run Python and Node tests together**

Run: `python -m unittest discover -s tests -v`

Run: `node --test tests/frontend_syntax.test.mjs`
Run: `node scripts/check_frontend_syntax.mjs`

Expected: all tests pass; no test output contains API keys, passwords, fixed follow-up copy, customer messages, or summary content.

- [ ] **Step 2: Build a disposable image with the mandatory frontend gate**

Run from the repository root: `docker build --tag cangnan88/waha-panel:1.0.3-rc .`

Expected: the Node frontend-check stage runs `check_frontend_syntax.mjs` before the Python image is produced.

- [ ] **Step 3: Start an isolated Compose/project or container using a temporary data volume**

Use `PANEL_IMAGE=cangnan88/waha-panel:1.0.3-rc` and a temporary `PORTABLE_VOLUME_PREFIX`; do not point the test at the production volume. Verify `/api/status`, chat page loading, current summary overwrite, label separation, follow-up state transitions, human takeover expiry, and frosted/real QR replacement with a fake WAHA/AI endpoint or recorded test responses.

- [ ] **Step 4: Verify persistence and clean up only the disposable test resources**

Restart the test container, confirm `PENDING` tasks and current summary survive, confirm `UNKNOWN` tasks are not resent, then remove only the temporary container, project, and volume. Leave the production `1.0.2` container untouched.

- [ ] **Step 5: Commit final test fixtures or documentation changes**

```bash
git add tests
git commit -m "test: cover chat engagement release acceptance"
```

### Task 9: Prepare the 1.0.3 release handoff

**Files:**
- Modify: `panel-release.json` only if the final build metadata differs from Task 7
- Create: `docs/releases/1.0.3.md`

- [ ] **Step 1: Write the release notes**

Document the date, one-time follow-up modes and skip rules, customer label separation, current-summary overwrite behavior, five-hour takeover recovery, frosted QR placeholder, migration `007`, and the explicit non-changes to WAHA/network/Nginx.

- [ ] **Step 2: Verify release notes contain no sensitive data**

Run: `rg -n "(token|secret|password|api[_ -]?key|Bearer|固定文案全文|客户消息全文)" docs/releases/1.0.3.md`

Expected: no credentials or customer content are present.

- [ ] **Step 3: Commit release notes and tag the source**

```bash
git add docs/releases/1.0.3.md panel-release.json
git commit -m "docs: add waha-panel 1.0.3 release notes"
git tag v1.0.3
```

- [ ] **Step 4: Publish only after the implementation review gate**

Push the reviewed commit/tag and build `cangnan88/waha-panel:1.0.3`; update the BT Docker deployment only after the image pull, migration, health, and browser acceptance checks pass. Keep `1.0.2` available for rollback and never overwrite that tag.
