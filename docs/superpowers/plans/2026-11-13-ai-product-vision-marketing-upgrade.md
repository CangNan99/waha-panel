# AI 产品图片识别、产品图片库与主动营销升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox - [ ] syntax for tracking.

**Goal:** 在保留现有文字 AI、产品图片库和营销发送行为的前提下，让客户图片能够匹配产品目录并自动发送产品图，让聊天 AI 和营销活动可以受策略约束地自动发送产品图片。

**Architecture:** 以现有 GLMService、ProductImageService、ChatService 和 MarketingAutomationService 为基础，新增产品级目录、视觉匹配审计、营销活动阶段和统一的图片发送策略。GLM 只返回结构化候选，服务器完成产品映射、置信度判断、权限、冷却、幂等和发送；首期不引入向量数据库，通过影子模式和会话级开关分批启用。

**Tech Stack:** Python 标准库、SQLite、现有 DataCipher/ChatService/WahaClient、Pillow、原生 HTML/CSS/JavaScript、Python unittest、Node.js 前端脚本检查。

**Spec:** docs/superpowers/specs/2026-11-13-ai-product-vision-marketing-upgrade-design.md

## Global Constraints

- 采用方案 B：多模态识别、服务器二次匹配和受控自动发送。
- 产品图片和营销图片直接自动发送，不逐条要求人工确认。
- 高置信度、唯一产品候选且当前消息存在产品意图时才自动发送；默认置信度阈值为 0.90。
- 中等置信度、多候选或版本不清时自动询问客户，不发送产品图；低置信度或未知产品只回复文字。
- 图片输入最大 10 MiB、最大 40,000,000 像素，只允许 JPEG、PNG、WebP。
- 媒体读取最多 15 秒，模型请求最多 30 秒，视觉和生成请求共用最多 2 个并发槽位。
- 客户图片只保存 SHA-256 和匹配摘要，不保存原始图片；API Key、完整提示词和推理内容不得写入普通日志。
- AI 不得提交任意远程 URL、文件路径或内部资产编号；服务器必须重新解析产品和图片资产。
- 同一客户消息最多发送一张产品图片；同一产品遵守客户冷却和每日上限。
- 客户回复、退订或人工接管后，当前营销序列立即停止或暂停。
- 新迁移只新增表、列和索引，不删除现有数据；旧文字自动回复、翻译、聊天发送和旧图片库必须保持兼容。
- 不修改 WAHA API、Docker 网络、端口、域名或反向代理，不执行生产部署。
- 先支持影子模式和分批启用，只有本地测试、迁移检查和只读运行状态验证通过后才讨论构建或部署。

## Review Focus

- **错误产品自动发送：** 相似产品、多个候选、版本不明或模型自报高分但服务器无法唯一映射时，必须不发图；由 Task 3 和 Task 4 的匹配与回退测试覆盖。
- **重复或未知发送：** webhook 重复、网络重试、服务重启、文字已发送但图片未知时，不能重复发送；由 Task 2、Task 4 和 Task 7 的发送账本测试覆盖。
- **营销停止条件：** 客户回复、退订、人工接管、安静时段、每日上限和最大触达次数必须阻止后续图片和文字；由 Task 6 和 Task 7 的状态测试覆盖。
- **越权输入和隐私：** 模型返回远程 URL、内部 ID、任意文件路径或图片中的提示注入时，服务器拒绝并且日志不含原始数据；由 Task 3 和 Task 5 的协议测试覆盖。
- **旧数据和回滚：** 旧图片资产、标签、营销设置和文字发送在迁移后保持可用，旧版本可忽略新增表；由 Task 1 和 Task 9 的迁移回归测试覆盖。

---

### Task 1: Add the product catalog and compatibility migration

**Files:**
- Create: panel/migrations/021_product_catalog.sql
- Create: panel/product_catalog_service.py
- Modify: panel/product_image_service.py
- Modify: panel/app.py (init_db, PanelState.__init__, service wiring)
- Test: tests/test_product_catalog.py
- Test: tests/test_product_image_integration.py

**Interfaces:**
- ProductCatalogService.list_products(include_inactive: bool = False) -> dict
- ProductCatalogService.get_product(product_id: str, include_inactive: bool = False) -> dict
- ProductCatalogService.save_product(payload: dict) -> dict
- ProductCatalogService.disable_product(product_id: str) -> dict
- ProductCatalogService.resolve_candidate(candidate: dict, session_name: str) -> dict
- ProductCatalogService.reference_assets(product_id: str, variant: str = "") -> list[dict]
- ProductCatalogService.import_legacy_assets() -> dict

