# IPTV Playback Server Implementation Plan

> Execute inline using executing-plans and test-driven-development. User approved scope and deployment on 2026-10-24; no further implementation checkpoint.

**Goal:** Accept the supplier's successful hostless playlist response using an explicitly configured public HTTP(S) playback server.

**Architecture:** Keep the supplier API endpoint fixed. Validate the shared playback server before encryption and again at the response boundary. Complete only the missing authority on the known `/get.php` shape; preserve full supplier URLs. Keep trial reservations, reuse and delivery bookkeeping unchanged.

**Tech Stack:** Python unittest, SQLite/Fernet, embedded HTML/JS, Node syntax tests, Docker Compose via BT MCP.

**Spec:** `docs/superpowers/specs/2026-10-24-iptv-playback-server.md`

**Global Constraints:** Preserve API Key and other configuration. Never log credentials. No additional Demo creation or unsolicited customer messages. Preserve unrelated untracked plans. Do not alter or restart WAHA. Back up before deployment and keep exact rollback artifacts.

**Review Focus:** Fallback must not hide supplier errors, repair partial credentials, rewrite a supplied host or retry unknown outcomes; settings must remain shared and preserve encrypted secrets.

### Task 1: Response and shared configuration

Files: `panel/iptv_service.py`, `tests/test_iptv_service.py`.

Interfaces: optional `playback_server` shared setting; `ActivationClient(..., playback_server='')`; injected one-argument client factories remain supported.

1. Add synthetic hostless success tests, rejection cases, complete URL preservation and shared setting roundtrip tests.
2. Run focused unittest and observe failures for the missing behavior.
3. Implement strict public origin validation and missing-host completion; pass configured origin to the default client.
4. Prove service creation saves encrypted complete credentials and reuses the same reservation; unknown stays non-retriable.
5. Run focused tests; commit the completed backend change.

### Task 2: Settings interface

Files: `panel/app.py`, `tests/frontend_syntax.test.mjs`, `tests/iptv_settings_acceptance.mjs`.

Interfaces: consume Task 1's `playback_server`, include it in settings read/save, with global scope help.

1. Add a failing frontend assertion/acceptance case for the labeled playback address and save payload.
2. Add the input, rendering and collection; verify Node syntax tests and browser settings flow.
3. Run Python suite and request one fresh whole-diff review. Fix material findings, rerun affected checks, commit.

### Task 3: Deploy and verify

Files: task deployment evidence outside repository; remote isolated deployment folder.

Interfaces: freeze reviewed source; preserve current Compose stack and encrypted configuration.

1. Build and run full suite in isolated container through BT MCP; no provider writes.
2. Back up consistent SQLite, current configuration, image reference and rollback procedure.
3. Configure only playback_server in encrypted shared settings, preserving existing key and all other values; replace only panel service.
4. Verify container health/logs, public authenticated page, persistence and response normalization using existing encrypted diagnostic response.
5. Inspect whether old unknown can be matched safely; only recover if proven, otherwise retain it and report the precise limitation. No message sends.
6. Save deployment evidence and report verified results, rollback path and remaining limits.
