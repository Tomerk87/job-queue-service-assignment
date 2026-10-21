# Job Queue Service — Execution Evidence

Per-phase record of commands actually run, results, coverage, deviations and commits. Checklists live in `PLAN.md`.

## Phase 1 — Foundation and API

_Recorded 2026-10-21._

- **Environment:** macOS host, Docker 29.8.2 / Compose v5.6.0. Containers run Python 3.12.15, postgres:16-alpine and redis:7-alpine. Local Python is 3.9, so all Python runs in containers.
- **Decisions approved by the human before execution:**
  - `scheduled_at`: past values within a 30s tolerance (configurable, inclusive) become PENDING. Older, naive or invalid values return 422. Values are normalized to UTC. TECHNICAL_SPEC §3, §5 and EC-01 were updated.
  - Tests run inside Docker against the Compose Postgres (`jobqueue_test`).
  - The retry endpoint is omitted until Phase 3.
  - Migrations run in a one-shot `migrate` service.
  - Sync SQLAlchemy 2.0 with psycopg 3.
  - Dependencies: `pyproject.toml` plus pinned `requirements*.txt`.
  - Health returns 200 ok/degraded and 503 unhealthy.
  - List `limit` is 1..100 (default 50).
- **Build:** `docker compose build` and `docker compose --profile test build tests` both succeeded.
- **Test suite:** `docker compose --profile test run --rm --build tests pytest -v --cov=app --cov-report=term-missing` → **68 passed, 0 failed, 0 skipped** (1.24s, fresh database volume).
  - `tests/test_schedule.py`: 10 unit tests. They cover `scheduled_at` boundaries (none, future, now, exactly 30s past, 30s+1µs, far past, zero tolerance, +03:00 → UTC) and config bounds.
  - `tests/test_api.py`: 51 tests.
    - EC-01: all 4 types created and fetched; 18 invalid bodies return 422 and create no rows; scheduled/recent-past/too-old via the API.
    - EC-02: 404 for get and cancel. Cancel PENDING/SCHEDULED → 200, repeat → 200, PROCESSING/COMPLETED/FAILED → 409.
    - Also: list order, pagination, filters and bounds; health ok/degraded/unhealthy; submit succeeds with Redis down; Redis wake-up published after commit; retry route absent.
  - `tests/test_idempotency.py`: 7 tests on real PostgreSQL.
    - Same key returns the original (also with a different payload).
    - Key retained ≥24h.
    - **EC-03:** 10 threads behind a barrier with the same key → exactly 1 job, 1 log row, 1 created and 9 returned the same id.
    - **EC-04:** an expired key maps to a new job and the old job is retained. In the 10-thread race on an expired key, exactly one new job is created.
- **Flakiness check:** 30 repeated runs of the two concurrent tests (`pytest -q -k concurrent tests/test_idempotency.py` in a loop) → 30 passed, 0 failed.
- **Coverage (informational; no per-phase gate):** 94% total line coverage, 338 statements, 19 missed.
  - `jobs.py` 99%. The only uncovered line is the bounded-retry `RuntimeError`, reachable only if a key's TTL were shorter than one request.
  - `config.py`, `db.py`, `main.py`, `models.py`, `notifier.py` and `schemas.py`: 100%.
  - `worker.py` 0%: the idle entrypoint is covered by the smoke test below, not by pytest. Nothing is excluded from measurement yet.
- **Smoke (`docker compose down -v && docker compose up -d --build --wait`, then curl):**
  - `migrate` exited 0 and ran revision 0001. api was healthy and the worker started idle.
  - `/health` → 200 ok.
  - POST with key → 201; same key → 200 with the same id; scheduled +1h → 201 SCHEDULED; invalid → 422.
  - GET → 200; list by type → 200.
  - Cancel → 200, cancel again → 200; unknown id → 404; retry → 404.
  - After `stop redis`: `/health` → 200 degraded, POST → 201, and a warning was logged with the job_id.
  - After `stop postgres`: `/health` → 503 unhealthy, POST → 500 (no false success).
  - `stop worker` → SIGTERM was logged and the worker exited with code 0.