- [ ] **Step 1: Write failing migration and compatibility tests**

Add tests that execute the migration twice, verify the product catalog and product-to-asset relationship tables, and confirm that existing product image assets, tags, product_name, SKU, aliases, caption templates and send ledger rows remain readable.

Add tests for:

- product name and product key validation;
- duplicate product keys;
- inactive products;
- legacy assets that map uniquely;
- legacy assets that are ambiguous and remain marked for review;
- cross-session access being rejected by the caller.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

~~~text
python -m unittest tests.test_product_catalog tests.test_product_image_integration -v
~~~

Expected: FAIL because the new migration and service interfaces do not exist.

- [ ] **Step 3: Implement the additive schema and service**

Create product_catalog with stable product_key, product metadata, language content and automatic-send flags. Add product_id, variant, reference_role, allow_visual_match and allow_auto_send to product_image_assets through an idempotent application-side column check. Keep current tag and exact matching behavior.

Implement ProductCatalogService with strict validation, inactive filtering, deterministic legacy import and a review marker for ambiguous legacy assets. Do not store API keys or raw customer images in the new tables.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run the same unittest command. Expected: PASS, with the legacy image-library tests still passing.

- [ ] **Step 5: Commit the task**

~~~text
git add panel/migrations/021_product_catalog.sql panel/product_catalog_service.py panel/product_image_service.py panel/app.py tests/test_product_catalog.py tests/test_product_image_integration.py
git commit -m "feat: add product catalog and legacy image migration"
~~~

### Task 2: Extend the product image library and send resolution

**Files:**
- Modify: panel/product_image_service.py
- Modify: panel/product_image_page.py
- Modify: panel/app.py product-image routes and public payloads
- Create: panel/migrations/022_product_image_matching.sql
- Test: tests/test_product_image_matching.py
- Test: tests/test_product_image_integration.py
- Test: tests/frontend_syntax.test.mjs

**Interfaces:**
- ProductImageService.list_reference_images(product_id: str, variant: str = "") -> list[dict]
- ProductImageService.resolve_product_asset(product_id: str, variant: str = "", role: str = "primary") -> dict
- ProductImageService.record_match_audit(payload: dict) -> dict
- ProductImageService.match_test(image_bytes: bytes, mime_type: str, candidate_product_id: str | None = None) -> dict
- PanelHandler.send_product_image_get(route: str) and product-image write routes expose only non-sensitive fields.

- [ ] **Step 1: Write failing tests for product assets and match-test behavior**

Cover:

- multiple assets per product;
- primary, reference and marketing image roles;
- variant filtering;
- inactive asset rejection;
- deterministic primary-asset resolution;
- duplicate image hash handling;
- match-test returning candidates without sending to WhatsApp;
- public payloads excluding filesystem paths and raw image data.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

~~~text
python -m unittest tests.test_product_image_matching tests.test_product_image_integration -v
node --test tests/frontend_syntax.test.mjs
~~~

Expected: FAIL for the new role, variant and match-test cases.

- [ ] **Step 3: Implement asset roles, reference selection and audit storage**

Extend product image writes and reads with product, variant and role fields while preserving old form fields. Make resolve_product_asset choose exactly one active asset or return an explicit ambiguous/not-found result. Add match_test as an administrator-only route that validates an uploaded image but never calls send_image.

Upgrade the page with product-card editing, multiple assets, primary/reference/marketing role, variant, AI-send flags, product search and a non-sending match diagnostic. Keep the current soft-delete and preview behavior.

- [ ] **Step 4: Run focused tests and frontend syntax checks**

Expected: PASS for the new tests and all existing product-image and frontend syntax tests.

- [ ] **Step 5: Commit the task**

~~~text
git add panel/product_image_service.py panel/product_image_page.py panel/app.py panel/migrations/022_product_image_matching.sql tests/test_product_image_matching.py tests/test_product_image_integration.py tests/frontend_syntax.test.mjs
git commit -m "feat: extend product image roles and matching diagnostics"
~~~

### Task 3: Add structured GLM product-image recognition

