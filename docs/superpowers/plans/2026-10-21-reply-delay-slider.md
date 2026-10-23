# 自动回复延迟滑杆实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将自动回复的固定等待常量改为按 WhatsApp 技术会话保存的 0–120 延迟强度滑杆，并保持 100 时的现有默认行为。

**Architecture:** 复用 `session_settings` 键值表，不新增延迟专用表。`PanelState.settings_payload/_save_settings` 负责校验和持久化 `auto_reply_delay_scale`；`_reply_delays` 按比例缩放热对话、冷对话和输入模拟区间；设置页通过原生 range 控件展示和保存。

**Tech Stack:** Python 3.12、SQLite、原生 HTML/JavaScript、现有 unittest 和前端语法检查。

**Spec:** `docs/superpowers/specs/2026-10-21-global-translation-iptv-delay-design.md`

## Global Constraints

- 设置键为 `auto_reply_delay_scale`，整数范围 `0..120`，默认 `100`。
- `100` 必须保持当前延迟：热对话 1–5 秒、冷对话 20–100 秒、输入模拟 6–15 秒。
- `0` 必须让这三段等待均为 0；其他值按 `value / 100` 缩放。
- 设置按当前 WhatsApp 技术会话隔离；旧会话缺少键时读取默认 100。
- 只影响标准 AI 自动回复路径，不改变人工发送、人工接管、跟进任务和 IPTV 试用状态机。
- 服务端必须拒绝非法值或明确归一化到边界，不能保存负数、浮点或超过 120 的值。
- 页面必须支持键盘、触控和 `aria-valuenow`；保存失败时恢复服务端旧值。

## Review Focus

- 旧数据库缺少设置键；测试读取默认 100 且不会覆盖其他设置。
- `0` 和 `120` 边界；测试不出现负睡眠时间和意外长等待。
- 多会话隔离；测试 A 会话保存不会改变 B 会话。
- 保存失败与脏状态；测试服务端拒绝后控件恢复旧值。
- 非 AI 路径；测试人工发送、跟进任务和 IPTV 试用不会调用新的延迟比例。

### Task 1: Add backend setting defaults and validation

**Files:**
- Modify: `panel/app.py:130-175,1241-1345`
- Test: `tests/test_chat_upgrade.py` (`SettingsAndContextTests`, `SettingsApiTests`)

**Interfaces:**
- `PanelState.settings_payload(session_name) -> dict` adds integer `auto_reply_delay_scale`.
- `PanelState._save_settings(payload, session_name)` accepts and validates `auto_reply_delay_scale`.
- Missing or corrupt stored values resolve to `100` without modifying unrelated settings.

- [ ] **Step 1: Write failing setting tests**

Add tests for:

- default value is 100 on fresh and old databases;
- values 0, 50, 100, and 120 round-trip per session;
- negative, decimal, non-numeric, and 121 values are rejected without overwriting the previous value;
- session A and session B retain different values.

- [ ] **Step 2: Run focused tests and verify failure**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade.SettingsAndContextTests.test_reply_delay_scale_defaults_and_round_trips_per_session
```

Expected: FAIL because the setting is not present in the payload or save validator.

- [ ] **Step 3: Add default and validation**

Add a default value to the existing settings default path. In `_save_settings`, parse only integer strings/integers, enforce `0 <= value <= 120`, and write the normalized decimal string to `session_settings`. Keep legacy `settings` synchronization for the default session.

- [ ] **Step 4: Run all settings tests**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade.SettingsAndContextTests tests.test_chat_upgrade.SettingsApiTests
```

Expected: PASS.

- [ ] **Step 5: Commit the backend setting slice**

```powershell
git add panel/app.py tests/test_chat_upgrade.py
git commit -m "feat: persist per-session reply delay scale"
```

### Task 2: Apply the delay scale to the reply algorithm

**Files:**
- Modify: `panel/app.py:2121-2130`
- Test: `tests/test_auto_reply_policy.py`

**Interfaces:**
- `_reply_delays(self, reply, is_hot, session_name=DEFAULT_SESSION_NAME) -> tuple[float, float]` reads the named session's `auto_reply_delay_scale` and returns pre-delay and typing-delay seconds.
- `_wait_before_reply(self, reply, is_hot, session_name=DEFAULT_SESSION_NAME)` passes the session through while keeping its existing cancellation and send-order behavior.

- [ ] **Step 1: Add failing delay algorithm tests**

Use a deterministic `uniform_fn` that records bounds. Assert:

```python
scale=100 -> hot (1, 5), typing (6, 15)
scale=100 -> cold (20, 100), typing (6, 15)
scale=0   -> hot/cold/typing all (0, 0)
scale=50  -> hot (0.5, 2.5), cold (10, 50), typing (3, 7.5)
scale=120 -> hot (1.2, 6), cold (24, 120), typing (7.2, 18)
```

Also assert the selected session is used when two sessions process messages.

