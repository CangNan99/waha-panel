# GLM 多模态模型接入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有 OpenAI 兼容 AI 行为的前提下，为每个 WhatsApp 会话增加 GLM 语言、视觉理解、视觉推理和图像生成能力，并让客户图片可以触发自动识别和自动回复。

**Architecture:** 新增 `ai_profiles.py` 管理按会话/能力分槽的配置和密钥加密，新增 `glm_service.py` 负责 GLM 对话补全、视觉请求、图像生成和短时图片缓存。`PanelState` 连接 profile、GLM 服务、Webhook 自动回复、聊天 API 和现有 `ChatService`；页面只调用受保护的面板接口，不接触 API Key 或任意远程媒体地址。

**Tech Stack:** Python 标准库、SQLite、现有 `DataCipher`/`ChatService`/`WahaClient`、Pillow 图片校验、原生 HTML/CSS/JavaScript、Python `unittest`、Node.js `vm.Script` 前端语法检查。

**Spec:** `docs/superpowers/specs/2026-11-09-glm-multimodal-design.md`

## Global Constraints

- 自动回复遇到客户发来的图片消息时，若配置了视觉模型则必须读取并理解图片内容，结合图片说明和现有业务上下文自动生成回复并发送；没有视觉配置时保持现有“媒体消息不自动回复”的行为。
- 每种能力使用独立配置槽位，允许一个会话同时配置语言、视觉理解、视觉推理和图像生成模型；API Key 可以相同，但各槽位独立保存模型、地址和推理设置。
- 现有 `session_settings` 中的 `ai_base_url`、`ai_model`、`ai_api_key` 继续作为语言模型的兼容回退。
- `openai_compatible` provider 只允许用于 `language` 槽位；三种非语言能力保存时必须使用 `glm`。
- 图片输入最大 10 MiB、最大 40,000,000 像素，只允许 JPEG、PNG、WebP。
- GLM 媒体读取最多 15 秒、模型请求最多 30 秒、生成/视觉请求共用最多 2 个并发槽位。
- 生成图片引用使用 `AVAILABLE → RESERVED → CONSUMED` 状态；明确失败释放为 `AVAILABLE`，未知结果转为 `CONSUMED` 并禁止重发。
- 没有 `PANEL_DATA_ENCRYPTION_KEY` 时不创建或保存新的 GLM profile；旧的明文 `ai_*` 配置继续按当前行为可用。
- 图像生成连接测试只校验配置，不实际生成图片。
- API Key、客户消息全文、图片内容、推理内容和生成结果不得写入普通日志。
- 本计划不修改 WAHA API、Docker 网络、端口、域名或反向代理，不进行生产部署。

## Review Focus

- **媒体消息去重与回退：** 客户重复发送同一图片、无 caption、视觉模型未配置或调用失败时，自动回复至多一次且不发送固定文案；由 Task 4 的 Webhook 测试覆盖。
- **配置隔离与密钥：** 四个能力槽位按会话隔离，Key 只返回配置状态，缺少加密密钥时新 profile 接口不可用；由 Task 1 的 profile 测试覆盖。
- **模型请求边界：** 视觉请求只能携带受限 data URI，推理请求按能力发送参数，图像生成只能走 GLM endpoint；由 Task 2 的协议测试覆盖。
- **生成图片发送状态：** 已确认失败可重试，未知结果不能重发，同一 `client_request_id` 幂等返回；由 Task 5 的生成发送测试覆盖。
- **前端状态与安全引用：** 视觉卡片/生成预览在实时刷新后保持，过期或跨会话引用被拒绝，内联脚本继续可解析；由 Task 6 的页面和路由测试覆盖。

---

### Task 1: Add the profile migration and encrypted profile store

**Files:**
- Create: `panel/migrations/007_glm_multimodal.sql`
- Create: `panel/ai_profiles.py`
- Modify: `panel/app.py:DEFAULT_SETTINGS`, `init_db`, `PanelState.__init__`, `enable_chat_services`, `settings_payload`, `save_settings`
- Test: `tests/test_ai_profiles.py`

**Interfaces:**
- Produces `AIProfileStore.profile_payload(session) -> dict` with four capability entries and only `api_key_configured` for secrets.
- Produces `AIProfileStore.save_profiles(session, payload) -> dict` and `AIProfileStore.effective_profile(session, capability, legacy_settings) -> dict | None`.
- Produces `AIProfileStore.validate_profile(capability, provider, base_url, model, reasoning_mode) -> None`.