**Files:**
- Modify: panel/glm_service.py
- Create: panel/product_match_service.py
- Modify: panel/app.py service wiring and error mapping
- Test: tests/test_product_match_service.py
- Test: tests/test_glm_state.py

**Interfaces:**
- GLMService.classify_product_image(profile: dict, prompt: str, image_bytes: bytes, mime_type: str, reference_images: list[dict] | None = None) -> dict
- ProductMatchService.build_prompt(catalog_context: dict, customer_text: str, conversation_context: str) -> str
- ProductMatchService.parse_result(raw: dict | str) -> dict
- ProductMatchService.evaluate(candidates: list[dict], catalog: list[dict], intent: str) -> dict
- ProductMatchService.classify_confidence(value: float) -> str

- [ ] **Step 1: Write failing protocol and parser tests**

Cover:

- valid structured result with one candidate;
- valid result with multiple candidates;
- malformed JSON;
- unknown fields;
- non-numeric or out-of-range confidence;
- model returning internal IDs, URLs or file paths;
- image prompt injection attempting to change policy;
- reference-image request body containing only validated data URLs;
- upstream timeout, HTTP error and invalid response.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

~~~text
python -m unittest tests.test_product_match_service tests.test_glm_state -v
~~~

Expected: FAIL because structured classification and the product-match policy do not exist.

- [ ] **Step 3: Implement the structured GLM adapter and server-side evaluator**

Build the request through the existing vision path. The adapter must validate image bytes, use the configured visual profile, optionally include a bounded set of product reference images, parse the response and return a sanitized result. The service must discard model-provided asset IDs, URLs and paths.

The evaluator maps model candidates to product catalog records, applies the initial 0.90 high-confidence threshold, detects ambiguity, checks customer intent and returns one of AUTO_SEND, ASK_CLARIFICATION, TEXT_ONLY or SKIP.

Do not write raw prompts, images or full model responses to logs.

- [ ] **Step 4: Run focused tests and verify they pass**

Expected: PASS with invalid model output rejected and valid requests producing the exact sanitized decision structure.

- [ ] **Step 5: Commit the task**

~~~text
git add panel/glm_service.py panel/product_match_service.py panel/app.py tests/test_product_match_service.py tests/test_glm_state.py
git commit -m "feat: add structured product image recognition"
~~~

### Task 4: Integrate customer-image recognition with automatic product-image sending

**Files:**
- Modify: panel/app.py webhook and automatic-reply flow
- Modify: panel/product_image_service.py send reservation and audit integration
- Modify: panel/chat_service.py only where the existing automated image ledger is required
- Test: tests/test_customer_image_product_flow.py
- Test: tests/test_product_image_integration.py

**Interfaces:**
- PanelState.handle_customer_product_image(session_name: str, chat_id: str, message_id: str, message: dict, incoming_text: str, context: str) -> dict
- PanelState.product_image_policy(settings: dict, session_name: str, chat_id: str, product_id: str, asset_id: str) -> dict
- ProductImageService.reserve_auto_send(session_name: str, chat_id: str, message_key: str, product_id: str, asset_id: str, source: str, caption: str) -> dict
- ProductImageService.finish_auto_send(request_id: str, state: str, message_id: str | None = None, error_code: str | None = None) -> dict

- [ ] **Step 1: Write failing end-to-end state tests**

Cover:

- high-confidence unique match sends exactly one image with the product caption;
- medium-confidence match asks for clarification without sending an image;
- low-confidence or unknown product falls back to text;
- no visual profile skips the image send;
- product auto-send switch, per-session switch and global kill switch;
- human takeover, opt-out, schedule and customer cooldown;
- duplicate webhook and retry behavior;
- WAHA image send failure, unknown result and service restart recovery;
- matching audit contains hash and decision but no raw image.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

~~~text
python -m unittest tests.test_customer_image_product_flow tests.test_product_image_integration -v
~~~

Expected: FAIL because the webhook path does not yet call the product-match decision or reserve the product image action.

- [ ] **Step 3: Implement the automatic customer-image path**

After the existing media validation and customer-state checks, call the product-match service only when the feature is enabled and a visual profile is available. Apply the decision before any send. For AUTO_SEND, reserve a single product-image request, resolve the validated asset, send through the existing automated-image path, and finish the ledger. For all other decisions, continue through the normal text/vision reply path without sending a product image.

