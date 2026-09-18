# Task 4 Report: Persistent Follow-up Scheduler

## Scope

Implemented `ChatAutomationService` in `panel/chat_automation.py` with:

- encrypted one-time task creation for the exact `24h`, `3d`, `7d`, and `15d` delays;
- safe task listing and pending-task cancellation;
- atomic SQLite claiming before external calls;
- AI-generated and translated fixed-copy follow-up modes;
- terminal `SENT`, `SKIPPED`, `FAILED`, and `UNKNOWN` transitions;
- restart reconciliation through the automated-send idempotency ledger;
- five-hour human-takeover auto-resume;
- idempotent scheduler thread start/stop behavior.

## Safety and Recovery

- Raw chat IDs and fixed copy are encrypted with the existing chat `DataCipher` before persistence.
- Task payloads returned to callers omit ciphertext, raw chat IDs, fixed copy, request IDs, and Waha message IDs.
- Every automated send uses the task's persistent UUID through `send_automated_text()` and never changes manual-send or takeover state.
- Concurrent workers use `BEGIN IMMEDIATE`; only one worker can move a due task from `PENDING` to `RUNNING`.
- `UNKNOWN`, `FAILED`, cancelled, skipped, and sent tasks are never automatically selected again.
- Construction reconciles stale `RUNNING` rows to `SENT` when a Waha message ID exists in the task or send ledger, and to `UNKNOWN` otherwise.
- Logs contain only task ID, session technical name, terminal state, and bounded error code. Fixed copy, generated messages, upstream response text, and raw exception text are excluded.

## Verification

- TDD red step: focused scheduler suite initially failed because `panel.chat_automation` did not exist.
- Focused: `python -m unittest -v tests.test_chat_engagement.AutomationSchedulerTests` - 16 passed.
- Combined: `python -m unittest -v tests.test_chat_engagement tests.test_chat_features` - 49 passed.
- Frontend syntax: `node scripts/check_frontend_syntax.mjs` - passed; 4 inline scripts validated.
- Compile check: `python -m py_compile panel/chat_automation.py tests/test_chat_engagement.py` - passed.
- `git diff --check` - passed.

## Concerns

No open Task 4 concerns. Application construction, HTTP routes, and lifecycle ownership remain intentionally deferred to Task 5.

## Review Fix: Blocking Shutdown

Addressed the Task 4 P2 lifecycle finding:

- `stop()` now holds lifecycle ownership and waits without a timeout for the active worker to finish.
- `start()` is serialized by the same lock and cannot create a replacement worker before the prior join and cleanup complete.
- Added a blocking fake-AI regression that proves shutdown does not return before release, the old worker is dead afterward, and restart creates one worker without overlapping AI execution or duplicate sends.
- The intentional tradeoff is that an upstream call which violates its configured timeout can delay application shutdown; clean shutdown and duplicate-send prevention take precedence.

Review-fix verification:

- Focused scheduler suite - 17 passed.
- Combined Python suites - 50 passed.
- Frontend syntax gate - passed; 4 inline scripts validated.
- Python compile check and `git diff --check` - passed.