- [ ] **Step 1: Write the failing migration and profile tests**

  Add tests that run `init_db()` twice, assert `session_ai_profiles` and the new `translation_settings.provider/reasoning_mode` columns exist, save all four profiles for two sessions, and assert:

  - non-language `openai_compatible` profiles are rejected;
  - Key plaintext never appears in `profile_payload()` or SQLite profile columns;
  - an existing legacy `ai_*` setting is returned as the language fallback when no language profile row exists;
  - a missing `DataCipher` disables new profile save/read without changing legacy `settings_payload()` behavior.

- [ ] **Step 2: Run the profile tests to verify they fail**

  Run: `python -m unittest -v tests.test_ai_profiles`

  Expected: FAIL because migration 007, `AIProfileStore`, and the new `PanelState` profile methods do not exist.

- [ ] **Step 3: Implement the additive migration and `AIProfileStore`**

  Create the idempotent table and indexes from the spec. In `ai_profiles.py`, validate HTTPS public URLs using an injectable resolver, enforce the capability/provider matrix and reasoning values, encrypt API Keys with `DataCipher.encrypt_json`, and decrypt only inside `effective_profile()`.

  Keep legacy `session_settings` as the language fallback. When the panel has no data encryption key, leave `self.ai_profiles` unset and return an explicit unavailable error from new profile operations.

- [ ] **Step 4: Wire profile payload/save methods into `PanelState`**

  Add the non-sensitive `ai_profiles` object to `/api/settings` responses. Accept a nested `ai_profiles` payload in `save_settings()` while preserving existing fields and default behavior. Ensure default and newly created sessions get no secret values and profile rows remain session-scoped.

- [ ] **Step 5: Run the profile tests to verify they pass**

  Run: `python -m unittest -v tests.test_ai_profiles`

  Expected: PASS with no API Key or encrypted value printed.

- [ ] **Step 6: Commit the profile layer**

  ```bash
  git add panel/migrations/007_glm_multimodal.sql panel/ai_profiles.py panel/app.py tests/test_ai_profiles.py
  git commit -m "feat: add encrypted GLM capability profiles"
  ```

### Task 2: Implement the GLM protocol adapter and bounded image cache

**Files:**
- Create: `panel/glm_service.py`
- Test: `tests/test_glm_service.py`

**Interfaces:**
- Produces `GLMService.chat_text(profile, messages, tools=None) -> GLMResult`.
- Produces `GLMService.chat_vision(profile, prompt, image_bytes, mime_type, reasoning=False, context=None, tools=None) -> GLMResult`.
- Produces `GLMService.generate_image(profile, prompt, size=None) -> GeneratedImage`.
- Produces `GeneratedImageStore.put(session, chat_key_hmac, mime_type, data) -> str`, `get(...)`, `reserve(...)`, `finalize(...)`.
- Raises `GLMError(code, public_message)` for validation, timeout, upstream, invalid-response and rate-limit failures.

- [ ] **Step 1: Write failing request/response contract tests**

  Use a fake opener and deterministic response objects. Assert that:

  - text requests target `<base_url>/chat/completions` and preserve tools;
  - vision requests use a bounded JPEG/PNG/WebP data URI inside an OpenAI-style `image_url` content item;
  - reasoning requests add the selected thinking/reasoning settings and return only final `content` to callers while retaining reasoning separately in the internal result;
  - image requests target `<base_url>/images/generations`, parse `url` and `b64_json` responses, and reject malformed/oversized results;
  - API keys never appear in raised messages or log callbacks;
  - cache references are session/chat bound, expire, reserve atomically, release after definite failure, and become unavailable after unknown/SENT finalization.

- [ ] **Step 2: Run the adapter tests to verify they fail**

  Run: `python -m unittest -v tests.test_glm_service`

  Expected: FAIL because `panel/glm_service.py` and its result/cache types do not exist.

- [ ] **Step 3: Implement the minimal adapter**

  Build JSON requests with `urllib.request`, a 30-second model timeout, and a 15-second provider image fetch timeout. Validate image bytes with the existing Pillow rules before encoding or storing them. Accept only the configured GLM origin or the documented GLM provider hosts for image URLs. Keep the short-lived cache in memory with a 32-item bound and monotonic expiration checks.

- [ ] **Step 4: Run the adapter tests to verify they pass**

  Run: `python -m unittest -v tests.test_glm_service`

  Expected: PASS with request bodies inspected only in memory.

- [ ] **Step 5: Commit the adapter**

  ```bash
  git add panel/glm_service.py tests/test_glm_service.py
  git commit -m "feat: add GLM multimodal protocol adapter"
  ```

