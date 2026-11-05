# 面板自动化、凭据展示与错误诊断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在面板 1.0.20 代码线上修复 AI/客户记忆错误诊断、人工接管恢复设置、手动 IPTV 卡密交付闭环，并扩展跟进时间滑杆。

**Architecture:** 沿用现有 `PanelState`、`ChatService`、`IptvService`、`ChatAutomationService` 和 SQLite 幂等账本，在现有迁移和回调边界上做增量修改。新增会话设置和跟进延迟 code 通过兼容迁移保存；卡密交付成功由统一回调幂等写入系统标签并创建 12 小时体验跟进。AI 错误在共享调用边界映射为稳定错误码，客户记忆 worker 只对可重试错误退避。

**Tech Stack:** Python 3.12、`sqlite3`、`threading`、现有 `DataCipher`/`ChatService`、内联 vanilla JavaScript、unittest、Node 语法检查。

**Spec:** `docs/superpowers/specs/2026-11-05-panel-automation-credentials-design.md`

## Global Constraints

- 目标工作树为 `github-waha-panel.worktrees/chat-engagement`，不修改根目录旧快照。
- 不删除 Docker 卷、SQLite 数据、WhatsApp 会话或媒体；不在实现阶段重启生产容器或发布镜像。
- 真实密码、API Key、Token、完整卡密 URL、客户聊天 ID 和固定跟进文案不得进入日志、浏览器响应或测试输出。
- 延迟 code 固定为 `1h`、`2h`、`3h`、`6h`、`12h`、`24h`、`3d`、`7d`、`15d`；显示文案不能作为 API 值。
- 人工恢复 code 固定为 `10m`、`30m`、`1h`、`5h`，旧会话默认 `5h`。
- 卡密系统标签必须在发送状态确认成功后写入，发送失败不得提前创建标签或体验跟进。

## Review Focus

- 旧 `follow_up_tasks` 表含有旧 CHECK 约束时迁移必须保留每一行和 `client_request_id`；由 Task 1 的迁移回归测试覆盖。
- 重复 webhook、发送重试和重复 worker 回调不能重复标签或跟进；由 Task 4 的幂等测试覆盖。
- 客户上下文中客服中文和内部行不能覆盖客户语言；由 Task 3 的入站优先测试覆盖。
- 402、配置缺失和超时必须分别影响重试策略且不泄露上游响应；由 Task 5 的错误分类/worker 测试覆盖。
- 安装脚本 stdout 只能包含掩码凭据和受限路径，已有安装不得重新生成密钥；由 Task 6 的脚本静态/执行合同测试覆盖。

---

### Task 1: Extend schema and stable setting contracts

**Files:**
- Create: `panel/migrations/017_follow_up_delay_codes.sql`
- Modify: `panel/app.py:DEFAULT_SETTINGS`, `init_db`, `settings_payload`, `_save_settings`
- Modify: `panel/chat_automation.py:DELAY_SECONDS`
- Test: `tests/test_chat_upgrade.py`, `tests/test_chat_engagement.py`

**Interfaces:**
- Produces `human_takeover_resume_delay` in the per-session settings payload with values `10m|30m|1h|5h`.
- Produces `DELAY_SECONDS` support for the nine follow-up codes while preserving the existing public task payload.
- Migration recreates `follow_up_tasks` only when its delay CHECK constraint lacks the new codes, copying all columns and indexes.

