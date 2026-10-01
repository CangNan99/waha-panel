# Customer Memory and Current Chat Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 WAHA / WhatsAPP AI 管理面板中实现按客户动态记忆、当前聊天阿里云双行翻译、最近聊天滚动位置保持和状态呼吸动画，并保持现有自动回复与人工接管流程兼容。

**Architecture:** 在现有 SQLite 多会话设置和消息归档上增加兼容迁移。客户记忆由独立的 `CustomerMemoryService` 异步合并任务维护，阿里云翻译由独立的 `AliyunTranslationService` 负责配置、限流、缓存和批量翻译；`PanelState` 负责把两者接入自动回复和 HTTP 路由。聊天页只在当前聊天明确开启翻译且页面可见时调用翻译接口，列表渲染通过 chat 引用锚点恢复滚动位置。

**Tech Stack:** Python 3.12、SQLite、`cryptography` 现有 `DataCipher`、阿里云 `alibabacloud_alimt20181012==1.1.0`、原生 HTML/CSS/JavaScript、现有 `unittest` 和前端语法检查脚本。

**Spec:** `docs/superpowers/specs/2026-10-01-customer-memory-aliyun-translation-design.md`

## Global Constraints

- 客户记忆开关位于“自动回复设置”的“回复规则”区域，默认关闭；关闭后保留已有记忆，停止更新和使用。
- 全局翻译按客户独立、默认关闭；切换客户不继承开关，刷新页面后恢复关闭。
- 全局翻译只处理当前打开聊天中已加载的消息；未打开聊天不得触发机器翻译请求。
- 客户、AI 和人工消息的原文必须保留；非中文消息在原文下方显示 `中文：译文`，纯中文消息不重复显示。
- 全局翻译只能调用阿里云机器翻译接口，不能回退到现有 AI 翻译接口。
- 记忆使用原始对话内容，不把机器译文写入客户记忆，不修改模型权重。
- `conversation_messages.origin` 取值为 `manual`、`auto_ai`、`automation` 或 `unknown`；历史行默认 `unknown`。
- 阿里云调用使用 `SourceLanguage=auto`、`TargetLanguage=zh`、`FormatType=text`；单条文本在官方 `<5000` 字符限制内，服务端采用 4800 字符安全上限。
- 阿里云翻译并发上限为 5，服务端仍遵守官方 50 QPS 限制；超限使用短退避，不阻塞消息加载。
- 凭据、客户原文、译文和记忆正文不得出现在普通日志、错误响应或测试输出中；AccessKey Secret 只保存加密值。
- 迁移必须可重复执行，先备份再迁移；本计划不包含远程部署、容器重启或生产凭据修改。
- 每个任务先写失败测试，再实现最小代码，再运行针对性测试并提交一次小提交。

## Review Focus

- 未打开或已切换的聊天不能触发翻译请求，过期响应不能写入新客户；由任务 5 的请求代次和路由测试覆盖。
- 纯中文、混合语言、空文本和超长文本必须得到可预测的 `SKIPPED`、`READY` 或 `FAILED` 状态；由任务 3 的语言判断和边界测试覆盖。
- 记忆关闭、AI 配置缺失、摘要失败时必须保留现有自动回复能力并安全降级；由任务 2 和任务 4 的服务测试覆盖。
- 旧数据库没有 `origin`、记忆表或翻译表时必须无损启动，已有设置不能被默认值覆盖；由任务 1 的迁移测试覆盖。
- 减少动态模式下动画必须停止，人工接管和翻译状态仍需通过文字和语义属性表达；由任务 5 的页面契约测试覆盖。

## File Map