- **Defects found and fixed during the phase:**
  1. POST echoed `scheduled_at` with the client's offset while the database returned UTC. Fixed by normalizing to UTC in the schema validator; caught by `test_offset_scheduled_at_is_stored_as_same_instant`.
  2. `migrate` failed with `ModuleNotFoundError: app` because the alembic CLI does not add the cwd to `sys.path`; tests had masked this through pytest's `pythonpath`. Fixed with `prepend_sys_path = .` in `alembic.ini` and caught by the Compose smoke run.
- **Simplicity/efficiency review:**
  - Runtime dependencies: fastapi, uvicorn, SQLAlchemy, psycopg, alembic, redis. There is no settings or validation library beyond pydantic (bundled with FastAPI).
  - One module per responsibility: config, db, models, schemas, jobs (persistence/transitions), notifier, main (routes), worker.
  - Every write runs in one short `session.begin()` transaction. Idempotency is a single `INSERT ... ON CONFLICT DO UPDATE ... WHERE expires_at <= now()`; on conflict the losing transaction rolls back and reads the active mapping (bounded to 2 passes).
  - Cancel is one conditional UPDATE with RETURNING.
  - The Redis call comes after commit, with 0.5s timeouts, and failures are logged rather than silently swallowed.
  - DB time `now()` drives scheduling and key expiry. `claim_token` is never serialized.
  - Indexes: partial eligibility, lease and scheduled indexes, plus (status, created_at) and (type, created_at) for list filters.
- **Known limitations (not defects):**
  - Unfiltered listing and health counts scan `jobs` (bounded by `limit`, acceptable at this scale).
  - The losing duplicate submission inserts and then rolls back a job row (cheap, and keeps one code path).
  - Starlette's TestClient emits a third-party `DeprecationWarning`.
- **Deviations:**
  - The plan's test list gained one line for the `scheduled_at` boundary tests, at the human's request.
  - The retry route is absent rather than stubbed, as the plan allows.
  - After the phase review, TECHNICAL_SPEC wording was aligned to "phase" and `PLAN.md` at the human's request. No requirement changed.
- **Commit:** `d659188` on branch `phase_1_implementation` (Phase 1 checkpoint; rollback point `810444a`). Human approval for Phase 1 granted on 2026-10-21; status set to COMPLETED.

## Phase 2 — Worker and Mock Execution

_Recorded 2026-10-21._

- **Branch:** `phase_2_implementation`, created by the human from `main` after PR #1 was merged (base `1b48a44`).
- **Decisions approved by the human before execution:**
  - Until Phase 3, a handler error is terminal: PROCESSING → FAILED, token-fenced, through `record_failure`, which Phase 3 replaces with the retry policy.
  - Webhook determinism comes from an injectable `random.Random`/`sleep` plus `WEBHOOK_FAILURE_RATE`.
  - Batch is capped at 100 items with 0.2s per item, so a job takes ≤ 20s, under the 30s lease. This changes the Phase 1 limit of 1000.
  - The happy path is tested in-process against real PostgreSQL, plus a Compose smoke run; automated Compose E2E stays in Phase 5.
  - The claim orders by `priority DESC, created_at ASC` now. The Phase 3 checkbox and EC-07 are still verified in Phase 3.
- **Test suite:** `docker compose --profile test run --rm --build tests pytest -q --cov=app --cov-report=term-missing` → **113 passed, 0 failed, 0 skipped** (3.88s).
  - Per file: `test_api.py` 53, `test_handlers.py` 14, `test_idempotency.py` 7, `test_schedule.py` 17, `test_worker.py` 22.
  - New in Phase 2: batch 100 items accepted and 101 rejected; worker config bounds; handler units; worker integration.
