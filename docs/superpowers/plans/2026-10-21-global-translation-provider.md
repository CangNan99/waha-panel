# 全局翻译双引擎实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将聊天页全局翻译从阿里云通用版升级为阿里云专业版，并让管理员在阿里云专业版与现有 AI 模型之间手动切换。

**Architecture:** 保留现有 `AliyunTranslationService` 和 `TranslationService` 的职责，在两者之上新增 `GlobalTranslationService` 负责 provider 选择、结果规范化和引擎隔离。聊天批量翻译接口只调用这个路由服务；销售辅助 AI 接口继续直接使用现有 AI 服务。

**Tech Stack:** Python 3.12、SQLite、现有阿里云 Python SDK、cryptography、原生 HTML/JavaScript、现有 unittest 与前端语法检查。

**Spec:** `docs/superpowers/specs/2026-10-21-global-translation-iptv-delay-design.md`

## Global Constraints

- 默认 provider 必须是 `aliyun_professional`；AI provider 只能由管理员手动选择。
- 阿里云专业版使用 `TranslateRequest`、`source_language=auto`、`target_language=zh`、`format_type=text`、`scene=communication`。
- 单条翻译继续限制为 4800 字符；本地并发为 5；本地速率不能超过专业版 50 QPS。
- 专业版失败时不得自动回退到 AI 或通用版；原文继续显示并返回脱敏错误码。
- AI provider 复用现有 `translation_settings`、密钥加密、IPTV 敏感信息过滤和结构化 JSON 校验。
- 两个 provider 的缓存必须隔离；过期请求不能写入已切换的聊天。
- 任何接口、日志和测试输出不得包含 AccessKey Secret、AI API Key、客户原文、译文或 IPTV 凭据。
- 本计划不包含远端部署、Docker 重启或生产凭据修改。

## Review Focus

- 阿里云 SDK 版本中的专业版请求模型是否真实可用；测试 `TranslateRequest` 的字段和 `scene=communication`。
- provider 切换期间旧请求返回；测试请求代次和缓存 key 不得把旧译文写入当前 provider。
- AI provider 未配置或限流；测试必须显示错误且不静默回退。
- 专业版权限、欠费、限流和网络错误；测试错误码脱敏且原文仍可读。
- IPTV 凭据进入两个 provider 的请求边界；复用现有 sanitizer 并测试原始密钥不出现在请求、日志和异常中。

### Task 1: Provider 配置迁移和基础读取

**Files:**
- Create: `panel/migrations/011_global_translation_settings.sql`
- Modify: `panel/app.py:441-532` (`init_db` migration order)
- Test: `tests/test_chat_upgrade.py` (`SettingsAndContextTests`)

**Interfaces:**
- Produces singleton table `global_translation_settings(id, provider, scene, updated_at)`.
- Produces default row `(1, 'aliyun_professional', 'communication', now)`.
- `PanelState` initialization after this task can safely read provider preferences without exposing credentials.

- [ ] **Step 1: Write the failing migration test**

Add `test_global_translation_settings_migrate_idempotently_and_default_to_aliyun_professional` to create a temporary database, call `init_db` twice, and assert:

```python
row = connection.execute(
    "SELECT provider, scene FROM global_translation_settings WHERE id=1"
).fetchone()
assert row == ("aliyun_professional", "communication")
```

Also assert existing `translation_settings` and `aliyun_translation_settings` tables remain present.

- [ ] **Step 2: Run the migration test and verify it fails**

Run:

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade.SettingsAndContextTests.test_global_translation_settings_migrate_idempotently_and_default_to_aliyun_professional
```

Expected: FAIL because the new table is not created.

- [ ] **Step 3: Add the migration and load it from `init_db`**

Create the idempotent SQL migration and execute it after migration 010. Do not alter or rebuild existing translation tables. Keep `provider` and `scene` constrained to the values used by the route service.

- [ ] **Step 4: Re-run the migration test**

Run the focused test again. Expected: PASS on a fresh database and on a database initialized twice.

- [ ] **Step 5: Commit the migration slice**

```powershell
git add panel/migrations/011_global_translation_settings.sql panel/app.py tests/test_chat_upgrade.py
git commit -m "feat: add global translation provider settings"
```

### Task 2: Switch Alibaba service to professional edition

**Files:**
- Modify: `panel/aliyun_translation_service.py:187-235,291-360`
- Test: `tests/test_aliyun_translation.py`

**Interfaces:**
- Preserve `AliyunTranslationService.translate_batch(session_name, chat_key, items) -> {"items": [...]}`.
- Preserve `get_cached_batch`, `settings_payload`, `save_settings`, `test_connection`, and `clear_cache` signatures.
- `_sdk_request(text, settings, scene="communication") -> (translated_text, detected_language)` uses professional `TranslateRequest`.

- [ ] **Step 1: Update the fake SDK test fixture**

Extend `FakeClient` in `tests/test_aliyun_translation.py` to record request type, `source_language`, `target_language`, `format_type`, `scene`, and source text. Change the current general-edition test into `test_non_chinese_uses_professional_auto_to_zh_communication` with assertions:

```python
assert request.kind == "TranslateRequest"
assert request.source_language == "auto"
assert request.target_language == "zh"
assert request.format_type == "text"
assert request.scene == "communication"
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_aliyun_translation.AliyunTranslationTests.test_non_chinese_uses_professional_auto_to_zh_communication
```

Expected: FAIL because the service still constructs `TranslateGeneralRequest` and sends `scene=general`.

- [ ] **Step 3: Implement the professional SDK request**

Import the SDK's professional request model, set the exact fields from the spec, and keep response normalization compatible with `Data.Translated` and `Data.DetectedLanguage`. Map professional error codes to existing public error codes without logging request text or credentials.

Change the local rate window to a safe value at or below 50 QPS. Keep the five-slot semaphore and the 4800-character validation.

- [ ] **Step 4: Add professional error and boundary tests**

Add tests for:

- 4800 characters accepted and 4801 rejected before SDK call;
- permission/service-not-opened response returns a stable `ALIYUN_PERMISSION` or equivalent public code;
- throttling response returns a retryable, sanitized error;
- source text and AccessKey values do not occur in captured logs or exception messages;
- cached rows still work without an SDK call.

- [ ] **Step 5: Run all Alibaba translation tests**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_aliyun_translation
```