- Create: `panel/migrations/008_customer_memory_translation.sql` — 客户记忆任务和机器翻译缓存表。
- Create: `panel/customer_memory_service.py` — 客户记忆加密读取、异步合并任务、摘要生成和上下文注入。
- Create: `panel/aliyun_translation_service.py` — 阿里云配置、SDK 调用、中文判断、缓存和批量翻译。
- Modify: `panel/requirements.txt` — 增加固定版本阿里云机器翻译 SDK。
- Modify: `panel/app.py` — 迁移加载、设置字段、消息来源归档、服务生命周期、自动回复上下文和 HTTP 路由。
- Modify: `panel/chat_service.py` — 为人工、自动化和图片发送归档传递明确来源。
- Modify: `panel/chat_page.py` — 列表滚动锚点、聊天头部翻译开关、双行消息、请求取消和呼吸动画。
- Modify: `tests/test_chat_upgrade.py` — 设置字段、迁移和自动回复记忆集成测试。
- Modify: `tests/test_chat_features.py` — 聊天页面 DOM/脚本契约、滚动锚点和动画契约测试。
- Create: `tests/test_customer_memory.py` — 客户记忆服务单元测试。
- Create: `tests/test_aliyun_translation.py` — 阿里云服务单元测试。
- Modify: `tests/frontend_syntax.test.mjs` only if the existing syntax test needs an explicit new page contract; otherwise run it unchanged.
- Modify: `README.zh-CN.md`, `INSTALL.zh-CN.md` — 设置、密钥、备份、回滚和当前聊天翻译说明。

Migration 008 must also create the single-row `aliyun_translation_settings` table with `id=1`, `endpoint`, `region_id`, `access_key_id`, encrypted `access_key_secret_ciphertext`, `last_test_status`, `last_test_at`, and `updated_at`. This makes the Alibaba configuration boundary explicit and keeps it separate from the existing AI `translation_settings` table.

---

### Task 1: Add compatible schema and automatic-reply memory setting

**Files:**
- Create: `panel/migrations/008_customer_memory_translation.sql`
- Modify: `panel/app.py:DEFAULT_SETTINGS`, `init_db`, `settings_payload`, `save_settings`
- Test: `tests/test_chat_upgrade.py`

**Interfaces:**
- Produces `session_settings.customer_memory_enabled` as a string boolean with default `0`.
- Produces `conversation_messages.origin` with default `unknown`.
- Produces tables `customer_memories`, `conversation_memory_jobs`, and `machine_message_translations` for later services.

- [ ] **Step 1: Write failing migration and settings tests**

  Add tests that assert:

  - a fresh database returns `customer_memory_enabled == False`;
  - the setting round-trips independently for `default` and `sales` sessions;
  - an old database upgraded through `init_db` has `origin == 'unknown'` for an existing message;
  - the three new tables exist after migration and contain no seeded customer data.

- [ ] **Step 2: Run the focused tests and verify they fail**

  Run:

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade
  ```

  Expected: failures for the missing setting field, missing origin column, and missing tables.

- [ ] **Step 3: Implement the idempotent schema and setting field**

  Add the new default to `DEFAULT_SETTINGS`, include the boolean in `settings_payload`, validate it with the existing `as_bool` convention in `save_settings`, and include it in the settings page payload. Execute migration 008 after migration 007. Use `_add_column_if_missing(connection, 'conversation_messages', 'origin TEXT NOT NULL DEFAULT \'unknown\'')` so old databases and fresh databases follow the same path. Define encrypted text columns for memory and translation bodies, composite unique keys for session/customer and session/chat/message/source fingerprints, and indexes for pending jobs and chat lookups.

- [ ] **Step 4: Run the focused tests and verify they pass**

  Run the same command from Step 2. Expected: all existing upgrade tests plus the new migration/settings tests pass.

- [ ] **Step 5: Commit the schema/settings slice**

  ```powershell
  git add panel/migrations/008_customer_memory_translation.sql panel/app.py tests/test_chat_upgrade.py
  git commit -m "feat: add memory setting and translation storage schema"
  ```

### Task 2: Implement customer memory service and message provenance

**Files:**
- Create: `panel/customer_memory_service.py`
- Modify: `panel/app.py:_archive_message`, `_record_outbound_message`, `_send_text`, `_ai_reply`, `enable_chat_services`, `stop_chat_services`
- Modify: `panel/chat_service.py:_notify_outbound`, `send_text`, `send_automated_text`, `send_image`
- Test: `tests/test_customer_memory.py`, `tests/test_chat_upgrade.py`

**Interfaces:**
- `CustomerMemoryService(database_path, cipher, settings_loader, completion_fn, logger=None, clock=None)`
- `start() -> None`, `stop(timeout=2.0) -> None`
- `enqueue(session_name: str, chat_id: str) -> None`
- `get(session_name: str, chat_id: str) -> dict`
- `rebuild(session_name: str, chat_id: str) -> dict`
- `clear(session_name: str, chat_id: str) -> dict`
- `context_for_reply(session_name: str, chat_id: str) -> str`
- `completion_fn(settings: dict, system_prompt: str, user_prompt: str) -> str`; the service owns prompt construction and JSON validation, while `PanelState` supplies the existing OpenAI-compatible completion transport.
- `get(...)` returns `{"enabled": bool, "status": str, "version": int, "memory": dict, "updated_at": int | None}` and never returns ciphertext.
- `ChatService.outbound_recorder` receives an optional `origin` keyword while remaining compatible with existing callbacks.

- [ ] **Step 1: Write failing service tests**

  Cover:

  - a message with `origin='manual'` and an AI message with `origin='auto_ai'` are both included in the next summary request;
  - duplicate enqueue calls produce one pending job per customer;
  - disabled customer memory does not enqueue or read a memory context;
  - a failed completion keeps the previous encrypted memory and records a retryable error state;
  - `context_for_reply` returns bounded, decrypted memory text and returns an empty string when no memory exists;
  - clearing a customer removes memory and pending job rows but leaves `conversation_messages` intact.

- [ ] **Step 2: Run the new service tests and verify they fail**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_customer_memory
  ```

  Expected: import or method failures because the service and provenance plumbing do not exist.

