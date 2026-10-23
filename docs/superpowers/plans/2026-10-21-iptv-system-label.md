# IPTV 测试用户系统标签实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 IPTV 试用消息实际发送成功后，给当前客户幂等添加只读系统标签 `IPTV测试过`。

**Architecture:** 不修改现有 `customer_labels` 的 `MANUAL/AI` 检查约束，新增独立的 `customer_system_labels` 表和 ChatService 内部写入接口。IPTV 自动回复流程在 `mark_delivery(..., True)` 成功后调用该接口；标签读取和页面展示统一合并三类标签，但系统标签不开放 HTTP 修改。

**Tech Stack:** Python 3.12、SQLite、现有 ChatService/IptvService、原生 HTML/JavaScript、unittest。

**Spec:** `docs/superpowers/specs/2026-10-21-global-translation-iptv-delay-design.md`

## Global Constraints

- 固定系统标签文字为 `IPTV测试过`。
- 只有供应商创建/复用成功、消息发送成功且 `mark_delivery(..., True)` 完成后才写入。
- `creating`、`created`、`unknown`、`failed`、`cancelled` 或发送失败不得写入。
- 同一客户跨会话复用时，成功发送的当前会话也要显示该标签。
- 系统标签与手动标签、AI 标签分表存储；AI 总结不得删除系统标签。
- 系统标签不提供新增、编辑、删除 HTTP 接口。
- 标签写入失败不能撤销已经成功的 WhatsApp 消息，只记录脱敏告警并允许后续成功事件补写。
- 不向标签、日志或 HTTP 响应写入 IPTV 账号、密码、M3U 地址或手机号。

## Review Focus

- 发送成功与 `created` 状态之间的边界；测试发送失败和结果未知不打标签。
- 重复 webhook/重复复用；测试唯一约束和 `INSERT OR IGNORE` 不产生重复。
- 跨会话同一手机号；测试当前会话标签与跨会话资格复用同时成立。
- AI 总结和手动标签更新；测试两种操作不会删除系统标签。
- 标签读取错误或写入失败；测试消息已发送时错误不会被错误地回滚成未发送。

### Task 1: 创建系统标签迁移

**Files:**
- Create: `panel/migrations/012_customer_system_labels.sql`
- Modify: `panel/app.py:441-532` (`init_db` migration order)
- Test: `tests/test_chat_engagement.py` or `tests/test_iptv_service.py`

**Interfaces:**
- Creates `customer_system_labels(id, session_name, chat_key_hmac, label, created_at, updated_at)`.
- Enforces `UNIQUE(session_name, chat_key_hmac, label)`.
- Creates an index for `(session_name, chat_key_hmac, updated_at)`.

- [ ] **Step 1: Write the failing schema test**

Add `test_system_label_schema_is_created_and_idempotent` that calls `init_db` twice and asserts the table, unique constraint, and index exist while existing `customer_labels` rows remain unchanged.

- [ ] **Step 2: Run the test and verify it fails**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_engagement.ChatServiceEngagementTests.test_system_label_schema_is_created_and_idempotent
```

Expected: FAIL because migration 012 does not exist.

- [ ] **Step 3: Add migration 012 and load it after migration 011**

Keep the migration idempotent and additive. Do not alter the `customer_labels` table or its source check constraint.

- [ ] **Step 4: Run the schema test**

Expected: PASS on a fresh and twice-initialized database.

- [ ] **Step 5: Commit the migration slice**

```powershell
git add panel/migrations/012_customer_system_labels.sql panel/app.py tests/test_chat_engagement.py
git commit -m "feat: add durable IPTV system labels"
```

### Task 2: Add ChatService system-label APIs

**Files:**
- Modify: `panel/chat_service.py:538-590,904-1035`
- Test: `tests/test_chat_engagement.py`, `tests/test_chat_upgrade.py`

**Interfaces:**
- `ChatService.add_system_label_by_chat_id(session: str, chat_id: str, label: str) -> None` validates the chat ID through existing session normalization and inserts idempotently.
- `ChatService.customer_labels(session, chat_ref) -> {"manual": list[str], "ai": list[str], "system": list[str]}`.
- `ChatService.customer_label_records(session, chat_ref) -> {"manual": list[dict], "ai": list[dict], "system": list[dict]}`.
- `_overview_metadata` returns merged label entries with `source` values `manual`, `ai`, or `system`.

- [ ] **Step 1: Write failing ChatService tests**

Add tests for:

- system label insertion is idempotent;
- system label is returned from both label APIs;
- manual and AI labels remain unchanged;
- invalid/empty/overlong system labels are rejected internally;
- system labels are isolated by session and chat HMAC.

- [ ] **Step 2: Run focused tests and verify failure**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_engagement.ChatServiceEngagementTests.test_system_label_is_idempotent_and_isolated
```

Expected: FAIL because the system-label methods and `system` result key are absent.

- [ ] **Step 3: Implement internal write and merged reads**

Reuse `_label_value` for validation, calculate the existing chat HMAC, and use `INSERT OR IGNORE`. Read system rows separately and merge them after manual/AI rows. Never allow the public manual-label mutation methods to accept `SYSTEM` or `system` as a writable source.

- [ ] **Step 4: Update overview metadata**

Extend `_overview_metadata` so chat-list chips contain a stable `id`, `source="system"`, and sanitized label. Preserve the existing manual-first ordering and visible-label cap.