Expected: PASS with no secret or source text in output.

- [ ] **Step 6: Commit the professional provider slice**

```powershell
git add panel/aliyun_translation_service.py tests/test_aliyun_translation.py
git commit -m "feat: use aliyun professional translation engine"
```

### Task 3: Add the global provider router

**Files:**
- Create: `panel/global_translation_service.py`
- Modify: `panel/translation_service.py:402-470` (display translation adapter)
- Test: `tests/test_global_translation.py`

**Interfaces:**
- Create `GlobalTranslationService(database_path, aliyun_service, ai_service, clock, logger=None)`.
- `settings_payload() -> dict` returns `provider`, `scene`, `aliyun_configured`, `ai_configured`, and last-test statuses only.
- `save_settings(payload: dict) -> dict` accepts only `aliyun_professional` or `ai_model` and optional `communication` scene.
- `translate_batch(session_name, chat_key, items) -> dict` returns normalized `READY`, `SKIPPED`, and `FAILED` items with `provider`.
- `get_cached_batch(session_name, chat_key, items) -> dict` never contacts a third-party service.
- `test_connection() -> dict` tests only the currently selected provider.
- `clear_cache(provider=None) -> dict` clears only the selected provider cache when specified.

- [ ] **Step 1: Write failing router tests**

Create `tests/test_global_translation.py` with fakes for both services. Cover:

- default provider is `aliyun_professional`;
- saving `ai_model` changes the singleton setting;
- invalid provider is rejected without modifying the old provider;
- Alibaba result is passed through with `provider=aliyun_professional`;
- AI result maps `chinese_translation` to `translation` and preserves `FAILED` on AI errors;
- cache reads do not call either service;
- the provider is part of the result/cache identity.

- [ ] **Step 2: Run the new tests and verify they fail**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_global_translation
```

Expected: FAIL because the router module does not exist.

- [ ] **Step 3: Add the AI display adapter**

Add `TranslationService.translate_display(text, force=False, role="customer") -> dict`. Reuse `translate`, return only normalized display fields, and preserve existing structured translation routes unchanged.

- [ ] **Step 4: Implement `GlobalTranslationService`**

Read the singleton preference inside each batch so a request uses one stable provider. For Alibaba call `translate_batch`; for AI iterate the validated items through `translate_display` under the existing AI request guard. Never invoke the other provider as a fallback.

- [ ] **Step 5: Run the router tests**

Expected: PASS, including provider isolation and no-secret assertions.

- [ ] **Step 6: Commit the router slice**

```powershell
git add panel/global_translation_service.py panel/translation_service.py tests/test_global_translation.py
git commit -m "feat: route global translation by provider"
```

### Task 4: Wire HTTP routes and request guards

**Files:**
- Modify: `panel/app.py:837-1045,3460-3490,3690-3845,3900-3945,4480-4555`
- Test: `tests/test_chat_upgrade.py` (`MemoryAndTranslationRouteTests`)

**Interfaces:**
- `PanelState.global_translation` stores the router service.
- `PanelHandler.send_translation_get` handles `/api/translation/preferences`.
- `PanelHandler.send_translation_write` handles `PUT /api/translation/preferences` and `POST /api/translation/preferences/test`.
- The chat `machine-translations` and `machine-translations/cache` actions call the router instead of the Alibaba service directly.

- [ ] **Step 1: Add failing route tests**

Add tests for:

- preference GET never returns credentials;
- preference PUT changes provider and returns only safe fields;
- test route calls only the selected provider;
- machine translations dispatch to AI after selecting `ai_model`;
- cross-chat message references remain rejected;
- current AI rate/slot guard is released on both success and failure.

- [ ] **Step 2: Run the route tests and verify the old direct-Alibaba path fails**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade.MemoryAndTranslationRouteTests
```