Keep the existing duplicate, human-takeover, time-window, order-tool and message-archive rules in their current order. Never send a fixed misleading product response when recognition or image delivery fails.

- [ ] **Step 4: Run focused and existing reply tests**

Run:

~~~text
python -m unittest tests.test_customer_image_product_flow tests.test_product_image_integration tests.test_glm_state tests.test_chat_features -v
~~~

Expected: PASS with existing text and media behavior unchanged.

- [ ] **Step 5: Commit the task**

~~~text
git add panel/app.py panel/product_image_service.py panel/chat_service.py tests/test_customer_image_product_flow.py tests/test_product_image_integration.py
git commit -m "feat: auto-send matched product images for customer photos"
~~~

### Task 5: Expand the chat product-image tool and policy controls

**Files:**
- Modify: panel/app.py product-tool schema, validation and execution
- Modify: panel/chat_page.py only if product-image status or tool result is shown
- Modify: panel/product_image_page.py settings controls
- Test: tests/test_product_image_tool.py
- Test: tests/test_chat_features.py
- Test: tests/frontend_syntax.test.mjs

**Interfaces:**
- PanelState.product_image_tool_schema(session_name: str, settings: dict) -> list[dict]
- PanelState.validate_product_image_tool_call(arguments: dict, incoming_text: str, context: str) -> dict | None
- PanelState.execute_product_image_tool(session_name: str, chat_id: str, message_id: str, arguments: dict) -> dict

- [ ] **Step 1: Write failing tool-policy tests**

Cover:

- explicit product-image request;
- product comparison and product-effect request;
- unknown product;
- missing product interest;
- invalid product key, path, URL or internal asset ID;
- duplicate tool call in one customer message;
- product disabled for AI or marketing;
- cooldown, daily limit, opt-out and human takeover;
- caption length and unsafe-content validation.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

~~~text
python -m unittest tests.test_product_image_tool tests.test_chat_features -v
node --test tests/frontend_syntax.test.mjs
~~~

Expected: FAIL for the expanded tool arguments and policy behavior.

- [ ] **Step 3: Implement the policy-constrained tool**

Allow the model to provide product key, optional variant, short caption and reason. The server must resolve the product and asset, ignore model-supplied IDs and URLs, enforce the conversation policy and use the existing image send ledger. Keep the product tool unavailable when the product-image feature is disabled or no active asset exists.

Add separate switches for customer-image recognition, chat proactive product images and marketing product images, plus a global stop switch.

- [ ] **Step 4: Run focused tests and frontend checks**

Expected: PASS with tool calls accepted only for allowed contexts and all unsafe/ambiguous inputs rejected.

- [ ] **Step 5: Commit the task**

~~~text
git add panel/app.py panel/chat_page.py panel/product_image_page.py tests/test_product_image_tool.py tests/test_chat_features.py tests/frontend_syntax.test.mjs
git commit -m "feat: add policy-controlled chat product image tool"
~~~

### Task 6: Add marketing campaigns and per-contact state

**Files:**
- Create: panel/migrations/023_marketing_campaigns.sql
- Modify: panel/marketing_automation.py
- Modify: panel/chat_service.py candidate and stop-condition helpers
- Modify: panel/app.py migration wiring and settings payload
- Test: tests/test_marketing_campaigns.py
- Test: tests/test_marketing_automation.py

**Interfaces:**
- MarketingAutomationService.create_campaign(payload: dict) -> dict
- MarketingAutomationService.update_campaign(campaign_id: str, payload: dict) -> dict
- MarketingAutomationService.list_campaigns(include_inactive: bool = False) -> dict
- MarketingAutomationService.pause_campaign(campaign_id: str, reason: str) -> dict
- MarketingAutomationService.resume_campaign(campaign_id: str) -> dict
- MarketingAutomationService.contact_state(session_name: str, chat_key_hmac: str, campaign_id: str) -> dict | None
- MarketingAutomationService.transition_contact(state: dict, event: str, now: int) -> dict

- [ ] **Step 1: Write failing migration and state-machine tests**

Cover:

- idempotent campaign and contact-state migrations;
- campaign validation, target rules, cadence, maximum touches and quiet hours;
- eligible, contacted, follow-up, replied, qualified, paused, opted-out and completed transitions;
- customer reply stopping a campaign;
- opt-out and human takeover suppression;
- old global marketing settings remaining valid as defaults.

- [ ] **Step 2: Run focused tests and verify they fail**