### Task 3: Connect GLM profiles to text, vision, and image-generation state services

**Files:**
- Modify: `panel/app.py:PanelState`, `send_service_error`, `send_session_get`, `send_session_post`, `do_GET`, `do_POST`
- Modify: `panel/chat_service.py` for a bounded `image_for_ai()` helper and existing send-image reuse
- Test: `tests/test_glm_state.py`

**Interfaces:**
- Produces `PanelState.test_ai_profile(session, capability) -> dict`.
- Produces `PanelState.vision_for_message(session, chat_ref, message_ref, prompt, mode) -> dict`.
- Produces `PanelState.generate_image(session, chat_ref, prompt, size=None) -> dict`.
- Produces `PanelState.generated_image(session, chat_ref, image_ref) -> (status, mime_type, bytes)`.
- Produces `PanelState.send_generated_image(session, chat_ref, image_ref, caption, client_request_id) -> dict`.

- [ ] **Step 1: Write failing state and route tests**

  Cover profile test semantics (text fixture for language, 1×1 PNG fixture for vision, configuration-only validation for image generation), session/chat reference ownership, the vision result shape, generated preview access, and generated-image sending states (`SENT`, `FAILED`, `UNKNOWN`, duplicate request).

- [ ] **Step 2: Run the state tests to verify they fail**

  Run: `python -m unittest -v tests.test_glm_state`

  Expected: FAIL because `PanelState` has no GLM service or generated-image routes.

- [ ] **Step 3: Add bounded media access and GLM lifecycle methods**

  Resolve `chat_ref`/`message_ref` through `ChatService`, download only the current WAHA image, validate it, and call `GLMService`. Acquire one of two shared GLM slots around provider calls. For generated images, reserve the cache entry before calling `ChatService.send_image`; finalize it as consumed on `SENT` or `UNKNOWN`, and release it on definite failure.

- [ ] **Step 4: Add protected HTTP routes and error mapping**

  Extend `chat_route()` actions with `vision`, `generate-image`, `generated-image`, and `send-generated-image`. Keep admin/CSRF/session checks identical to existing chat actions. Return `403/400/409/502` through existing redacted error handling without exposing upstream bodies.

- [ ] **Step 5: Run the state tests to verify they pass**

  Run: `python -m unittest -v tests.test_glm_state`

  Expected: PASS, including cross-session and expired-reference rejection.

- [ ] **Step 6: Commit the state integration**

  ```bash
  git add panel/app.py panel/chat_service.py tests/test_glm_state.py
  git commit -m "feat: expose GLM vision and image generation routes"
  ```

### Task 4: Make customer image messages trigger automatic GLM replies

**Files:**
- Modify: `panel/app.py:PanelState._ai_reply`, `handle_webhook`, `_publish_chat_event`, and related helpers
- Test: `tests/test_glm_auto_reply.py`

**Interfaces:**
- Extends `_ai_reply(..., image_bytes=None, image_mime=None, image_prompt=None)` without changing existing text callers.
- `handle_webhook()` returns `{"action": "replied", "mode": "glm_vision", ...}` for a successfully handled customer image.

- [ ] **Step 1: Write failing webhook tests**

  Add fake WAHA media and fake GLM responses. Assert that a customer image with caption is recorded once, sent to the visual model with both caption and image, and produces a single outgoing reply after the existing checks. Cover no-caption images, duplicate webhook delivery, human takeover, outside schedule, missing visual profile, failed image download, failed GLM response, and simultaneous vision profiles where reasoning wins.

- [ ] **Step 2: Run the webhook tests to verify they fail**

  Run: `python -m unittest -v tests.test_glm_auto_reply`

  Expected: FAIL because current `handle_webhook()` unconditionally ignores media messages.

- [ ] **Step 3: Implement the vision auto-reply path**

  Move image-specific dedupe and conversation recording before the media early return, preserve the existing text-only early return for unsupported media, and choose `vision_reasoning` before `vision_understanding`. Build the same persona, system prompt, knowledge, conversation and business-tool context as text replies. On missing profile or failure, record a redacted status and return without fixed-text fallback.

- [ ] **Step 4: Preserve existing text and tool behavior**

  Route legacy text calls through the existing path, route GLM language profiles through the adapter, and keep order tool parsing and send confirmation unchanged. Do not include reasoning content in the outgoing message, event payload, or logs.

- [ ] **Step 5: Run the webhook tests and existing chat tests**

  Run: `python -m unittest -v tests.test_glm_auto_reply tests.test_chat_features`

  Expected: PASS; existing text, manual send, and media-display behavior remain unchanged.

