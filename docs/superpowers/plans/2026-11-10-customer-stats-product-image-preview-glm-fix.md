# 客户统计、产品图片引用与 GLM 修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在 1.0.26 发布基线上增加账号客户统计、确定性产品图片引用、聊天图片预览缩放，并修复 GLM 完整 endpoint 配置导致的重复路径 404。

**Architecture:** Webhook 在本地 SQLite 中以客户 HMAC 记录首次入站时间，监控服务只读聚合并由状态页面展示；产品图片资产保存产品名、SKU 和别名，解析器按确定性来源顺序选择唯一启用主图；GLM 适配器在 endpoint 边界兼容根地址和完整路径；聊天页面在现有媒体代理之上增加私有预览对话框。

**Tech Stack:** Python 3.12、SQLite、现有 ThreadingHTTPServer、Pillow、原生 HTML/CSS/JavaScript、Python `unittest`、Node `node:test`。

**Spec:** `docs/superpowers/specs/2026-11-10-customer-stats-product-image-preview-glm-fix-design.md`

## Global Constraints

- 基线固定为 `release/glm-multimodal-1.0.26`；不修改 WAHA API、Docker 网络、端口、反向代理或生产容器。
- “今日新客户”使用 Asia/Shanghai 当天第一次非群聊入站消息；“总客户”使用账号内去重客户聊天数。
- 统计响应只返回数量，不返回手机号、聊天 ID、HMAC、图片二进制、API Key 或绝对路径。
- 产品图片按完整产品名、SKU、标签别名规范化精确匹配；只发送唯一启用主图；歧义和 `UNKNOWN` 不自动重试。
- GLM 根地址与已带 `/chat/completions` 或 `/images/generations` 的地址都必须生成单一正确 endpoint。
- 客户图片预览必须继续走受保护媒体路由，支持放大、缩小、复位、Esc 关闭，并尊重 reduced-motion。

## Review Focus

- 午夜边界和时区：统计必须按 Asia/Shanghai 计算，不能用主机本地时区。
- 客户隔离：相同 HMAC 只在同一账号内计数，群聊和状态消息不能成为客户。
- 产品图片歧义：同一来源和优先级有多个启用主图时不发送；别名不能通过模糊子串命中。
- GLM URL 兼容：根地址、完整 endpoint、尾部斜杠和未知路径必须有明确测试，错误不能泄露密钥。
- 图片预览资源边界：预览只使用现有媒体引用，关闭/切换聊天后不能残留旧图片或缩放状态。

---

### Task 1: Add customer first-seen storage and overview aggregation

**Files:**
- Create: `panel/migrations/018_customer_first_seen.sql`
- Modify: `panel/app.py:init_db`, `panel/app.py:PanelState.handle_webhook`/archive helpers
- Modify: `panel/monitoring_service.py:MonitoringService.overview`
- Modify: `panel/overview_page.py`
- Modify: `panel/static/views/overview.js`
- Test: `tests/test_monitoring_service.py`
- Test: `tests/test_chat_upgrade.py`
- Test: `tests/frontend_syntax.test.mjs`

**Interfaces:**
- Produces `customer_first_seen(session_name, customer_key_hmac, first_inbound_at, last_inbound_at, created_at, updated_at)`.
- Produces `MonitoringService.overview(...)["customer_counts"]` and per-session `customer_counts` with `total` and `new_today`.
- Internal helper records one non-group inbound customer with an HMAC and is idempotent.

- [x] **Step 1: Write failing storage and aggregation tests**

  Add fixed-clock tests that insert two accounts and several inbound records, then assert total and today counts, account isolation, Shanghai midnight behavior, group/status exclusion, and duplicate inbound idempotence. Extend the read-only hash test to prove monitoring does not mutate the database.

- [x] **Step 2: Run the tests and verify the expected failure**

  Run: `& $python -m unittest -v tests.test_monitoring_service tests.test_chat_upgrade`

  Expected: FAIL because the migration, customer recording, and response fields do not exist.

- [x] **Step 3: Implement migration and webhook recording**

  Apply migration 018 idempotently from `init_db()`. After a message is safely archived and confirmed as a non-group inbound message, upsert the customer HMAC with the original inbound timestamp; never store the raw chat ID in this table.

- [x] **Step 4: Implement read-only aggregation**

  Add a Shanghai day-start helper in `MonitoringService`; query per-session counts from the new table and aggregate them for the selected scope. On missing/unreadable statistics, return `null` fields and a redacted partial error while preserving existing session/automation data.