Run:

~~~text
python -m unittest tests.test_marketing_campaigns tests.test_marketing_automation -v
~~~

Expected: FAIL because campaign tables and state transitions do not exist.

- [ ] **Step 3: Implement campaign and contact state persistence**

Create additive campaign, contact-state and event tables. Keep raw customer identifiers out of marketing tables; use the existing chat identity HMAC. Preserve the current candidate scan and send ledger as the compatibility path when no campaign is configured.

Implement explicit transitions and make customer reply, opt-out, human takeover, quiet hours, daily limit and maximum touch count terminal or pausing events as defined by the design.

- [ ] **Step 4: Run focused and existing marketing tests**

Expected: PASS with the current single-prompt marketing behavior unchanged when using legacy settings.

- [ ] **Step 5: Commit the task**

~~~text
git add panel/migrations/023_marketing_campaigns.sql panel/marketing_automation.py panel/chat_service.py panel/app.py tests/test_marketing_campaigns.py tests/test_marketing_automation.py
git commit -m "feat: add marketing campaigns and contact states"
~~~

### Task 7: Add marketing text-and-image delivery with unified idempotence

**Files:**
- Create: panel/marketing_delivery_service.py
- Modify: panel/marketing_automation.py
- Modify: panel/product_image_service.py
- Modify: panel/chat_service.py
- Create: panel/migrations/024_marketing_delivery.sql
- Test: tests/test_marketing_delivery.py
- Test: tests/test_marketing_automation.py
- Test: tests/test_product_image_integration.py

**Interfaces:**
- MarketingDeliveryService.reserve(delivery: dict) -> dict
- MarketingDeliveryService.send_text_and_optional_image(delivery: dict) -> dict
- MarketingDeliveryService.reconcile(delivery_id: str) -> dict
- MarketingDeliveryService.finish(delivery_id: str, state: str, error_code: str | None = None) -> dict
- MarketingAutomationService.generate_marketing_action(session_name: str, chat_id: str, history: list[dict], campaign: dict) -> dict

- [ ] **Step 1: Write failing delivery tests**

Cover:

- text-only delivery;
- text plus one product image;
- product image selection restricted to campaign product scope;
- customer reply arriving between reservation and send;
- text sent and image failed;
- text sent and image unknown;
- text unknown, which must prevent automatic image send;
- duplicate delivery request;
- service restart recovery;
- per-customer cooldown, daily cap and maximum touches.

- [ ] **Step 2: Run focused tests and verify they fail**

Run:

~~~text
python -m unittest tests.test_marketing_delivery tests.test_marketing_automation tests.test_product_image_integration -v
~~~

Expected: FAIL because marketing currently sends only one text request and has no delivery bundle state.

- [ ] **Step 3: Implement the delivery coordinator**

Reserve a campaign delivery before sending. Use derived request IDs for text and image, but keep one delivery state for the campaign contact. Send the text first. Only after confirmed text success may the image send be reserved. If text is unknown, do not send the image. If text succeeds and image is unknown, mark the delivery unknown and do not blindly retry either part. A confirmed image failure may be retried through a separate explicit reconciliation path.

Use the existing ProductImageService reservation and the existing automated send ledger instead of creating a second image-send implementation.

- [ ] **Step 4: Run focused, legacy and restart tests**

Expected: PASS with no duplicate or unsafe retry.

- [ ] **Step 5: Commit the task**

~~~text
git add panel/marketing_delivery_service.py panel/marketing_automation.py panel/product_image_service.py panel/chat_service.py panel/migrations/024_marketing_delivery.sql tests/test_marketing_delivery.py tests/test_marketing_automation.py tests/test_product_image_integration.py
git commit -m "feat: add idempotent marketing text and image delivery"
~~~

### Task 8: Add campaign, product and rollout administration UI

**Files:**
- Modify: panel/product_image_page.py
- Modify: panel/app.py settings page and routes
- Create: panel/marketing_page.py
- Test: tests/test_admin_product_marketing_routes.py
- Test: tests/frontend_syntax.test.mjs

**Interfaces:**
- Product catalog API payloads expose only validated public metadata and api_key_configured-style status fields.
- Marketing campaign payloads expose stage, counters and status but no raw customer IDs or sensitive content.
- Settings payload includes:
  - customer_image_product_match_enabled
  - chat_product_image_enabled
  - marketing_product_image_enabled
  - product_image_shadow_mode
  - product_image_global_kill_switch
  - configurable confidence, cooldown and limit values.