- [ ] **Step 3: Implement `CustomerMemoryService`**

  Use a daemon worker with a condition variable. `enqueue` upserts a `PENDING` row only when `settings_loader(session_name)['customer_memory_enabled']` is true. The worker claims one job, reads messages after the stored cursor, decrypts the previous memory, builds a bounded system/user prompt, calls `completion_fn(settings, system_prompt, user_prompt)`, validates a bounded JSON structure, encrypts the result, advances the cursor atomically, and retries with a capped exponential delay. `get` and `context_for_reply` must never expose ciphertext. `stop` wakes the worker and joins it without hanging the process.

- [ ] **Step 4: Add provenance to all archive paths**

  Extend `_archive_message(..., origin='unknown')` and persist `origin`. Mark manual text/image sends as `manual`, automated follow-up sends as `automation`, and `_send_text` calls from webhook AI replies as `auto_ai`; keep commerce/system sends as `automation` or `unknown` according to their existing caller. After a successful insert, call `self.memory.enqueue(...)` when the service exists. Preserve dedupe behavior.

- [ ] **Step 5: Inject memory into automatic replies**

  In `_ai_reply`, call `memory.context_for_reply(session_name, customer_id)` only when the session setting is enabled, append it after the bounded recent conversation context, and let the current inbound message and business rules remain higher priority. If memory service or AI summary is unavailable, keep the existing reply path and log only a redacted error code.