- **Edge cases verified:**
  - **Happy path:** submit via API → `run_once` → COMPLETED for all 4 types, with result, progress 100, attempts 1, worker_id and timestamps. Token and lease are cleared. Logs: created → claimed → completed.
  - **Failure:** a simulated webhook failure or an unexpected handler exception → FAILED with `last_error`, and the worker keeps running.
  - **EC-06:**
    - 10 threads behind a barrier race for 1 job → exactly 1 claim.
    - 10 threads drain 30 jobs → 30 unique claims, each at attempt 1.
  - **EC-05:** 20 rounds of a cancel-vs-claim barrier race → exactly one winner each round, and the final status matches the winner.
  - **EC-16:** batch progress is monotonic and ends at 100 (unit tests for 1, 3, 7 and 100 items). Persisted progress observed mid-run was 0/20/40/60/80, then the job completed with `{total, processed}`.
  - **EC-14:**
    - With Redis unreachable for both API and worker, the real `run()` loop completes the job by polling.
    - `Wakeup` returns early on a notification, and falls back to a timed wait when Redis is down.
  - **Fencing baseline:** progress, complete and failure with a wrong token are rejected and leave the row unchanged. A worker whose token was replaced does not complete the job, and a batch stops at the rejected progress write.
  - **Ineligible jobs:** SCHEDULED, CANCELLED and future-`next_run_at` jobs are never claimed.
  - **DB outage:** the worker loop logs the error and retries about every poll interval, with no crash and no busy loop.
- **Flakiness check:** 30 repeated runs of the 8 race/outage tests (`pytest -q -k "concurrent or race or redis_is_down or outage"`) → 30 passed, 0 failed.
- **Coverage (informational):** 97% total line coverage (513 statements, 14 missed).
  - `handlers.py` 100%.
  - `jobs.py` 99%; the only uncovered line is the Phase 1 bounded-retry `RuntimeError`.
  - `worker.py` 88%. Uncovered are `main()` signal wiring (exercised by the smoke run) and the error path of closing a broken pub/sub connection.
- **Query plan:** `EXPLAIN` of the claim subquery shows `Index Scan using ix_jobs_pending_priority` under `LockRows`. Sequential scans had to be disabled to show it, because the test table is tiny.
- **Compose smoke** (`docker compose down -v && docker compose up -d --build --wait`, then `scratchpad/smoke2.sh`):
  1. Email, webhook and report → COMPLETED at attempt 1 with worker_id `8940d33dddfa-1`.
  2. A 100-item batch was polled every 3s at progress 14 → 29 → 44 → 59 → 73 → 88 → COMPLETED 100 `{total: 100, processed: 100}`.
  3. With the worker stopped, a cancel returned 200, and the job stayed CANCELLED after the worker restarted.
  4. A one-off worker with `WEBHOOK_FAILURE_RATE=1` turned the webhook to FAILED with `SimulatedFailure: ...`.
  5. With Redis stopped, a report job still COMPLETED, and the worker logged "redis wake-ups unavailable (ConnectionError); relying on database polling".
  6. `docker compose stop worker` during a 40-item batch logged SIGTERM. The job went from progress 5 to COMPLETED 100 before exit, the stop took 9s (within the 30s grace period), and the worker exited with code 0.
- **Simplicity/efficiency review:**
  - No new dependencies.
  - All state transitions remain in `app/jobs.py`: claim, a single `_fenced_update` helper for progress/complete/failure, and cancel.
  - Handlers are plain functions with a small context dataclass. `Wakeup` is the only new class; it holds pub/sub state.
  - Each DB operation uses its own short session and transaction; no session or lock is held while a handler runs.
  - Idle wait is bounded by `WORKER_POLL_INTERVAL_SECONDS` (≤ 2s). On DB errors the loop waits a poll interval.
  - Logs contain job_id, worker_id and exception type, never payloads.
- **Known limitations:**
  - If the final complete or failure write fails because the DB is down, the job stays PROCESSING until Phase 4 lease recovery.
  - Leases are set but not renewed or recovered (Phase 4).
  - A batch writes one short progress transaction per item (at most 100).
  - The starlette TestClient `DeprecationWarning` is unchanged.
- **Deviations:**
  - Failures are terminal until Phase 3.
  - The batch limit changed from 1000 to 100: the TECHNICAL_SPEC §3 payload note and README were updated.
  - Priority ordering was written early; it is still verified in Phase 3.
  - Worker Compose `stop_grace_period: 30s` was added ahead of Phase 4's shutdown work.