- [x] **Step 5: Add overview UI**

  Add two top-level cards and two columns in the account table. Render unavailable counts as `暂不可用`, update on scope changes and polling, and keep all DOM text creation escaped through existing helpers.

- [x] **Step 6: Run task tests and frontend syntax checks**

  Run: `& $python -m unittest -v tests.test_monitoring_service tests.test_chat_upgrade`; `node --test tests/frontend_syntax.test.mjs`; `node scripts/check_frontend_syntax.mjs`.

  Expected: all existing tests plus the new customer-stat tests pass.

- [x] **Step 7: Commit the customer statistics slice**

  ```powershell
  git add panel/migrations/018_customer_first_seen.sql panel/app.py panel/monitoring_service.py panel/overview_page.py panel/static/views/overview.js tests/test_monitoring_service.py tests/test_chat_upgrade.py tests/frontend_syntax.test.mjs
  git commit -m "feat: show per-account customer statistics"
  ```

### Task 2: Extend product image metadata and deterministic matching

**Files:**
- Create: `panel/migrations/019_product_image_matching.sql`
- Modify: `panel/product_image_service.py`
- Modify: `panel/product_image_page.py`
- Modify: `panel/app.py` product-image upload/update routes and AI prompt/tool validation
- Test: `tests/test_product_image_service.py`
- Test: `tests/test_product_image_integration.py`
- Test: `tests/test_product_image_routes.py` (create if absent)

**Interfaces:**
- `ProductImageService.upload(..., product_name="", sku="", aliases=()) -> dict` stores safe matching metadata.
- `ProductImageService.update_asset()` accepts `product_name`, `sku`, and `aliases` with existing validation limits.
- `resolve_for_send(product_tag, variant="")` resolves exact product-name/SKU/alias/tag candidates by source rank, then priority, and returns `NOT_FOUND`, `AMBIGUOUS`, or `FOUND`.

- [x] **Step 1: Write failing metadata and resolver tests**

  Add tests for migration idempotence, Unicode NFKC and whitespace normalization, exact matches for product name/SKU/alias/tag, rejection of fuzzy substrings, source-rank selection, variant filtering, ambiguous primary assets, disabled assets, and safe metadata responses without filesystem paths.

- [x] **Step 2: Run product-image tests and verify the expected failure**

  Run: `& $python -m unittest -v tests.test_product_image_service tests.test_product_image_integration tests.test_product_image_routes`

  Expected: FAIL because the new columns, upload fields, resolver sources, and route/page controls do not exist.

- [x] **Step 3: Implement migration and service normalization**

  Add `product_name`, `sku`, and JSON aliases columns with bounded lengths. Normalize all values with NFKC, whitespace folding and `casefold()` for comparison; preserve display values separately. Keep existing tag/primary/priority and SHA256 behavior unchanged.

- [x] **Step 4: Implement resolver and AI authorization**

  Build candidate rows in Python from the normalized product fields and tags. Require the tool argument to be present as a complete token in the current customer message, then resolve by source rank (`product_name`, `sku`, `alias`, `tag`), priority, and uniqueness. Update the system rule and safe route payloads without exposing internal paths.

- [x] **Step 5: Update admin page and routes**

  Add product name, SKU, alias inputs and a small image icon in the product-image navigation/asset header. Keep upload/update responses safe and preserve CSRF/admin checks.

- [x] **Step 6: Run task tests and frontend checks**

  Run the three product-image test modules plus `node --test tests/frontend_syntax.test.mjs` and `node scripts/check_frontend_syntax.mjs`.

  Expected: all new and existing product-image tests pass, including duplicate webhook and `UNKNOWN` no-retry behavior.

- [x] **Step 7: Commit the product-image slice**

  ```powershell
  git add panel/migrations/019_product_image_matching.sql panel/product_image_service.py panel/product_image_page.py panel/app.py tests/test_product_image_service.py tests/test_product_image_integration.py tests/test_product_image_routes.py
  git commit -m "feat: improve deterministic product image matching"
  ```

### Task 3: Fix GLM full endpoint compatibility

**Files:**
- Modify: `panel/glm_service.py`
- Modify: `panel/ai_profiles.py` only if validation normalization is needed
- Test: `tests/test_glm_service.py`
- Test: `tests/test_ai_profiles.py`

**Interfaces:**
- `GLMService._endpoint(profile, suffix) -> str` accepts a root URL, a URL ending in the requested endpoint, or a URL ending in the other known GLM endpoint and returns one valid endpoint without duplicate suffixes.