- [ ] **Step 6: Run the service and regression tests**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_customer_memory tests.test_chat_upgrade
  ```

  Expected: PASS with no changes to existing send dedupe or automatic reply tests.

- [ ] **Step 7: Commit the memory slice**

  ```powershell
  git add panel/customer_memory_service.py panel/app.py panel/chat_service.py tests/test_customer_memory.py tests/test_chat_upgrade.py
  git commit -m "feat: add encrypted customer memory pipeline"
  ```

### Task 3: Implement the isolated Alibaba machine translation service

**Files:**
- Create: `panel/aliyun_translation_service.py`
- Modify: `panel/requirements.txt`, `panel/app.py:enable_chat_services`, `stop_chat_services`
- Test: `tests/test_aliyun_translation.py`

**Interfaces:**
- `AliyunTranslationService(database_path, cipher, logger=None, clock=None, sdk_client_factory=None)`
- `settings_payload() -> dict`
- `save_settings(payload: dict) -> dict`
- `test_connection() -> dict`
- `translate_batch(session_name: str, chat_ref: str, items: list[dict]) -> dict`
- `clear_cache() -> dict`
- `is_pure_chinese(text: str) -> bool`
- `settings_payload()` returns `{"endpoint": str, "region_id": str, "access_key_id_configured": bool, "access_key_secret_configured": bool, "last_test_status": str | None, "last_test_at": int | None}`; it never returns either credential.
- `translate_batch(...)` returns `{"items": [{"message_ref": str, "status": "READY|SKIPPED|FAILED", "translation": str | None, "detected_language": str | None, "error_code": str | None}]}`.

- [ ] **Step 1: Write failing SDK and cache tests**

  Use a fake SDK client and assert:

  - missing configuration returns a safe configuration state and does not call the SDK;
  - pure Chinese text returns `SKIPPED` without an SDK call;
  - non-Chinese text invokes `TranslateGeneral` with `SourceLanguage='auto'`, `TargetLanguage='zh'`, `FormatType='text'`;
  - mixed-language text is sent for translation, empty/media-only text is skipped;
  - a second request with the same message/source fingerprint is served from encrypted cache;
  - a 4801-character input is rejected before SDK invocation;
  - SDK errors return `FAILED` without logging credentials or source text;
  - AccessKey Secret is never returned by `settings_payload`.

- [ ] **Step 2: Run the focused tests and verify they fail**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_aliyun_translation
  ```

  Expected: import or method failures.

- [ ] **Step 3: Add the fixed SDK dependency**

  Add `alibabacloud_alimt20181012==1.1.0` to `panel/requirements.txt`. Keep the SDK import isolated in the new service so tests can inject a fake client and installations without the optional package fail with a clear configuration error rather than a module traceback.

- [ ] **Step 4: Implement settings, language gate, cache, and SDK call**

  Store endpoint/region, AccessKey ID, and encrypted AccessKey Secret in the single-row `aliyun_translation_settings` table created by migration 008. Implement local pure-Chinese detection that ignores punctuation, digits, whitespace and emoji; mixed or uncertain text goes to Alibaba. Enforce the 4800-character safety limit, a five-slot semaphore, cache lookup by session/chat/message/source/target, and a bounded rate window. Use the official SDK request object and normalize the response to `READY`, `SKIPPED`, or `FAILED` items.

- [ ] **Step 5: Run the focused tests and verify they pass**

  Run the command from Step 2. Expected: PASS, including the no-secret-output assertions.

- [ ] **Step 6: Commit the translation service slice**

  ```powershell
  git add panel/aliyun_translation_service.py panel/requirements.txt tests/test_aliyun_translation.py
  git commit -m "feat: add aliyun machine translation service"
  ```

### Task 4: Wire memory and machine-translation HTTP routes

**Files:**
- Modify: `panel/app.py:enable_chat_services`, `chat_route`, `send_chat_get`, `send_chat_post`, `do_DELETE`, `send_translation_get`, `send_translation_write`
- Modify: `tests/test_chat_upgrade.py`, `tests/test_chat_features.py`

**Interfaces:**
- `GET /api/chat/sessions/{session}/memory?chat_ref=...`
- `POST /api/chat/sessions/{session}/memory/rebuild` with `{chat_ref}`
- `DELETE /api/chat/sessions/{session}/memory?chat_ref=...`
- `GET /api/translation/aliyun/settings`
- `PUT /api/translation/aliyun/settings`
- `POST /api/translation/aliyun/test`
- `POST /api/chat/sessions/{session}/machine-translations` with `{chat_ref, items:[{message_ref,text}]}`

- [ ] **Step 1: Write failing route tests**

  Assert admin and CSRF guards, session scoping, successful memory get/rebuild/clear, successful translation settings save/test, batch response shape, missing `chat_ref`, invalid message references, and the fact that route handling never calls translation for an overview request.