- [ ] **Step 1: Write failing route and frontend tests**

Cover:

- authenticated product catalog CRUD;
- authenticated campaign CRUD and pause/resume;
- admin-only match-test upload;
- public payload redaction;
- feature switch round trip;
- product page and marketing page scripts parse successfully;
- kill switch immediately prevents tool and marketing image actions.

- [ ] **Step 2: Run focused tests and verify they fail**

Run:

~~~text
python -m unittest tests.test_admin_product_marketing_routes -v
node --test tests/frontend_syntax.test.mjs
~~~

Expected: FAIL because campaign routes, new settings fields and pages do not exist.

- [ ] **Step 3: Implement the pages and routes**

Add a product catalog page or extend the existing product image page with product cards, image roles, variants, matching diagnostics and send switches. Add a marketing campaign page with campaign objective, target rules, product scope, cadence, maximum touches, quiet hours, image permission, pause/resume and event counters.

Add an AI settings section with separate customer-image, chat-tool and marketing-image switches, shadow mode and global stop. Keep the existing GLM profile fields and hidden API Key state.

- [ ] **Step 4: Run route, permission and frontend checks**

Expected: PASS with all new pages authenticated, redacted and syntactically valid.

- [ ] **Step 5: Commit the task**

~~~text
git add panel/product_image_page.py panel/marketing_page.py panel/app.py tests/test_admin_product_marketing_routes.py tests/frontend_syntax.test.mjs
git commit -m "feat: add product catalog and marketing campaign administration"
~~~

### Task 9: Run end-to-end local verification and prepare rollout artifacts

**Files:**
- Modify: docs/releases/ only if a release note is requested after code is complete
- Test: tests/test_product_catalog.py
- Test: tests/test_product_match_service.py
- Test: tests/test_customer_image_product_flow.py
- Test: tests/test_marketing_campaigns.py
- Test: tests/test_marketing_delivery.py
- Test: tests/test_glm_state.py
- Test: tests/test_chat_features.py
- Test: tests/test_product_image_integration.py
- Test: tests/test_marketing_automation.py
- Test: tests/frontend_syntax.test.mjs

**Interfaces:**
- The complete local harness exposes deterministic fake GLM and WAHA clients.
- The migration verifier reports schema before and after migration.
- The rollout checklist reports feature switches, kill switch, backup paths and current image/container version.

- [ ] **Step 1: Add missing cross-component regression tests**

Add one deterministic scenario covering: customer image webhook, product match, automatic product image send, matching audit, subsequent customer reply, marketing pause, and no duplicate send after retry.

- [ ] **Step 2: Run the full local test suite**

Run:

~~~text
python -m unittest discover -s tests -p "test_*.py" -v
node --test tests/frontend_syntax.test.mjs
~~~

Expected: PASS with no real customer session, external model request or exposed secret.

- [ ] **Step 3: Verify migration, backup and rollback conditions**

Use a temporary SQLite database and temporary product-image directory to verify migration idempotence, legacy asset readability, new table presence, and rollback to the old code path. Verify that the global kill switch stops all automatic product-image sends before any deployment discussion.

- [ ] **Step 4: Run local HTTP and shadow-mode checks**

Start the panel locally with fake GLM/WAHA adapters, call settings and product/campaign routes, upload a test image, execute a non-sending match test, run one shadow-mode webhook and inspect redacted logs. Do not use production credentials or customer data.

- [ ] **Step 5: Record verification evidence**

Record commands, exit codes, schema checks, local HTTP responses, feature-switch state, redacted logs and the current image/version. Do not claim deployment or production readiness until all evidence is present.

## Execution order and handoff

Tasks are sequential because Tasks 2–8 consume the product and catalog interfaces from Task 1 and share the existing app and sending boundaries. Each task must finish its focused tests before the next task starts.

The implementation method must be chosen before code changes begin:

- **Subagent-driven:** a fresh worker implements each task and a fresh reviewer checks it before the next task; best for the number of interacting state machines and sending failure modes in this project.
- **Native:** one implementer executes every task in this session, followed by one whole-branch review; faster but relies on the plan and same-session verification.

No task includes production deployment. Deployment requires a separate review after all local verification, backup checks, container health checks and real access checks are complete.