- **Commit:** the first Phase 2 checkpoint is `0f80d7e` on `phase_2_implementation`; rollback point `1b48a44`.
- **Revision pass (2026-10-21, human-requested: improvements, simplicity, efficiency):**
  - *Simpler DB access:* `run_once` now uses one `Session` per job on the worker thread instead of a new session per write (claim, up to 100 progress writes, then complete or fail). Each write is still its own short `begin()` transaction, so no transaction or connection is held while a handler runs. The `_log_lost_ownership` helper and the duplicate lost-ownership paths collapsed into `_execute`, which returns False on lost ownership.
  - *Efficiency fix:* `Wakeup` drained only one hint per wake-up. After a burst of N submissions, the idle worker woke N times on stale hints and ran N empty claim queries. It now drains all buffered hints after the first one. A mutation check confirmed the new test fails without the drain loop.
  - *Correctness fix:* a database error inside a handler (e.g. a batch progress write) was caught by `except Exception` and recorded as a terminal FAILED. It now propagates to the worker loop, which logs it and backs off, and the job is left for Phase 4 lease recovery.
  - *Robustness:* `restart: unless-stopped` on `api` and `worker` (confirmed with `docker inspect`; `migrate` stays `no`), so an unexpected crash doesn't leave the worker down. A manual `docker compose stop` is still respected.
  - *Tests:* the suite (same command) gives **115 passed, 0 failed, 0 skipped**; `test_worker.py` now has 24 tests. Coverage is 97% (512 statements, 14 missed; `worker.py` 88%, `jobs.py` 99%, `handlers.py` 100%). Repeating the 11 race, outage and wake-up tests 30 times (`-k "concurrent or race or redis_is_down or outage or drains or wakeup"`) gave 30 passed, 0 failed.
  - *Compose smoke rerun* (clean volume, same script): every scenario matched the first run. 100-item batch progress went 14 → 29 → 44 → 59 → 74 → 89 → 100. SIGTERM drained a batch from progress 7 to COMPLETED in 7s, with exit code 0.
  - *Reviewed and kept:* one progress write per batch item (each changes the percentage, at most 100 short transactions); handlers as plain functions; claim via `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED)`, which uses `ix_jobs_pending_priority`.
- **Revision commit:** `4bd8c97`.
- **Phase 1 code improvements (2026-10-21, human-requested; applied on this branch because Phase 1 is already merged):** I reviewed the Phase 1 code (routes, schemas, notifier, db, the create/idempotency path, the migration) and found no correctness bugs in the core paths. The human chose two of the proposed improvements:
  - *`scheduled_at` upper limit:* more than `SCHEDULE_MAX_AHEAD_DAYS` (default 365, inclusive) in the future → 422. `ScheduleTooOldError` was renamed `InvalidScheduleError`, since it now covers both bounds. TECHNICAL_SPEC §3, README, `.env.example` and Compose were updated. A worker test that used year 2999 now uses now + 1 hour.
  - *Listing index:* new Alembic revision `0002` adds `ix_jobs_created (created_at DESC, id DESC)` to match `GET /jobs` ordering; 0001 is unchanged. TECHNICAL_SPEC §4 was updated. `EXPLAIN ... ORDER BY created_at DESC, id DESC LIMIT 50` shows `Index Scan using ix_jobs_created`. Sequential scans had to be disabled to show it, because the test table is tiny.
  - *Not chosen:* 503 for DB outages, pinning the DB session timezone to UTC, a payload size limit.
  - *Tests:* `docker compose --profile test run --rm --build tests pytest -q --cov=app --cov-report=term-missing` → **120 passed, 0 failed, 0 skipped**; 97% coverage (520 statements, 14 missed). New tests:
    - Exactly 365 days ahead → SCHEDULED; 365 days + 1µs → rejected.
    - Config rejects max-ahead 0.
    - API: 2 years ahead → 422 with no row created.
    - `ix_jobs_created` exists. `conftest` runs `downgrade base` → `upgrade head`, so 0002 is exercised both ways.
  - *Migration on a database with data:* on a fresh Compose stack, migrate ran 0001 → 0002. With 3 jobs present, `docker compose run --rm migrate alembic downgrade 0001` ran "0002 -> 0001" and `alembic upgrade head` ran "0001 -> 0002". Afterwards `alembic_version` = 0002, 3 rows were intact and the index existed.
  - *API checks:* 400 days ahead → 422 "scheduled_at is more than 365 days in the future"; 300 days ahead → 201 SCHEDULED; `GET /jobs` → 200.