Expected: the new preference and AI dispatch assertions fail.

- [ ] **Step 3: Instantiate and require the router**

Initialize it after both translation services are available. Add `require_global_translation_service()` with the same encrypted-capability error behavior as existing service guards.

- [ ] **Step 4: Add preference routes and dispatch machine translations**

Keep existing Alibaba and AI configuration routes backward-compatible. In the batch route, validate message references before calling the router and wrap AI provider calls in `begin_translation_request()`/`end_translation_request()` with `finally`.

- [ ] **Step 5: Run focused route and existing translation tests**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_upgrade.MemoryAndTranslationRouteTests tests.test_aliyun_translation tests.test_global_translation
```

Expected: PASS.

- [ ] **Step 6: Commit the HTTP integration slice**

```powershell
git add panel/app.py tests/test_chat_upgrade.py
git commit -m "feat: expose global translation provider routes"
```

### Task 5: Add manual engine switching to the chat UI

**Files:**
- Modify: `panel/chat_page.py:63-125,135-490`
- Test: `tests/test_chat_features.py`, `tests/ui_acceptance.mjs`

**Interfaces:**
- Frontend state adds `translationProvider`, `translationProviders`, and a provider-aware translation key.
- `loadTranslationPreferences()` calls `/api/translation/preferences`.
- `saveTranslationProvider()` calls `PUT /api/translation/preferences`.
- Existing `translateCurrentChat()` and cached translation calls use the selected provider returned by the server.

- [ ] **Step 1: Add failing page contract tests**

Extend `test_current_chat_machine_translation_and_separate_aliyun_settings_contract` to assert:

- provider selector exists;
- `/api/translation/preferences` is used;
- UI labels contain both `阿里云机器翻译专业版` and `AI 模型翻译`;
- current provider appears in the global toggle;
- no automatic fallback string is present.

Add a browser acceptance scenario that selects AI, enables global translation, switches back to Alibaba, and confirms an in-flight request is aborted before a new provider request starts.

- [ ] **Step 2: Run the focused frontend contract test and verify it fails**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_features.ChatPageRegressionTests.test_global_translation_provider_switch_contract
```

Expected: FAIL because the selector and preference route are absent.

- [ ] **Step 3: Add the selector and safe status rendering**

Extend the existing Alibaba dialog instead of creating a second settings page. Keep credential inputs blank after load and display only configured booleans and test times.

- [ ] **Step 4: Make translation state provider-aware**

Include provider in client cache keys, abort and increment request generation on switch, hide stale rows, then reload current chat. Preserve completed server cache for both providers.

- [ ] **Step 5: Run page tests and syntax checks**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_chat_features.ChatPageRegressionTests
node scripts/check_frontend_syntax.mjs
node --test tests/frontend_syntax.test.mjs
```

Expected: PASS with no JavaScript syntax errors.

- [ ] **Step 6: Commit the UI slice**

```powershell
git add panel/chat_page.py tests/test_chat_features.py tests/ui_acceptance.mjs
git commit -m "feat: allow manual global translation provider switching"
```

### Task 6: Documentation and provider-specific verification

**Files:**
- Modify: `README.zh-CN.md`, `INSTALL.zh-CN.md`
- Test/verification: `tests/test_aliyun_translation.py`, `tests/test_global_translation.py`, `tests/test_chat_upgrade.py`

- [ ] **Step 1: Update Chinese operations documentation**

Document the professional `Translate` API, `communication` scene, provider selector, no-fallback behavior, credential privacy, cache cleanup, and the requirement that the Alibaba account has professional translation access.

- [ ] **Step 2: Run the provider test group**

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest -v tests.test_aliyun_translation tests.test_global_translation tests.test_chat_upgrade tests.test_chat_features
```

Expected: all selected tests pass.

- [ ] **Step 3: Verify the migration and diff**

```powershell
git diff --check HEAD~5..HEAD
git status --short --branch
```

Expected: no whitespace errors; only intended committed changes plus the pre-existing untracked plan documents.

- [ ] **Step 4: Commit documentation**

```powershell
git add README.zh-CN.md INSTALL.zh-CN.md
git commit -m "docs: document global translation providers"
```

## Rollback

- Revert the provider route and UI commits, then restart only the local test process if one is running.
- Set the provider preference back to `aliyun_professional` before removing access to the AI provider.
- Keep `global_translation_settings` and both encrypted caches; older code ignores the new table and can continue using the existing Alibaba service.
- Do not restore an older SQLite file over newer translation cache rows unless the database itself is damaged and the backup time has been verified.

## Execution Handoff

This plan is one of three independently testable tracks from the approved design. It owns migration 011 and all provider-specific code; the IPTV system-label track owns migration 012, and the reply-delay track owns session settings and slider UI. The tracks share only the final `app.py`/`chat_page.py` regression run.

Plan complete and saved to `docs/superpowers/plans/2026-10-21-global-translation-provider.md`. Please review it together with the two companion plans and confirm the execution approach before implementation starts.