- [ ] **Step 5: Run ChatService and overview tests**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_engagement.ChatServiceEngagementTests tests.test_chat_upgrade.AvatarAndMetadataTests.test_overview_orders_manual_labels_first_and_caps_visible_labels
```

Expected: PASS.

- [ ] **Step 6: Commit the ChatService slice**

```powershell
git add panel/chat_service.py tests/test_chat_engagement.py tests/test_chat_upgrade.py
git commit -m "feat: expose read-only system customer labels"
```

### Task 3: Add the successful IPTV delivery hook

**Files:**
- Modify: `panel/app.py:2064-2105`
- Test: `tests/test_iptv_integration.py`

**Interfaces:**
- `_execute_iptv_trial(...)` calls `self.chat.add_system_label_by_chat_id(session, chat_id, "IPTV测试过")` only after `mark_delivery(result["id"], True)` returns.
- Label write failures are logged with an event such as `iptv.system_label_failed` and a redacted error code; they do not change the already successful send response.

- [ ] **Step 1: Add failing integration tests**

Extend `IptvIntegrationTests` with:

- `test_successful_trial_adds_system_label_after_delivery`;
- `test_delivery_failure_does_not_add_system_label`;
- `test_unknown_creation_does_not_add_system_label`;
- `test_reused_trial_in_second_session_adds_current_session_label`;
- `test_repeated_success_is_idempotent`.

Assert the database contains exactly one `IPTV测试过` row for each successful `(session, chat)` and zero rows for non-success states.

- [ ] **Step 2: Run the new integration tests and verify failure**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_iptv_integration.IptvIntegrationTests.test_successful_trial_adds_system_label_after_delivery
```

Expected: FAIL because the IPTV path currently marks delivery but does not write a customer label.

- [ ] **Step 3: Implement the post-delivery hook**

Call the ChatService internal method after the successful delivery state transition. Wrap only the label write in a narrow `try/except`; do not catch or rewrite the preceding send exception. Log only the session and public error code.

- [ ] **Step 4: Run all IPTV integration tests**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_iptv_integration tests.test_iptv_service
```

Expected: PASS; existing credential-protection assertions remain unchanged.

- [ ] **Step 5: Commit the delivery hook**

```powershell
git add panel/app.py tests/test_iptv_integration.py
git commit -m "feat: tag customers after successful IPTV delivery"
```

### Task 4: Render system labels in the chat UI

**Files:**
- Modify: `panel/chat_page.py:45,110-125,135,256-260`
- Test: `tests/test_chat_features.py`, `tests/ui_acceptance.mjs`

**Interfaces:**
- Frontend label state adds `system: []` while preserving `manual` and `ai`.
- `renderLabelChips` maps `source="system"` to a read-only system style.
- `renderLabels` adds a “系统” group with no edit/delete actions.
- `syncCompactLabels` includes system labels in the visible chip list and overflow count.

- [ ] **Step 1: Add failing UI contract tests**

Assert the page contains:

- a system label group;
- the `system` result key in the label renderer;
- no system-label POST/PUT/DELETE path;
- visible text indicating the system group is read-only.

Add a browser fixture that returns a system label and confirms it appears in the chat header and label dialog without edit/delete buttons.

- [ ] **Step 2: Run the page contract test and verify failure**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_features.ChatPageRegressionTests.test_system_labels_are_read_only
```

Expected: FAIL because the page currently renders only manual and AI groups.

- [ ] **Step 3: Implement merged system-label rendering**

Keep manual label editing unchanged. Render system labels with a distinct non-editable style and accessible text. Ensure compact chips remain capped at four entries and preserve manual-first ordering.

- [ ] **Step 4: Run frontend checks**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_features.ChatPageRegressionTests
node scripts/check_frontend_syntax.mjs
node --test tests/frontend_syntax.test.mjs
```

Expected: PASS.

- [ ] **Step 5: Commit the UI slice**

```powershell
git add panel/chat_page.py tests/test_chat_features.py tests/ui_acceptance.mjs
git commit -m "feat: display read-only IPTV system labels"
```

### Task 5: Documentation and system-label verification

**Files:**
- Modify: `README.zh-CN.md`, `INSTALL.zh-CN.md`, `docs/iptv-trials.md`
- Test/verification: IPTV and chat test groups

- [ ] **Step 1: Document the label rule**

State that `IPTV测试过` is added only after a successful message delivery, is scoped to the current conversation, is read-only, and does not expose trial credentials. Document that failed/unknown supplier results do not add the tag.

- [ ] **Step 2: Run the combined label and IPTV tests**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_iptv_service tests.test_iptv_integration tests.test_chat_engagement tests.test_chat_upgrade
```

Expected: PASS and no credential-bearing output.

- [ ] **Step 3: Verify database and diff integrity**

```powershell
git diff --check HEAD~4..HEAD
git status --short --branch
```

Expected: no whitespace errors; only intended committed changes plus pre-existing untracked plan documents.

- [ ] **Step 4: Commit documentation**

```powershell
git add README.zh-CN.md INSTALL.zh-CN.md docs/iptv-trials.md
git commit -m "docs: document IPTV system customer labels"
```

## Rollback

- Revert the system-label hook and UI commits; keep `customer_system_labels` so a later code update can reuse its rows.
- If the old code is restored, it will ignore the additive table and existing `customer_labels` rows remain intact.
- Never delete `iptv_trials` or restore an older database over successful trial records just to remove a display tag.
- If a tag write fails after a successful send, leave the trial state as sent and repair the tag through a later idempotent success path.

## Execution Handoff

This plan is the IPTV system-label track from the approved design. It owns migration 012, ChatService label merging, the post-delivery hook, and read-only UI rendering. It assumes migration 011 from the translation-provider track is loaded before migration 012; no existing label table is rebuilt.

Plan complete and saved to `docs/superpowers/plans/2026-10-21-iptv-system-label.md`. Review it together with the translation-provider and reply-delay plans before implementation starts.