- **Phase 1 improvements commit:** `ca9887a`. Evidence moved from PLAN.md to EVIDENCE.md in `27eda14`.
- **Approval:** human approval for Phase 2 granted on 2026-10-21; status set to COMPLETED. Phase 3 not started.

## Phase 3 — Retry, Priority and Scheduling

_Recorded 2026-10-21._

- **Branch:** `phase_3_implementation`, created by the human from `main` after PR #2 was merged (base `a6283cb`).
- **Decisions approved by the human before execution:**
  - The worker loop releases due SCHEDULED jobs with one conditional UPDATE before each claim; there is no separate scheduler service.
  - `last_error` is kept while a job waits for an automatic retry and cleared on COMPLETED.
  - From the spec: 3 attempts per cycle with 30s/120s delays (`RETRY_DELAYS_SECONDS`, configurable), then FAILED. Manual retry is allowed only from FAILED and resets attempts.
- **Implemented:**
  - `after_failed_attempt(retry_delays)` in `app/jobs.py` is the single retry rule: SQL `CASE` on `attempts`/`max_attempts`, delay looked up from the attempt number. It is designed so Phase 4 lease recovery can reuse it.
  - `record_failure` is one token-fenced UPDATE that returns the outcome and logs `retry_scheduled` (warning) or `failed` (error). `_fenced_update` now returns the updated row.
  - `release_due_jobs` updates SCHEDULED→PENDING with `id IN (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 100)`, logs `released` per job, and is called by `run_once` before each claim.
  - `cancel_job` and the new `retry_job` share `_transition`, a conditional UPDATE followed by 404/409 lookup, replacing the separate cancel implementation.
  - `POST /jobs/{id}/retry` returns 200, 404 or 409 and sends a Redis wake-up after commit. `complete_job` now clears `last_error`.
  - Docs: README (retry, scheduling, endpoint, env var), TECHNICAL_SPEC §5 (`last_error` rule), `.env.example`, Compose.
  - Tests: shared worker helpers moved from `test_worker.py` to `tests/helpers.py`.
- **Test suite:** `docker compose --profile test run --rm --build tests pytest -q --cov=app --cov-report=term-missing` → **146 passed, 0 failed, 0 skipped**.
  - Per file: `test_api.py` 54, `test_handlers.py` 14, `test_idempotency.py` 7, `test_retry.py` 13, `test_schedule.py` 23, `test_scheduling.py` 10, `test_worker.py` 25.
  - Phase 2 tests that expected a terminal FAILED after the first failure were updated to the new retry rule. The Phase 1 "retry route absent" test was replaced by the EC-02 retry tests. `pyflakes app tests` reports nothing.
- **Edge cases verified:**
  - **EC-07 priority:**
    - Priorities 9 > 5 > 1 are claimed in that order, with FIFO among equal priority.
    - A running low-priority job keeps its claim when a priority-10 job arrives.
    - A high-priority job waiting for its retry delay does not block an eligible low-priority job.
  - **EC-08 scheduling:**
    - A job 1 hour ahead is not released, claimed or run.
    - `scheduled_at = now()` is released (inclusive boundary) and then claimed. Logs: created → released → claimed.
    - The worker releases and runs a due job; a cancelled scheduled job is never released.
    - The release batch limit holds (5 due, limit 2 → 2, 2, 1, 0).
    - 10 concurrent releasers on 20 due jobs → exactly 20 releases and 20 `released` logs.
  - **EC-09 retry:**
    - Attempt 1 fails → PENDING with `last_error`, and `next_run_at − now()` within [28s, 30s].
    - An immediate claim finds nothing (not early).
    - Attempt 2 → delay within [118s, 120s]. Attempt 3 → FAILED with `next_run_at` unchanged, and it is never retried automatically.
    - Log sequence: created, claimed, retry_scheduled, claimed, retry_scheduled, claimed, failed.
    - Success on attempt 2 → COMPLETED with `last_error` NULL. Configured delays (5, 7) are honored. A job waiting for a retry can be cancelled.
    - Delays are checked against DB time and skipped with `fast_forward`; no test sleeps for production delays.
  - **EC-10 manual retry:**
    - FAILED → 200 PENDING with attempts 0 and `last_error`, `result`, `progress`, `started_at`, `completed_at` reset. The earlier log history is kept, plus `manual_retry`.
    - The next run is attempt 1 and completes; a failing retry restarts the 30s backoff.
    - Two concurrent manual retries → exactly one succeeds (one `manual_retry` log).
  - **EC-02 remainder:** retry on PENDING, SCHEDULED, PROCESSING, COMPLETED or CANCELLED → 409 with the status unchanged; unknown id → 404.
  - **Fencing:** a worker that loses ownership during a failing attempt records nothing (status stays PROCESSING, no `last_error`).