- [ ] **Step 1: Write failing migration and settings tests**

  Add tests that insert legacy follow-up rows, run `init_db` twice, assert the row and idempotency UUID remain, and assert all nine codes are accepted while an unknown code is rejected. Add per-session round-trip tests for `human_takeover_resume_delay`, a default of `5h`, and rejection without overwriting the previous value.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run:

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade tests.test_chat_engagement
  ```

  Expected: failures for the missing setting and unsupported short delay codes/legacy migration.

- [ ] **Step 3: Add the additive migration and setting validation**

  Add `017_follow_up_delay_codes.sql` with the same columns, indexes and states as `007_chat_engagement.sql`, but the expanded delay CHECK. In `init_db`, detect the existing table constraint through `PRAGMA table_info`/schema SQL and copy rows into the new table only when necessary. Add `human_takeover_resume_delay` to `DEFAULT_SETTINGS`, return it from `settings_payload`, and validate it in `_save_settings`.

- [ ] **Step 4: Run the focused tests and verify they pass**

  Re-run the command from Step 2. Expected: migration is idempotent, old rows survive, all delay codes calculate due times, and invalid settings are rejected.

- [ ] **Step 5: Commit the schema/settings slice**

  ```powershell
  git add panel/migrations/017_follow_up_delay_codes.sql panel/app.py tests/test_chat_upgrade.py tests/test_chat_engagement.py
  git commit -m "feat: add follow-up delays and takeover timeout setting"
  ```

### Task 2: Make manual takeover restoration configurable

**Files:**
- Modify: `panel/chat_service.py:_pause`, `_finish_send`
- Modify: `panel/app.py` settings page markup and inline settings JavaScript
- Test: `tests/test_chat_engagement.py`, `tests/test_chat_upgrade.py`

**Interfaces:**
- `ChatService` consumes a `settings_loader` callback compatible with `PanelState._settings` (or an equivalent constructor dependency) and computes the timeout from `human_takeover_resume_delay`.
- Successful manual text and image sends, plus takeover click, set `auto_resume_at` to `int(clock()) + configured_seconds`.

- [ ] **Step 1: Write failing timeout and UI tests**

  Assert the four setting codes map to 600, 1800, 3600 and 18000 seconds; takeover and successful manual text/image sends use the saved value; failed/unknown sends leave the timer unchanged. Assert the settings page exposes a labeled discrete range with all four labels, load/save wiring and a safe default.

- [ ] **Step 2: Run tests to verify the expected failures**

  Run:

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_engagement tests.test_chat_upgrade
  ```

  Expected: current hard-coded 18000 behavior fails for the other choices and the page contract is absent.

- [ ] **Step 3: Implement timeout lookup and settings slider**

  Pass the session settings loader when constructing `ChatService`, add one bounded helper for code-to-seconds conversion, and use it in `_pause` and `_finish_send`. Add a four-position `input[type=range]` with an output label to `settings_page()`, load/save `human_takeover_resume_delay`, and keep the existing automatic reply delay slider separate.

- [ ] **Step 4: Run timeout, settings and regression tests**

  Run the command from Step 2 plus `tests.test_auto_reply_policy`. Expected: all existing takeover and automatic reply tests remain green.

- [ ] **Step 5: Commit the takeover slice**

  ```powershell
  git add panel/chat_service.py panel/app.py tests/test_chat_engagement.py tests/test_chat_upgrade.py
  git commit -m "feat: configure human takeover auto resume"
  ```

### Task 3: Correct customer-language inference and expose follow-up delay slider

**Files:**
- Modify: `panel/iptv_service.py` language helpers
- Modify: `panel/chat_automation.py:DELAY_SECONDS`
- Modify: `panel/chat_page.py` follow-up dialog markup/state/submit handler
- Test: `tests/test_iptv_service.py`, `tests/test_chat_engagement.py`, `tests/test_chat_features.py`

**Interfaces:**
- Produces `infer_customer_language_from_history(history_or_context) -> str`, returning the most recent supported customer/inbound language or `en`.
- `ChatAutomationService.create_task()` accepts all nine delay codes and keeps existing `delay_code` in task responses.
- The follow-up UI submits `{delay_code, mode, fixed_copy}` and uses a discrete slider index/output label rather than display text.

- [ ] **Step 1: Write failing language, delay and UI tests**

  Add a history containing an English customer message followed by Chinese agent messages and assert `en`; add a Portuguese/Spanish/Arabic customer case and assert that language. Extend due-time tests to `1h`, `2h`, `3h`, `6h`, `12h`. Assert the page contains a range input, all nine labels, an output update function and submits the selected code.