- [ ] **Step 2: Run the algorithm tests and verify failure**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_auto_reply_policy.AutoReplyPolicyTests.test_reply_delay_scale_preserves_current_default_ranges
```

Expected: FAIL because `_reply_delays` still uses fixed constants.

- [ ] **Step 3: Implement proportional delay calculation**

Add the `session_name` parameter to `_reply_delays` and `_wait_before_reply`, update the auto-reply call at `panel/app.py:2349` to pass the current session, read the setting through the existing session settings loader, normalize missing/corrupt values to 100, compute `ratio = value / 100.0`, and pass non-negative bounds to `uniform_fn`. Do not change the ticket check or the `sleep_fn` call ordering.

- [ ] **Step 4: Add non-AI path regression tests**

Assert manual sends, scheduled follow-up tasks, and IPTV trial execution do not call `_wait_before_reply` merely because the delay setting exists. Keep existing tests for manual/automation send idempotency intact.

- [ ] **Step 5: Run auto-reply and engagement tests**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_auto_reply_policy tests.test_chat_engagement tests.test_iptv_integration
```

Expected: PASS.

- [ ] **Step 6: Commit the algorithm slice**

```powershell
git add panel/app.py tests/test_auto_reply_policy.py
git commit -m "feat: scale automatic reply delays by session setting"
```

### Task 3: Add the settings-page range control

**Files:**
- Modify: `panel/app.py:3110-3235` (settings markup and controller)
- Test: `tests/test_chat_upgrade.py`, `tests/frontend_syntax.test.mjs`

**Interfaces:**
- Settings markup contains `id="replyDelayScale"`, `type="range"`, `min="0"`, `max="120"`, `step="5"`.
- Settings controller reads `data.auto_reply_delay_scale`, updates the visible value, and sends `auto_reply_delay_scale` in the existing `/api/settings` POST body.
- Save failure restores the previously loaded value and keeps the dirty-state indicator meaningful.

- [ ] **Step 1: Add failing HTML/JS contract tests**

Assert the settings page exposes the slider, value output, accessible label, and save payload key. Add a test that the displayed value updates on `input` without requiring a save.

- [ ] **Step 2: Run the page contract test and verify failure**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade.SettingsApiTests.test_settings_page_exposes_reply_delay_slider
```

Expected: FAIL because the slider and payload key are absent.

- [ ] **Step 3: Implement the range control and controller binding**

Place the control in the existing reply-rules area. Use a text output such as `100 · 当前默认`, set `aria-valuetext`, and keep the control usable in both daylight and night themes. Do not add a second settings form or new endpoint.

- [ ] **Step 4: Run settings page and syntax checks**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade.SettingsApiTests
node scripts/check_frontend_syntax.mjs
node --test tests/frontend_syntax.test.mjs
```

Expected: PASS.

- [ ] **Step 5: Commit the UI slice**

```powershell
git add panel/app.py tests/test_chat_upgrade.py tests/frontend_syntax.test.mjs
git commit -m "feat: add adjustable reply delay slider"
```

### Task 4: Documentation and full delay verification

**Files:**
- Modify: `README.zh-CN.md`, `INSTALL.zh-CN.md`, `docs/releases/1.0.18.md` (create when release notes are requested)
- Test/verification: settings, auto-reply, chat, IPTV, frontend suites

- [ ] **Step 1: Document the setting semantics**

Document the range 0–120, the default 100, exact hot/cold/typing formulas, per-session scope, and the fact that manual sends, follow-ups and IPTV state handling are unaffected.

- [ ] **Step 2: Run the complete local regression suite**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -p 'test_*.py'
node scripts/check_frontend_syntax.mjs
node --test tests/frontend_syntax.test.mjs
```

Expected: all applicable Python and frontend tests pass.

- [ ] **Step 3: Rehearse fresh and upgraded database settings**

Create a temporary fresh database and a copy initialized at 1.0.17, assert the slider reads 100 in both cases, and assert existing settings remain unchanged.

- [ ] **Step 4: Run diff and status checks**

```powershell
git diff --check HEAD~4..HEAD
git status --short --branch
```

Expected: no whitespace errors; only intended committed changes plus pre-existing untracked plan documents.

- [ ] **Step 5: Commit documentation**

```powershell
git add README.zh-CN.md INSTALL.zh-CN.md
git commit -m "docs: document adjustable automatic reply delay"
```

## Rollback

- Revert the slider UI and delay algorithm commits, or remove `auto_reply_delay_scale` from session settings to restore the code's default 100 behavior.
- Do not restore the whole SQLite database for a settings-only rollback; preserve messages, IPTV trials, labels and translation caches.
- If a stored value is malformed during rollback, the safe read default remains 100 and automatic replies use the pre-change timing ranges.

## Execution Handoff

This plan is the reply-delay track from the approved design. It deliberately reuses `session_settings` and does not add a database migration. It should be executed after the translation and IPTV tracks have landed, then serve as the final full-regression gate because it touches the shared settings page and `PanelState`.

Plan complete and saved to `docs/superpowers/plans/2026-10-21-reply-delay-slider.md`. Review it together with the two companion plans before implementation starts.