- **Flakiness check:** 30 repeated runs of the 13 race, outage and wake-up tests (`-k "concurrent or race or redis_is_down or outage or drains or wakeup"`) → 30 passed, 0 failed.
- **Coverage (informational):** 98% total line coverage (562 statements, 14 missed). `jobs.py` 99% (only the Phase 1 bounded-retry `RuntimeError`); `worker.py` 88% (`main()` signal wiring and the broken-pub/sub close path).
- **Query plans** (`EXPLAIN` with sequential scans disabled, because the test table is tiny): the release subquery uses `Index Scan using ix_jobs_scheduled_at` under `LockRows`. The claim query is unchanged and uses `ix_jobs_pending_priority`.
- **Compose smoke** (`docker compose down -v && docker compose up -d --build --wait`, then `scratchpad/smoke3.sh`):
  1. A one-off worker with `WEBHOOK_FAILURE_RATE=1 RETRY_DELAYS_SECONDS=2,4` ran the webhook through PENDING 0 → PROCESSING 1 → PENDING 1 → PROCESSING 2 → PENDING 2 → PROCESSING 3 → FAILED 3. In the job log, `retry_scheduled` → next `claimed` took 2.02s, then 4.02s.
  2. Manual retry → 200 PENDING, attempts 0, `last_error` null. A healthy worker then COMPLETED it at attempt 1 with `last_error` null. Retrying the COMPLETED job → 409 "only FAILED jobs can be retried; job is COMPLETED".
  3. A job scheduled 8s ahead was SCHEDULED at t+4s, then COMPLETED. `started_at` was 1.76s after `scheduled_at`: not early, and within the 2s poll interval.
  4. With no worker running, low (priority 1) was submitted before high (9). After the worker started, high started at 19:25:15.606 and low at 19:25:16.867.
- **Simplicity/efficiency review:**
  - No new dependencies or services.
  - One retry rule (`after_failed_attempt`) shared by the worker and, later, recovery. One `_transition` helper for cancel and manual retry instead of two copies.
  - The release is bounded (100 rows per call), indexed and SKIP LOCKED, so concurrent workers never block each other. It costs one indexed UPDATE per claim attempt.
  - No new busy loops or sleeps; tests skip delays with `fast_forward` instead of sleeping.
- **Known limitations:**
  - A due scheduled or retrying job can start up to one poll interval (≤ 2s) late; it is never early.
  - Continuous high-priority arrivals can starve low-priority jobs (documented; no aging).
  - Lease expiry and crash recovery remain Phase 4.
  - The starlette TestClient `DeprecationWarning` is unchanged.
- **Deviations:** none from the approved plan. The retry-endpoint test placeholder from Phase 1 was replaced as planned.
- **Commit:** Phase 3 checkpoint `e7b4f4b`; rollback point `a6283cb`.
- **Approval:** human approval for Phase 3 granted on 2026-10-21; status set to COMPLETED. Phase 4 not started.

## Phase 4 — Crash Recovery and Reliability

_Not run. Commands/results/coverage/commit: pending._

## Phase 5 — Full Integration, Edge Cases and Hardening

_Not run. Commands/results/coverage/commit: pending._

## Phase 6 — Documentation, Regression and Submission

_Not run. Commands/results/coverage/commit: pending._