- [ ] **Step 2: Run tests to verify they fail**

  Run:

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_iptv_service tests.test_chat_engagement tests.test_chat_features
  ```

  Expected: language selection still sees mixed context, short delay codes are rejected, and the page only contains a select with four old options.

- [ ] **Step 3: Implement inbound-first inference and slider contract**

  Parse structured history/context lines and retain only customer/inbound entries before language matching; preserve current-message-first behavior for automatic requests. Replace the follow-up select with an indexed range and a code/label array; keep keyboard accessibility, output text and existing AI/FIXED mode controls.

- [ ] **Step 4: Run focused tests and frontend syntax validation**

  Run the command from Step 2 and:

  ```powershell
  node scripts/check_frontend_syntax.mjs
  ```

  Expected: all language/delay/UI contract tests pass and JavaScript syntax remains valid.

- [ ] **Step 5: Commit the follow-up UI slice**

  ```powershell
  git add panel/iptv_service.py panel/chat_automation.py panel/chat_page.py tests/test_iptv_service.py tests/test_chat_engagement.py tests/test_chat_features.py
  git commit -m "feat: add short follow-up delay slider and customer language detection"
  ```

### Task 4: Close the manual IPTV delivery loop

**Files:**
- Modify: `panel/app.py` IPTV callbacks, manual-card route and background integration
- Modify: `panel/iptv_service.py` retry success callback shape
- Modify: `panel/chat_automation.py` optional idempotency key/context for system-created tasks
- Test: `tests/test_iptv_integration.py`, `tests/test_iptv_service.py`, `tests/test_chat_engagement.py`

**Interfaces:**
- Produces `PanelState.record_iptv_manual_delivery_success(session, chat_id, card) -> None`, which writes the system label and creates exactly one 12-hour FIXED follow-up using `client_request_id='iptv-manual-follow-up:<card_id>'`.
- `IptvService.retry_due_once()` invokes the same success callback after a manual send retry; automatic retry behavior remains unchanged.
- System-created follow-up tasks have a stable idempotency key derived from the manual card ID and never expose fixed copy in public task payloads.

- [ ] **Step 1: Write failing delivery-loop tests**

  Assert immediate manual card success writes `IPTV测试过` and exactly one `12h` task; repeat the callback and assert no duplicate task/label. Assert manual send retry success writes both records, while supplier creation failure or send failure before confirmation writes neither. Assert the fixed copy is encrypted and never appears in logs or API responses.

- [ ] **Step 2: Run the focused tests to verify failure**

  Run:

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_iptv_integration tests.test_iptv_service tests.test_chat_engagement
  ```

  Expected: manual cards have no success callback, no 12-hour task, and manual retries do not add the system label.

- [ ] **Step 3: Extend task creation with a stable idempotency key and implement the success callback**

  Add optional `client_request_id=None` to `ChatAutomationService.create_task()`. Validate a supplied value, use it instead of a random UUID, and on a UNIQUE conflict return the existing task when the session/chat/delay/mode payload matches. `PanelState.record_iptv_manual_delivery_success()` calls `chat.add_system_label_by_chat_id()` only after `mark_manual_delivery(..., True)`, then creates the task with `client_request_id='iptv-manual-follow-up:<card_id>'`. Create the fixed Chinese source copy only inside the encrypted follow-up row, let the existing `translate_follow_up()` produce customer-language output at due time, and pass the stable task request ID through the existing automated-send ledger.

- [ ] **Step 4: Wire immediate and retry delivery paths**

  Update the manual-card POST route and `IptvService.retry_due_once()` to invoke the callback only after confirmed delivery. Preserve the existing response shape `{status, card_id}` and never return the account.

- [ ] **Step 5: Run IPTV, automation and redaction tests**

  Re-run the command from Step 2 plus `tests.test_customer_memory` and the frontend syntax check. Expected: immediate and retry paths are idempotent and all existing automatic trial tests remain green.

- [ ] **Step 6: Commit the manual-card slice**

  ```powershell
  git add panel/app.py panel/iptv_service.py panel/chat_automation.py tests/test_iptv_integration.py tests/test_iptv_service.py tests/test_chat_engagement.py
  git commit -m "feat: label manual IPTV cards and schedule experience follow-up"
  ```

### Task 5: Classify AI upstream errors and stop deterministic memory retry loops

**Files:**
- Modify: `panel/app.py` shared AI request/error helpers and `_customer_memory_completion`
- Modify: `panel/customer_memory_service.py` retry classification/state transitions
- Test: `tests/test_customer_memory.py`, `tests/test_auto_reply_policy.py`, `tests/test_chat_upgrade.py`

**Interfaces:**
- Produces `AIUpstreamError(code, status, retryable)` with stable codes including `AI_PAYMENT_REQUIRED`, `AI_CONFIG_MISSING`, `AI_CONNECTION_ERROR`, `AI_UPSTREAM_ERROR` and `AI_RESPONSE_INVALID`.
- `_customer_memory_completion()` raises the typed error without exposing the key or response body.
- Memory jobs with deterministic codes finish as `FAILED`/paused with the code recorded; transient connection/5xx failures remain pending with bounded backoff.

- [ ] **Step 1: Write failing 402 and worker retry tests**

  Use a fake `HTTPError(402, ...)` and assert automatic reply logs `AI_PAYMENT_REQUIRED` without the secret. Enqueue a memory job with the same error and assert `conversation_memory_jobs.last_error_code` is stable, no immediate retry is scheduled, and the previous encrypted memory remains readable. Add a timeout/503 case that still schedules a retry.

