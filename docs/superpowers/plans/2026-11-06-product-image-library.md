# Product Image Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent product image library with tagged assets and a strictly matched AI tool that automatically sends one approved product image to the current customer.

**Architecture:** Store validated image files under the existing panel data volume and store asset metadata, tags, settings, and send idempotency in SQLite. Add a dedicated product-image service, admin APIs/page, and one `send_product_image` AI function that resolves a unique active primary image before calling a new automated image sender on `ChatService`.

**Tech Stack:** Python 3.12, `sqlite3`, Pillow, existing `ThreadingHTTPServer` routes, existing admin auth/CSRF, existing WAHA `sendImage` client, inline HTML/JavaScript pages, unittest, Node syntax tests, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-06-product-image-library-design.md`

## Global Constraints

- Use the existing `panel_data` volume; do not add object storage, CDN, or a public image URL.
- Accept only JPEG, PNG, and WebP with the existing 10 MB and 40,000,000 pixel limits and real-content validation.
- AI sends only after an exact normalized product-tag match resolves to one active primary image.
- AI sends directly when the product-image setting is enabled; keep a global kill switch and daily limit with default `auto_send_enabled=1`, `daily_limit=20`.
- `UNKNOWN` WAHA image-send results must not be retried automatically.
- Never expose customer identifiers, server paths, image bytes, credentials, or full customer messages in AI responses or ordinary logs.
- Upgrade only the panel container; preserve WAHA, sessions, media, panel data, and rollback image `1.0.24`.
- Do not stage or modify the four pre-existing untracked plan files under `docs/superpowers/plans/`.

## Review Focus

- Spoofed MIME, path traversal, oversized/decompression-bomb image: `ProductImageService` validation tests must reject before writing a file.
- Duplicate webhook/tool call and WAHA `UNKNOWN`: automated image send tests must prove one external request and no automatic retry.
- Ambiguous or disabled tags: resolver tests must prove no image is sent without a unique active primary asset.
- Non-admin upload/preview and missing CSRF: route tests must prove authorization and no absolute-path leakage.
- SQLite/file backup mismatch: migration and deployment verification must compare asset metadata and image files after restore.

---

### Task 1: Add the product-image schema and storage service

**Files:**
- Create: `panel/migrations/014_product_image_library.sql`
- Create: `panel/product_image_service.py`
- Modify: `panel/app.py:init_db()` and `PanelState.__init__()`
- Test: `tests/test_product_image_service.py`

**Interfaces:**
- Produces `ProductImageService(database_path: Path, data_root: Path, clock=None, logger=None)`.
- Produces `settings_payload() -> dict`, `save_settings(payload: dict) -> dict`.
- Produces `upload(filename: str, declared_type: str, data: bytes, title: str, tags: list[str], caption_template: str = "") -> dict`.
- Produces `list_assets(tag: str = "", include_inactive: bool = False) -> dict`, `get_asset(asset_id: str, include_inactive: bool = False) -> dict`, `preview(asset_id: str) -> tuple[str, bytes]`.
- Produces `update_asset(asset_id: str, payload: dict) -> dict`, `disable_asset(asset_id: str) -> dict`, and `resolve_for_send(product_tag: str, variant: str = "") -> dict`.

- [ ] **Step 1: Write failing schema and service tests**

  Add tests proving that `init_db()` creates `product_image_settings`, `product_image_assets`, `product_image_tags`, `product_image_asset_tags`, and `product_image_send_requests`; a second `init_db()` is idempotent; settings default to `auto_send_enabled=True` and `daily_limit=20`; and a valid upload returns metadata without returning an absolute path.

- [ ] **Step 2: Add adversarial upload tests**

  Test path traversal names, mismatched MIME/extension/content, non-image bytes, over-10 MB data, over-40,000,000-pixel images, and duplicate SHA256 uploads. Assert no invalid file is left under `product-images/`.

- [ ] **Step 3: Run the service tests and verify failure**

  Run: `python -m unittest -v tests.test_product_image_service`

  Expected: FAIL because the migration and service do not exist yet.

- [ ] **Step 4: Implement the migration and service**

  Create the exact tables and indexes from the approved spec. Normalize tags with Unicode NFKC, whitespace folding, and `casefold()`. Store files at `product-images/<sha256-prefix>/<asset_id>.<jpeg|png|webp>` using a temporary file and `os.replace()`. On duplicate SHA256, return the existing asset with `reused=True` and do not silently merge metadata. `resolve_for_send()` must return `NOT_FOUND` or `AMBIGUOUS` instead of guessing.

- [ ] **Step 5: Wire initialization**

  Apply migration 014 after the existing migrations and construct `PanelState.product_images` with `database_path` and `database_path.parent / "product-images"`. Keep initialization safe when the directory is absent.

- [ ] **Step 6: Run the service tests and verify success**

  Run: `python -m unittest -v tests.test_product_image_service`

  Expected: all tests pass, including migration idempotence, validation, dedupe, tag resolution, and soft disable.

- [ ] **Step 7: Commit the storage layer**

  ```bash
  git add panel/migrations/014_product_image_library.sql panel/product_image_service.py panel/app.py tests/test_product_image_service.py
  git commit -m "feat: add product image library storage"
  ```

### Task 2: Add automated image sending and its idempotency boundary

**Files:**
- Modify: `panel/chat_service.py`
- Modify: `tests/test_chat_engagement.py`
- Test: `tests/test_chat_engagement.py`

**Interfaces:**
- Produces `ChatService.send_automated_image(session: str, chat_id: str, filename: str, declared_type: str, data: bytes, caption: str, client_request_id: str, origin: str = "product_image") -> dict`.
- The method validates image bytes with the existing rules, sends through the configured WAHA client, archives the outbound media context with `origin="product_image"`, and returns `{request_id, state, kind, message_ref?}`.
- `ProductImageService` remains the owner of `product_image_send_requests`; `ChatService` does not write manual-send rows or own product-image idempotency.

- [ ] **Step 1: Write failing automated-image tests**

  Add tests for a successful send, invalid image rejection, origin-aware outbound archiving, and a timeout/error that returns `UNKNOWN` without a second client call. Idempotency and request-ID conflict belong to Task 4 because the product-image service owns that ledger.

- [ ] **Step 2: Run the new tests and verify failure**

  Run: `python -m unittest -v tests.test_chat_engagement.ChatServiceEngagementTests`

  Expected: FAIL because `send_automated_image` is not defined.

- [ ] **Step 3: Implement `send_automated_image`**

  Reuse `_validated_image`, `_payload_hash`, `_error_state`, `_notify_outbound`, and the existing Waha client call shape. Keep the method independent from `manual_send_requests` and do not change the behavior of `send_image`, `send_text`, or `send_automated_text`.

- [ ] **Step 4: Run the chat-service tests**

  Run: `python -m unittest -v tests.test_chat_engagement.ChatServiceEngagementTests`

  Expected: all existing and new automated-image tests pass.

- [ ] **Step 5: Commit the send primitive**

  ```bash
  git add panel/chat_service.py tests/test_chat_engagement.py
  git commit -m "feat: add idempotent automated image sending"
  ```

### Task 3: Add admin APIs and the product-image management page

**Files:**
- Create: `panel/product_image_page.py`
- Modify: `panel/app.py` (`PanelHandler` routes and multipart handling)
- Modify: `panel/app_shell.py` (navigation entry)
- Test: `tests/test_product_image_routes.py`
- Test: `tests/frontend_syntax.test.mjs`

**Interfaces:**
- Adds the authenticated routes `GET/POST /api/product-images`, `GET /api/product-images/{id}/preview`, `PUT/DELETE /api/product-images/{id}`, `GET/POST/PUT/DELETE /api/product-tags`, and `GET/PUT /api/product-image-settings`.
- Adds a page route `/product-images` and `product_image_page()` with upload, tag filter, preview, priority, primary, active/disabled, soft-delete, and global auto-send settings.
- Upload responses return only safe metadata: asset ID, title, tags, MIME, size, dimensions, active state, priority, and timestamps.

- [ ] **Step 1: Write failing route and authorization tests**

  Test authenticated admin GET/POST/PUT/DELETE, CSRF rejection for mutations, non-admin rejection, preview response headers, no absolute paths in JSON or HTML, tag CRUD, settings round-trip, and multipart upload through the existing request parser.

- [ ] **Step 2: Run route tests and verify failure**

  Run: `python -m unittest -v tests.test_product_image_routes`

  Expected: FAIL because the page and routes do not exist.

- [ ] **Step 3: Implement service-backed routes**

  Add `PanelHandler` helpers that call `ProductImageService`; enforce `require_admin_auth()` for reads and `require_csrf()` for writes. Keep preview responses private and use `X-Content-Type-Options: nosniff`. Do not expose `relative_path`, filesystem paths, SHA256-derived paths, or image bytes in metadata responses.

- [ ] **Step 4: Implement the management page**

  Keep page JavaScript inline or in the established page generator style. Add safe DOM rendering, upload progress/error state, tag editing, primary selection, active toggle, soft-delete confirmation, and settings save. Never insert server-provided titles or tags with `innerHTML` without escaping.

- [ ] **Step 5: Run route and frontend tests**

  Run: `python -m unittest -v tests.test_product_image_routes`; `node --test tests/frontend_syntax.test.mjs`; `node scripts/check_frontend_syntax.mjs`

  Expected: all route, authorization, preview-header, and JavaScript syntax tests pass.

- [ ] **Step 6: Commit the admin surface**

  ```bash
  git add panel/product_image_page.py panel/app.py panel/app_shell.py tests/test_product_image_routes.py tests/frontend_syntax.test.mjs
  git commit -m "feat: add product image library admin page"
  ```

### Task 4: Integrate the strict AI `send_product_image` tool

**Files:**
- Modify: `panel/app.py` (`PanelState`, `_ai_reply`, webhook tool execution)
- Modify: `panel/product_image_service.py` (send reservation helpers)
- Test: `tests/test_product_image_integration.py`
- Test: `tests/test_chat_upgrade.py` (route/auth and AI tool contract)

**Interfaces:**
- Adds `PanelState._execute_product_image(session, chat_id, message_id, original_timestamp, ticket, arguments, incoming, context, dispatch) -> dict`.
- Adds the single AI function `send_product_image(product_tag: str, variant: str = "", caption: str = "")` with `additionalProperties=False` and required `product_tag`.
- Adds a safe product-image system-rule block to `_ai_reply()` and includes the tool only when `product_image_settings.auto_send_enabled=1` and at least one active asset exists.
- Produces `ProductImageService.reserve_send(session_name: str, chat_id: str, message_key: str, asset_id: str, tag_key: str, caption: str) -> dict | None` and `finish_send(request_id: str, state: str, message_id: str | None = None, error_code: str | None = None) -> None`.

- [ ] **Step 1: Write failing AI integration tests**

  Add tests for: exact tag match sends one image; repeated webhook/tool call sends once; no match returns a normal ignored result and sends nothing; ambiguous tags send nothing; disabled assets send nothing; human takeover and disconnected sessions send nothing; daily limit blocks the next send; invalid tool arguments are rejected; `UNKNOWN` is persisted and not retried.

- [ ] **Step 2: Run integration tests and verify failure**

  Run: `python -m unittest -v tests.test_product_image_integration`

  Expected: FAIL because the AI tool and executor do not exist.

- [ ] **Step 3: Implement the tool contract**

  Extend `_ai_reply()` with the exact function schema and rules: only current explicit product interest authorizes the tool; no guessing from history; no more than one image per message; never reveal internal IDs or paths. Preserve the current single-tool-call behavior.

- [ ] **Step 4: Implement `_execute_product_image()`**

  Validate tool arguments and policy ticket, resolve a unique active primary via `resolve_for_send()`, enforce daily limit, reserve the request in `product_image_send_requests`, read the validated file, call `send_automated_image()`, update `SENT/FAILED/UNKNOWN`, and emit redacted `product_image.*` logs. Return safe action/mode text without credentials, paths, or customer identifiers.

- [ ] **Step 5: Run integration and regression tests**

  Run: `python -m unittest -v tests.test_product_image_integration tests.test_iptv_integration tests.test_chat_upgrade`

  Expected: product-image tests pass and existing IPTV, translation, route, and AI tool tests remain green.

- [ ] **Step 6: Commit the AI integration**

  ```bash
  git add panel/app.py panel/product_image_service.py tests/test_product_image_integration.py tests/test_chat_upgrade.py
  git commit -m "feat: let AI send tagged product images"
  ```

### Task 5: Add backup, release metadata, documentation, and operational checks

**Files:**
- Modify: `.env.example`, `docker-compose.yml`, `install.sh`, `install.ps1`, `panel-release.json`, `panel/app.py`, `panel/update_service.py`
- Modify: `README.md`, `README.zh-CN.md`, `INSTALL.zh-CN.md`
- Create: `docs/releases/1.0.25.md`
- Modify: `backup.sh`, `backup.ps1` if the existing archive output needs an explicit product-image manifest
- Test: `tests/test_chat_features.py`, `tests/test_chat_upgrade.py`, `tests/test_product_image_service.py`

**Interfaces:**
- Release defaults become `1.0.25`; rollback target remains `1.0.24`.
- Backup verification reports the SQLite file and `product-images` directory in the same backup set.

- [ ] **Step 1: Write failing release and backup assertions**

  Assert release metadata, Compose defaults, installers, update-service default, 1.0.25 release notes, migration presence, and backup documentation all mention the product-image directory and the 1.0.24 rollback image.

- [ ] **Step 2: Update metadata and docs**

  Document automatic direct sending, strict tag/primary rules, `UNKNOWN` no-retry behavior, admin-only management, backup/restore coupling, and kill-switch defaults. Do not include actual credentials, customer IDs, image bytes, or production paths beyond the documented data-volume layout.

- [ ] **Step 3: Run release and documentation tests**

  Run: `python -m unittest -v tests.test_chat_features tests.test_chat_upgrade tests.test_product_image_service`; `git diff --check`

  Expected: all release, migration, and backup assertions pass.

- [ ] **Step 4: Commit release preparation**

  ```bash
  git add .env.example docker-compose.yml install.sh install.ps1 panel-release.json panel/app.py panel/update_service.py README.md README.zh-CN.md INSTALL.zh-CN.md docs/releases/1.0.25.md backup.sh backup.ps1 tests/test_chat_features.py tests/test_chat_upgrade.py tests/test_product_image_service.py
  git commit -m "release: prepare product image library 1.0.25"
  ```

### Task 6: Full verification and production deployment

**Files:**
- No product-code files are changed in this task.
- Operational artifacts: release branch, Docker image, backup manifest, deployment override.

**Interfaces:**
- Publishes `docker.io/cangnan88/waha-panel:1.0.25` for `linux/amd64`.
- Keeps `devlikeapro/waha:latest-2026.9.2` unchanged and updates only `waha-panel`.

- [ ] **Step 1: Run the complete local verification**

  Run: `python -m unittest discover -s tests -p 'test_*.py'`; `node --test tests/frontend_syntax.test.mjs tests/chat_iptv_pagination.test.mjs`; `node scripts/check_frontend_syntax.mjs`; `python -m compileall -q panel`; `git diff --check`.

  Expected: all tests pass, JavaScript syntax passes, Python compilation passes, and no diff-check errors occur.

- [ ] **Step 2: Create the production rollback backup**

  Back up `.env`, Compose and deployment overrides, the panel SQLite database, the `panel_data` volume, and the complete `product-images` directory. Record SHA256 for the SQLite backup and a file manifest for image assets.

- [ ] **Step 3: Build and publish the image**

  Build with the Node frontend-check stage, push `docker.io/cangnan88/waha-panel:1.0.25`, and verify the Docker Hub tag is `linux/amd64` with the expected digest.

- [ ] **Step 4: Deploy only the panel container**

  Pull the image and run Compose `up -d --no-deps waha-panel`; do not restart or recreate WAHA.

- [ ] **Step 5: Verify migration and runtime behavior**

  Confirm the new tables, default settings, asset directory, core-table counts, panel startup log, panel container state, WAHA container identity/version, authenticated HTTP boundary, and runtime tool source. Use a synthetic test asset in a temporary database for an end-to-end send check; do not send a real customer image during deployment verification.

- [ ] **Step 6: Verify rollback readiness and publish GitHub release**

  Confirm the 1.0.24 image remains available, the backup manifest is readable, and the release notes contain no secrets. Push the reviewed branch, merge to `main`, tag `v1.0.25`, and publish the GitHub Release only after the production checks pass.