- [ ] **Step 2: Run route tests and verify they fail**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade tests.test_chat_features
  ```

  Expected: unsupported route or missing service failures.

- [ ] **Step 3: Wire services into `PanelState` lifecycle**

  Instantiate `CustomerMemoryService` and `AliyunTranslationService` from the same `DataCipher` in `enable_chat_services`; start the memory worker after initialization and stop both services in `stop_chat_services`. Keep the existing AI `TranslationService` separate.

- [ ] **Step 4: Add route dispatch and validation**

  Extend `chat_route` with `memory`, `memory/rebuild`, and `machine-translations`. `send_chat_get` handles memory reads; `send_chat_post` handles rebuild and batch translation; `do_DELETE` delegates memory clearing without reusing label deletion. Validate `chat_ref`, limit batch count and total text, and verify each `message_ref` belongs to the requested chat before translating. Add global `/api/translation/aliyun/*` dispatch without changing the existing AI settings routes.

- [ ] **Step 5: Run route and full backend regression tests**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade tests.test_chat_features tests.test_customer_memory tests.test_aliyun_translation
  ```

  Expected: PASS and no secret values in captured response bodies or logs.

- [ ] **Step 6: Commit the route slice**

  ```powershell
  git add panel/app.py tests/test_chat_upgrade.py tests/test_chat_features.py
  git commit -m "feat: expose memory and aliyun translation APIs"
  ```

### Task 5: Add automatic-reply memory controls and Alibaba settings UI

**Files:**
- Modify: `panel/app.py` inline automation settings page and settings-page JavaScript
- Modify: `panel/chat_page.py` translation settings dialog labels/fields/status copy
- Test: `tests/test_chat_upgrade.py`, `tests/test_chat_features.py`

**Interfaces:**
- Settings page uses the existing `GET /api/settings` and `POST /api/settings` payload with `customer_memory_enabled`.
- Chat translation dialog uses `/api/translation/aliyun/settings`, `/api/translation/aliyun/test`, and cache cleanup without exposing secrets.

- [ ] **Step 1: Write failing HTML/behavior contract tests**

  Assert the settings page contains `customerMemoryEnabled`, the explanatory text, load/save wiring, and no secret echo. Assert the chat page contains separate Alibaba labels, no promise that message translation uses the AI settings, and the machine-translation status copy.

- [ ] **Step 2: Run the page tests and verify they fail**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade tests.test_chat_features
  ```

- [ ] **Step 3: Implement the settings controls**

  Add the memory checkbox near the existing context range, load it from the settings payload, include it in the save request, clear transient notices after save, and preserve the existing per-session URL behavior. Add Alibaba endpoint/region/AccessKey fields to the translation dialog, keep the secret input write-only, and show configured/test status without echoing the secret.

- [ ] **Step 4: Run page tests and frontend syntax validation**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade tests.test_chat_features
  node scripts/check_frontend_syntax.mjs
  ```

  Expected: PASS and `Validated JavaScript syntax in 4 inline scripts.` or the updated equivalent count.

- [ ] **Step 5: Commit the settings UI slice**

  ```powershell
  git add panel/app.py panel/chat_page.py tests/test_chat_upgrade.py tests/test_chat_features.py
  git commit -m "feat: add memory and aliyun settings controls"
  ```

### Task 6: Fix chat-list scroll and implement current-chat translation UI

**Files:**
- Modify: `panel/chat_page.py` chat list markup, CSS, state, `renderChats`, `selectChat`, `loadMessages`, message renderer and event handlers
- Test: `tests/test_chat_features.py`, `tests/frontend_syntax.test.mjs` if needed

**Interfaces:**
- Frontend request body: `{chat_ref, items:[{message_ref,text}]}` to `/api/chat/sessions/{session}/machine-translations`.
- Frontend state: `translationEnabledByChat: Map<string, boolean>`, `translationAbort`, `translationRequestId`, and `translationByMessage` entries with `status`, `translation`, and `detected_language`.

- [ ] **Step 1: Write failing page contract tests**

  Assert:

  - `renderChats` captures a visible `chat_ref` and restores its offset after replacement;
  - a chat-header control has `aria-pressed`, per-chat state, and a current-chat-only translation handler;
  - the page submits to `machine-translations`, not `/api/translation/translate`, for global display translation;
  - message rendering has separate original/Chinese line containers and handles inbound/outbound equally;
  - the page cancels translation on chat switch/back/disable and checks request generation before applying results;
  - takeover/translation classes and `prefers-reduced-motion` CSS are present.

- [ ] **Step 2: Run page tests and verify they fail**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_features
  ```

- [ ] **Step 3: Implement the scroll anchor restoration**

  Add small helpers that capture `{chatRef, relativeTop, scrollTop}` before `replaceChildren` and restore the same chat after rendering. Pass an explicit `resetScroll` flag from search/filter operations; polling, selection, metadata sync and event refresh use the preservation path.

- [ ] **Step 4: Implement the header toggle and request lifecycle**

  Add the current-chat `全局翻译` button beside the takeover controls. Store state in `translationEnabledByChat`, default to false, abort on selection change/back/disable, and increment `translationRequestId` for every new batch. Only call the batch endpoint when the conversation is visible and `state.selected.chat_ref` still matches. Translate current message pages and newly received messages without touching unselected chats.

- [ ] **Step 5: Implement dual-line message rendering**

  Render the original body/caption unchanged. When a translation result is `READY`, append a labeled Chinese line; when `SKIPPED`, keep one line; when `FAILED`, append a retry action that only retries the current message through the same batch endpoint. Do not render translations for empty or media-only rows.

- [ ] **Step 6: Implement breathing states and accessibility**

  Add low-amplitude active/inactive breathing classes for global translation and takeover/AI state, with green/amber/blue tones, `aria-pressed`, and static fallback under `prefers-reduced-motion`. Keep disabled/requesting buttons keyboard accessible and prevent double submits.

- [ ] **Step 7: Run page tests and syntax validation**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_features
  node scripts/check_frontend_syntax.mjs
  ```

  Expected: PASS; no old global-display path calls `/api/translation/translate`.

- [ ] **Step 8: Commit the chat UI slice**

  ```powershell
  git add panel/chat_page.py tests/test_chat_features.py tests/frontend_syntax.test.mjs
  git commit -m "feat: preserve chat scroll and translate active conversation"
  ```

### Task 7: Documentation, migration rehearsal, and full verification

**Files:**
- Modify: `README.zh-CN.md`, `INSTALL.zh-CN.md`
- Test/verification: all test files and migration rehearsal database

- [ ] **Step 1: Document operational settings**

  Document the automatic-reply memory switch, per-customer behavior, Alibaba credentials and endpoint, original-plus-Chinese display, default-off behavior, data encryption, no-secret logging, cache cleanup, backup and rollback. State that the existing AI sales assistant remains separate from global display translation.

- [ ] **Step 2: Rehearse fresh and upgrade migrations**

  Create a temporary SQLite database with the pre-008 schema and sample session/message rows, run `init_db`, assert existing settings/messages remain, `origin` defaults to `unknown`, and all new tables/indexes exist. Repeat on a fresh database and confirm no customer rows are seeded.

- [ ] **Step 3: Run the complete verification suite**

  ```powershell
  & 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_features tests.test_chat_engagement tests.test_chat_upgrade tests.test_customer_memory tests.test_aliyun_translation
  node scripts/check_frontend_syntax.mjs
  git diff --check HEAD~7..HEAD
  git status --short --branch
  ```

  Expected: all tests pass, frontend syntax validation passes, diff check is clean, and the worktree contains only intended committed changes.

- [ ] **Step 4: Verify service limitations explicitly**

  Record that Docker/WAHA runtime health and actual page access remain unverified until the Docker Desktop Linux engine is available. Do not claim deployment completion from local tests alone.

- [ ] **Step 5: Commit documentation and verification notes**

  ```powershell
  git add README.zh-CN.md INSTALL.zh-CN.md
  git commit -m "docs: document customer memory and machine translation"
  ```

## Execution Handoff

This plan is intentionally ordered because the UI and HTTP routes depend on the service contracts, and the memory/translation data must be migrated before production code can safely read it. I recommend the **Native** execution approach: I implement each task in this same worktree with the specified test gate, then perform one whole-branch review. The files are tightly coupled (`panel/app.py` and `panel/chat_page.py` are shared integration points), so parallel edits would add merge risk without shortening the validation path.

After reviewing this plan, confirm two things before implementation starts:

1. The plan captures the approved design.
2. Choose **Native** execution, or explicitly request **Subagent-driven** execution.