- [ ] **Step 2: Run tests to verify expected failure**

  Run:

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_customer_memory tests.test_auto_reply_policy tests.test_chat_upgrade
  ```

  Expected: current code collapses 402 to `ai_error` and always writes `MEMORY_WORKER_FAILED` with another pending retry.

- [ ] **Step 3: Implement typed upstream error mapping**

  Add one redacted mapping helper around the OpenAI-compatible request. Map HTTP 402 to `AI_PAYMENT_REQUIRED`, 401/403 to configuration/access failure, 408/429/5xx and transport errors to retryable upstream/connection codes, and malformed successful envelopes to `AI_RESPONSE_INVALID`. Keep response bodies and credentials out of logs.

- [ ] **Step 4: Update memory worker state transitions**

  Preserve the old encrypted memory on failure, write the typed error code, mark deterministic jobs terminal/paused without another due retry, and retain exponential retry only for `retryable=True`. Update system log copy to include a safe remediation hint for payment-required errors.

- [ ] **Step 5: Run focused and full backend tests**

  Run the command from Step 2, then the complete unittest suite. Expected: 402 and memory-worker tests pass, all prior auto-reply behavior remains unchanged.

- [ ] **Step 6: Commit the error-diagnosis slice**

  ```powershell
  git add panel/app.py panel/customer_memory_service.py tests/test_customer_memory.py tests/test_auto_reply_policy.py tests/test_chat_upgrade.py
  git commit -m "fix: classify AI payment errors and stop memory retry storms"
  ```

### Task 6: Secure installer credential guidance and documentation

**Files:**
- Modify: `install.sh`, `install.ps1`
- Modify: `README.zh-CN.md`, `INSTALL.zh-CN.md`, `docs/iptv-trials.md`
- Test: add `tests/test_install_output.py` or a shell/PowerShell contract test under `tests/`

**Interfaces:**
- First-install output shows panel/WAHA usernames, masked secret values and restricted local file paths only.
- Existing-install output states credentials were preserved and points to the protected files without printing secret contents.

- [ ] **Step 1: Write failing installer-output tests**

  Assert the scripts mention WAHA username, masked password/API Key labels and the protected credential paths, while the output templates do not interpolate raw secret variables. Assert the existing-install branch does not generate or print new credentials.

- [ ] **Step 2: Run tests to verify failure**

  Run the repository's shell/PowerShell contract command and inspect the expected missing output tokens.

- [ ] **Step 3: Implement masked first-install and preserved-install output**

  Add a small shell/PowerShell masking helper that shows only a short prefix/suffix, keep real values in the existing `secrets/` files with restrictive permissions, and update installation documentation to explain how an operator can read the files locally without copying them into logs or chat.

- [ ] **Step 4: Run installer contract tests and docs checks**

  Verify no raw credential variable is printed by the scripts, run `git diff --check`, and confirm all documentation describes the secure output behavior.

- [ ] **Step 5: Commit installer/documentation changes**

  ```powershell
  git add install.sh install.ps1 README.zh-CN.md INSTALL.zh-CN.md docs/iptv-trials.md tests/test_install_output.py
  git commit -m "docs: secure installer credentials and automation settings"
  ```

### Task 7: Full verification and production-read-only evidence

**Files:**
- Verify only; no new production code.

- [ ] **Step 1: Run the complete backend suite**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v
  ```

  Expected: all tests pass with no secret values in output.

- [ ] **Step 2: Run frontend syntax and static checks**

  ```powershell
  node scripts/check_frontend_syntax.mjs
  git diff --check
  git status --short --branch
  ```

  Expected: syntax validation and diff check pass; only intended files are changed.

- [ ] **Step 3: Rehearse fresh and upgrade migrations**

  Create temporary SQLite databases for a fresh install and an old `follow_up_tasks` schema, run `init_db` twice, assert all old rows/settings remain and all nine delay codes are accepted.

- [ ] **Step 4: Perform read-only production checks**

  Use the existing Baota read-only tools to verify both containers remain running, inspect only sanitized startup/container metadata, and record that no production restart or deployment was performed.

- [ ] **Step 5: Review final diff and delivery evidence**

  Confirm that the 402 remediation is configuration guidance plus safe classification, not a claim that external billing was repaired; report local test evidence separately from any future deployment gate.