- [x] **Step 1: Write failing endpoint tests**

  Add tests for root `/api/paas/v4`, `/api/paas/v4/`, `/api/paas/v4/chat/completions`, and `/api/paas/v4/images/generations` profiles. Assert chat and generation requests each contain exactly one expected suffix and preserve existing root behavior.

- [x] **Step 2: Run GLM tests and verify the expected failure**

  Run: `& $python -m unittest -v tests.test_glm_service tests.test_ai_profiles`

  Expected: FAIL for full endpoint profiles because the current adapter appends a duplicate suffix.

- [x] **Step 3: Implement safe endpoint normalization**

  Normalize only known trailing endpoint segments; preserve the configured origin/path, reject query/fragment as today, and never include the API key in exception text. Keep image-generation URL fetching and redirect protections unchanged.

- [x] **Step 4: Run GLM tests and compile check**

  Run: `& $python -m unittest -v tests.test_glm_service tests.test_ai_profiles`; `& $python -m compileall -q panel`.

  Expected: all GLM/profile tests pass.

- [x] **Step 5: Commit the GLM fix**

  ```powershell
  git add panel/glm_service.py panel/ai_profiles.py tests/test_glm_service.py tests/test_ai_profiles.py
  git commit -m "fix: normalize GLM configured endpoints"
  ```

### Task 4: Add protected customer-image preview and zoom controls

**Files:**
- Modify: `panel/chat_page.py`
- Modify: `tests/frontend_syntax.test.mjs`
- Modify: `tests/test_chat_features.py`

**Interfaces:**
- Adds a private `imagePreviewDialog` with `imagePreviewZoomIn`, `imagePreviewZoomOut`, `imagePreviewReset`, and `imagePreviewClose` controls.
- Adds `openMessageImage(message, imageElement)` and `closeMessageImage()` front-end helpers scoped to the selected chat.

- [x] **Step 1: Write failing DOM/script contract tests**

  Assert the chat page contains the preview dialog, zoom controls, keyboard close handling, and click binding for message images; assert the existing media endpoint remains the image source.

- [x] **Step 2: Run frontend tests and verify the expected failure**

  Run: `node --test tests/frontend_syntax.test.mjs`; `& $python -m unittest -v tests.test_chat_features.ChatPageRegressionTests`.

  Expected: FAIL because the dialog and handlers do not exist.

- [x] **Step 3: Implement preview UI**

  Add accessible dialog markup and CSS. Keep the image source as `/api/chat/sessions/{session}/media?...`; reset zoom and pan whenever the dialog closes or a different message opens; clamp zoom to a safe range and prevent stale object references.

- [x] **Step 4: Implement interactions**

  Bind click/keyboard activation to message images, provide pointer drag and wheel/touch scale behavior, close on Esc/backdrop/close button, and honor reduced-motion for dialog transitions.

- [x] **Step 5: Run frontend and chat regressions**

  Run: `node --test tests/frontend_syntax.test.mjs`; `node scripts/check_frontend_syntax.mjs`; `& $python -m unittest -v tests.test_chat_features tests.test_chat_upgrade`.

  Expected: all existing chat tests plus the new preview contract pass.

- [x] **Step 6: Commit the preview slice**

  ```powershell
  git add panel/chat_page.py tests/frontend_syntax.test.mjs tests/test_chat_features.py
  git commit -m "feat: preview and zoom customer images"
  ```

### Task 5: Full verification and review artifact

**Files:**
- Modify: `docs/releases/1.0.26.md` only if the final change needs a release note entry
- Create: `deployments/` verification artifact only if explicitly requested; no production deployment in this task

- [x] **Step 1: Run the complete local verification**

  Run: `& $python -m unittest discover -s tests -p 'test_*.py'`; `node --test tests/frontend_syntax.test.mjs`; `node scripts/check_frontend_syntax.mjs`; `& $python -m compileall -q panel`; `git diff --check`.

- [x] **Step 2: Review the diff against the design**

  Check migration order/idempotence, no secret/path leakage, no changes to WAHA/Compose settings, no accidental edits to the old `main` worktree, and no product-image behavior that bypasses the existing policy/ledger.

- [x] **Step 3: Report evidence and remaining limits**

  Report exact test counts, changed files, the GLM URL fix evidence, customer-count semantics, and that Docker/public deployment remains unverified until separately authorized and available.