- [ ] **Step 6: Commit the automatic image reply**

  ```bash
  git add panel/app.py tests/test_glm_auto_reply.py
  git commit -m "feat: auto-reply to customer images with GLM vision"
  ```

### Task 5: Add settings and chat-management UI for the four GLM capabilities

**Files:**
- Modify: `panel/app.py:settings_page()` and its inline script
- Modify: `panel/chat_page.py` markup, styles, and inline script
- Modify: `tests/frontend_syntax.test.mjs`, `tests/test_chat_features.py`

**Interfaces:**
- Settings page submits `ai_profiles` with four capability objects and renders only non-sensitive response fields.
- Chat page calls the new vision/generation routes and keeps the opaque `image_ref` in memory only.

- [ ] **Step 1: Write failing page and behavior assertions**

  Assert that the settings page contains four capability labels, GLM provider guidance, the configuration-only image-generation test text, and a Key-hidden state. Assert that the chat page contains visual-understanding/reasoning actions for image messages, a generate-image control, a preview card, and send/remove/regenerate actions.

- [ ] **Step 2: Run page tests to verify they fail**

  Run: `python -m unittest -v tests.test_chat_features && node --test tests/frontend_syntax.test.mjs`

  Expected: FAIL because the new controls and request handlers are not present.

- [ ] **Step 3: Implement settings UI**

  Add four compact profile sections to the existing settings page, default the GLM base URL only in the UI, keep model names editable, and submit empty Key fields as “keep existing”. Add a capability-specific test button that uses the protected route and clearly labels image-generation tests as configuration-only.

- [ ] **Step 4: Implement chat visual actions and generated-image preview**

  Add buttons only for inbound image messages, render results in the existing assistant-card pattern, abort stale requests when the chat changes, keep cards through event refreshes, and require a new UUID for every confirmed send. Generated preview uses the protected image endpoint and never renders a provider URL supplied by the browser.

- [ ] **Step 5: Run page tests and syntax checks to verify they pass**

  Run: `python -m unittest -v tests.test_chat_features && node --test tests/frontend_syntax.test.mjs`

  Expected: PASS with every inline script parseable and no secret literals in rendered HTML.

- [ ] **Step 6: Commit the UI**

  ```bash
  git add panel/app.py panel/chat_page.py tests/frontend_syntax.test.mjs tests/test_chat_features.py
  git commit -m "feat: add GLM vision and image generation controls"
  ```

### Task 6: Document configuration and run full verification

**Files:**
- Modify: `README.zh-CN.md`, `INSTALL.zh-CN.md`
- Create: `tests/test_glm_regression.py` if a final cross-layer regression test is needed

**Interfaces:**
- Documentation describes GLM base URL/model setup, four capability slots, image auto-reply behavior, data-sharing boundary, and rollback without containing real credentials.

- [ ] **Step 1: Write failing documentation/regression assertions**

  Add a small regression test that initializes an old-style database, applies all migrations twice, verifies legacy text auto-reply remains available, and asserts the documentation names the four capabilities and the no-key fallback.

- [ ] **Step 2: Run the regression test to verify it fails**

  Run: `python -m unittest -v tests.test_glm_regression`

  Expected: FAIL until the documentation and final migration behavior are present.

- [ ] **Step 3: Update user-facing documentation**

  Explain the recommended China endpoint, editable model names, profile precedence, customer-image auto-reply conditions, limits, third-party data transfer, configuration-only image test, and the backup/rollback sequence. Do not include API Keys or private URLs.

- [ ] **Step 4: Run the complete verification suite**

  Run:

  ```bash
  python -m unittest -v
  node --test tests/frontend_syntax.test.mjs
  python -m py_compile panel/*.py tests/*.py
  ```

  Expected: all existing and new tests pass, inline scripts parse, Python bytecode compilation succeeds, and output contains no secrets.

- [ ] **Step 5: Perform a local read-only HTTP smoke test**

  Start the panel with a temporary SQLite database, fake WAHA client, and fake GLM opener. Verify `/api/settings`, profile test, vision route, generated preview, and one-click send responses. Stop the process and inspect that no generated cache survives restart. Do not call a real GLM endpoint or a production WAHA session.

- [ ] **Step 6: Record verification and commit documentation**

  ```bash
  git add README.zh-CN.md INSTALL.zh-CN.md tests/test_glm_regression.py
  git commit -m "docs: document GLM multimodal configuration and verification"
  ```

