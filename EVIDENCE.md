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

_Recorded 2026-10-21._

- **Branch:** `phase_4_implementation`, created by the human from `main` after PR #3 was merged (base `1c1b5fb`).
- **Decisions approved by the human before execution:**
  - **Timeout:** a cooperative `JOB_TIMEOUT_SECONDS` (60s) whose expiry is a normal failed attempt. As a backstop, the heartbeat stops renewing at the deadline, and if the handler is still running at deadline + lease the worker process exits (`os._exit(1)`) and Docker restarts it.
  - **Recovery** runs in every worker's loop every `RECOVERY_INTERVAL_SECONDS`.
  - **Lease is authority:** every fenced write also requires `lease_expires_at > now()`.
  - **JSON logs** via `python-json-logger==3.3.0`.
- **Implemented:**
  - `app/jobs.py`:
    - `_fenced_update` also requires an unexpired lease.
    - `renew_lease` is a fenced lease extension with no log row.
    - `recover_expired_leases` is a bounded `SKIP LOCKED` UPDATE that applies the Phase 3 `after_failed_attempt` rule, sets `last_error` to "lease expired (worker …)", and logs `lease_expired` plus `retry_scheduled` or `failed`.
  - `app/worker.py`:
    - A `Heartbeat` thread per job renews on its own sessions, marks the job lost on a rejected renewal or after a full lease without a successful renewal, stops renewing at the deadline, and exits the process on a hang.
    - A guarded `ctx.sleep` raises `LostOwnership` or `JobTimeout`.
    - `run()` calls `recover_once` on a monotonic timer.
    - All log calls carry `event`/`job_id`/`worker_id` in `extra`.
  - `app/logging_config.py` (new): a JSON formatter on the root logger. uvicorn's loggers are routed through it, and uvicorn's ANSI `color_message` field is excluded.
  - `app/config.py`: heartbeat, recovery and timeout settings (validated); `LEASE_SECONDS` is now a float.
  - Docs: README reliability section and env vars; TECHNICAL_SPEC §7 (lease-authority rule) and §9 (hang backstop); `.env.example`; Compose.
- **Test suite:** `docker compose --profile test run --rm --build tests pytest -q --cov=app --cov-report=term-missing` → **169 passed, 0 failed, 0 skipped** (about 31s).
  - Per file: `test_api.py` 54, `test_handlers.py` 14, `test_idempotency.py` 7, `test_process.py` 4, `test_recovery.py` 13, `test_retry.py` 13, `test_schedule.py` 27, `test_scheduling.py` 10, `test_worker.py` 27.
  - `pyflakes app tests` reports nothing (it found one unused variable in `test_recovery.py`, which was fixed by asserting the claimed job id).
- **Edge cases verified:**
  - **EC-11:**
    - An expired lease after attempt 1 → PENDING, attempts 1, `next_run_at − now()` within [28s, 30s], `last_error` "lease expired (worker test-worker)", token and lease cleared. Logs: created, claimed, lease_expired, retry_scheduled.
    - Expiry on attempt 3 → FAILED.
    - A live lease is never recovered.
    - 10 concurrent recoverers on 20 expired jobs → exactly 20 recoveries.
    - The batch limit holds (5 expired, limit 2 → 2, 2, 1, 0).
  - **EC-12:**
    - After recovery and a re-claim by another worker, the stale token's progress, renew, complete and fail are all rejected, while the new owner completes.
    - A lapsed lease rejects complete, progress and renew even before recovery runs, and recovery then succeeds.
  - **EC-13:**
    - `renew_lease` extends a 1s lease to more than 28s.
    - A real `Heartbeat` thread (lease 1s, interval 0.2s) kept a job safe from recovery attempted every 0.1s for 3s (3 leases), and the job then completed.
    - After the deadline, renewals stop (the lease timestamp stops moving).
    - A rejected renewal marks the job lost.
    - With the DB unreachable, the job is marked lost only after a full lease without renewal.
    - Renewal racing recovery on an expired lease (10 rounds): recovery wins, renewal is rejected.
  - **EC-17** (real `python -m app.worker` subprocesses: lease 2s, heartbeat 0.3s, recovery 0.3s):
    - SIGTERM mid-batch → COMPLETED 100 at attempt 1, exit 0, log order stopping < completed < stopped.
    - SIGKILL mid-batch → the job stays PROCESSING. A second worker recovers it and completes it at attempt 2. Logs: created, claimed, lease_expired, retry_scheduled, claimed, completed.
  - **EC-18:**
    - *Cooperative:* a batch with a 0.25s timeout stops at the deadline (in less than 1s) → PENDING with `last_error` "JobTimeout: job exceeded 0.25s timeout".
    - *Non-cooperative:* `tests/hung_worker.py` replaces the email handler with `time.sleep(1000)` and sets `JOB_TIMEOUT_SECONDS=1`. The process exits with code 1 in under 10s, logging a CRITICAL `handler_hung` line with the job_id, and the job is then recovered to PENDING with "lease expired (worker proc-hung)".
    - A heartbeat that detects a lost lease stops the handler, and nothing is written.
  - **Logs:** every worker stdout line parses as JSON with ts/level/logger/message, job lines carry `event` and `worker_id`, and the payload value never appears.
- **Mutation checks** (in throwaway containers; the host code was not changed):
  - With `os._exit(1)` replaced by `pass`, the hang test fails.
  - With the `lease_expires_at > now()` fence removed, 2 recovery tests fail.
- **Flakiness checks:**
  - 30 repeated runs of the 34 race, heartbeat, recovery and timeout tests (`-k "concurrent or race or redis_is_down or outage or drains or wakeup or heartbeat or recover or lapsed or timeout"`) → 30 passed, 0 failed.
  - 10 repeated runs of `tests/test_process.py` → 10 passed, 0 failed.
- **Coverage (informational):** 99% total line coverage (672 statements, 6 missed). `jobs.py` 99%, `worker.py` 97%, `logging_config.py` 100%. Uncovered: the hang-exit lines, which run only inside the subprocess test and so aren't measured in-process; the broken-pub/sub close path; and the Phase 1 bounded-retry `RuntimeError`.
- **Query plan:** the recovery subquery uses `Index Scan using ix_jobs_processing_lease` under `LockRows` (sequential scans disabled, because the test table is tiny).
- **Compose smoke** (`docker compose down -v && docker compose up -d --build --wait`, then `scratchpad/smoke4.sh`; production defaults: lease 30s, heartbeat 5s, recovery 5s, retry 30s):
  1. Worker and API logs are one JSON object per line. uvicorn's `color_message` was present on the first run, then excluded and rechecked: 0 lines.
  2. `docker compose kill -s SIGKILL worker` mid-batch: the worker exited with 137, the job stayed PROCESSING, recovery about 30s later logged `leases_recovered` (attempt 1 → `retry_scheduled`), it was reclaimed at about t+66s (30s retry delay) and COMPLETED at attempt 2.
  3. `docker compose stop worker` mid-batch took 9s: the job reached COMPLETED 100 at attempt 1 and the worker exited with 0.
  4. `--scale worker=3` with 30 email jobs: all 30 COMPLETED, at most 1 claim per job, distributed 11/9/10 across the workers.
- **Simplicity/efficiency review:**
  - One new dependency (python-json-logger, approved).
  - The recovery and retry rule is shared (`after_failed_attempt`), with no duplicate transition logic.
  - The heartbeat is one short-lived thread per job using its own sessions (no session sharing). Renewal writes no log rows, so `job_logs` doesn't grow every 5s.
  - Recovery is bounded, indexed and SKIP LOCKED.
  - No busy loops: the heartbeat waits on an Event, recovery runs on a timer, and the idle wait is still bounded.
- **Known limitations:**
  - **At-least-once:** a job can run twice, e.g. a crash after its side effect but before the completion commit, or a paused worker whose lease lapsed. Fencing protects database state, not external effects.
  - A worker busy with a job runs recovery only between jobs (jobs are bounded by the timeout).
  - A non-cooperative hang occupies its worker until deadline + lease (about 90s by default) before it exits.
  - The cooperative timeout fires only at handler wait points (`ctx.sleep`).
  - The starlette TestClient `DeprecationWarning` is unchanged.
- **Deviations:** none from the approved plan. Added beyond the plan: a test for the heartbeat during a DB outage (spec §7), and removal of uvicorn's `color_message` from the JSON logs.
- **Commit:** Phase 4 checkpoint `c658876`; rollback point `1c1b5fb`.
- **Approval:** human approval for Phase 4 granted on 2026-10-21; status set to COMPLETED. Phase 5 not started.

## Phase 5 — Full Integration, Edge Cases and Hardening

_Recorded 2026-10-21._

- **Branch:** `phase_5_implementation`, created by the human from `main` after PR #4 was merged (base `386668d`). **Not committed:** per the human's instruction, changes stay uncommitted until explicitly requested.
- **Decisions approved by the human before execution:**
  - End-to-end tests run with real worker subprocesses, plus a committed manual Compose demo `scripts/smoke.sh`.
  - An unavailable database returns **503** on every endpoint (reversing the Phase 1 choice to keep 500).
  - No CI workflow.
- **Gap review:** EC-01–EC-13 and EC-16–EC-18 were already covered by automated tests. The gaps were EC-15 (no automated test), EC-19 (manual evidence only), EC-14 (in-process only, while the spec asks for end-to-end), and the undocumented coverage configuration and test-category mapping. All are closed below.
- **Implemented:**
  - `app/main.py`: an `OperationalError` exception handler → 503 `{"detail": "database unavailable"}`, logged with `event=db_unavailable`; `/health` keeps its own 503 body.
  - Docs: TECHNICAL_SPEC §3 (503 rule); README (test section, smoke demo, DB-outage note, status line).
  - `pyproject.toml`: coverage `source = ["app"]` (migrations and tests excluded by location) and `exclude_also = ['if __name__ == .__main__.:']` (entrypoint boilerplate only). Both are documented in comments.
  - New `scripts/smoke.sh`: 10 steps and 30 PASS/FAIL checks, with a non-zero exit on failure.
- **New tests:**
  - **EC-15** (`tests/test_api.py`):
    - DB unreachable → `POST /jobs` returns 503 with no row and no Redis notification.
    - GET, list, cancel and retry also return 503.
    - The connection dropping at COMMIT (`before_commit` raises `OperationalError`) → 503 with 0 jobs, keys and logs and no notification; a retry with the same idempotency key afterwards → 201.
  - **EC-19** (`tests/test_process.py`): 3 real worker processes and 30 batch jobs → all COMPLETED at attempt 1, exactly 1 `claimed` log per job, all workers exit 0, and at least 2 workers used.
  - **EC-14** (`tests/test_process.py`): API notifier and worker both pointed at an unreachable Redis → the job COMPLETES by polling, and the worker logs `redis_down` but never `redis_up`.
- **Test suite:** `docker compose --profile test run --rm --build tests pytest -v --cov=app --cov-report=term-missing` → **177 passed, 0 failed, 0 skipped** (38.7s).
  - Per file: `test_api.py` 60, `test_handlers.py` 14, `test_idempotency.py` 7, `test_process.py` 6, `test_recovery.py` 13, `test_retry.py` 13, `test_schedule.py` 27, `test_scheduling.py` 10, `test_worker.py` 27.
  - `pyflakes app tests`: exit 0.
- **Flakiness checks:**
  - 30 repeated runs of the 38 in-process race, heartbeat, recovery, timeout and failure tests → 30 passed, 0 failed.
  - 10 repeated runs of `tests/test_process.py` (6 tests) → 10 passed, 0 failed. The EC-19 distribution was `{proc-1: 10, proc-2: 10, proc-3: 10}` in all 10 runs.
- **Coverage (measured with the command above):** **99%** total line coverage (675 statements, 6 missed). The target is ≥ 85%, so it is met.
  - Critical modules, target ≥ 90%:
    - `app/jobs.py` (claim, state transitions, retry rule, idempotency, lease recovery): **99%** (151/152). The miss is line 111, the bounded idempotency-retry `RuntimeError`, which is unreachable unless a key's TTL is shorter than one request.
    - `app/worker.py` (worker loop, heartbeat, recovery timer): **97%** (177/182). The misses are lines 186–191, the hang `os._exit`, which is exercised by the subprocess test `test_hung_handler_exits_worker_and_job_is_recovered` and not measured in-process; and lines 260–261, closing an already-broken pub/sub connection.
  - All other modules: 100%.
  - Exclusions: only `if __name__ == "__main__":` blocks. Migrations and tests are outside `source`.
- **Assignment's six required test categories:**

  | Category | Tests |
  |---|---|
  | Job submission and retrieval | `test_api.py::test_submit_and_get_each_job_type` (4 types), `test_list_is_newest_first_and_paginated`, `test_list_filters_by_status_and_type` |
  | Job completion flow | `test_worker.py::test_submitted_job_is_claimed_executed_and_completed` (4 types), `test_result_shapes_per_type`; `test_process.py::test_sigterm_finishes_running_job_then_exits_cleanly` |
  | Job failure and retry | `test_retry.py::test_three_attempts_with_30s_and_120s_backoff_then_failed`, `test_success_on_retry_completes_and_clears_last_error`, `test_manual_retry_resets_attempts_and_keeps_history` |
  | Cancellation | `test_api.py::test_cancel_pending_job`, `test_cancel_scheduled_job`, `test_cancel_in_non_cancellable_state_returns_409`; `test_worker.py::test_cancel_and_claim_race_has_exactly_one_winner` |
  | Idempotency | `test_idempotency.py::test_same_key_returns_original_job`, `test_concurrent_same_key_creates_exactly_one_job`, `test_concurrent_reuse_of_expired_key_creates_exactly_one_new_job` |
  | Priority ordering | `test_scheduling.py::test_higher_priority_is_claimed_first`, `test_equal_priority_is_first_in_first_out`, `test_running_job_is_not_preempted_by_higher_priority` |

- **Edge-case matrix:**

  | EC | Tests |
  |---|---|
  | EC-01 | `test_api.py::test_invalid_submission_returns_422_and_creates_nothing` (19 cases), scheduled_at API tests; `test_schedule.py` boundary and config tests |
  | EC-02 | `test_api.py` get/cancel 404 and cancel 409; `test_retry.py::test_retry_in_non_failed_state_returns_409` (5), `test_retry_unknown_job_returns_404` |
  | EC-03 | `test_idempotency.py::test_concurrent_same_key_creates_exactly_one_job` |
  | EC-04 | `test_idempotency.py::test_expired_key_creates_new_job_and_keeps_old_one`, `test_concurrent_reuse_of_expired_key_creates_exactly_one_new_job` |
  | EC-05 | `test_worker.py::test_cancel_and_claim_race_has_exactly_one_winner` (20 rounds) |
  | EC-06 | `test_worker.py::test_concurrent_workers_cannot_claim_the_same_job`, `test_concurrent_workers_drain_queue_without_duplicates` |
  | EC-07 | `test_scheduling.py` priority tests (4) |
  | EC-08 | `test_scheduling.py` release tests (6), including `test_concurrent_release_releases_each_job_once` |
  | EC-09 | `test_retry.py` automatic-retry tests (4) |
  | EC-10 | `test_retry.py` manual-retry tests (3) |
  | EC-11 | `test_recovery.py` recovery tests (5) |
  | EC-12 | `test_recovery.py::test_stale_worker_cannot_write_after_recovery_and_reclaim`, `test_lapsed_lease_rejects_writes_even_before_recovery`; `test_worker.py` fencing tests |
  | EC-13 | `test_recovery.py` heartbeat tests (6) |
  | EC-14 | `test_worker.py::test_job_submitted_while_redis_is_down_is_processed_by_polling`, wake-up tests; `test_process.py::test_redis_outage_end_to_end_jobs_still_complete_by_polling` |
  | EC-15 | `test_api.py::test_submit_returns_503_when_database_is_unreachable`, `test_other_endpoints_return_503_when_database_is_unreachable` (4), `test_failed_commit_is_never_acknowledged` |
  | EC-16 | `test_handlers.py` batch tests; `test_worker.py::test_batch_progress_is_persisted_monotonically` |
  | EC-17 | `test_process.py::test_sigterm_finishes_running_job_then_exits_cleanly`, `test_sigkill_mid_job_is_recovered_and_completed_by_another_worker` |
  | EC-18 | `test_worker.py::test_job_past_its_timeout_fails_the_attempt_and_is_retried`; `test_process.py::test_hung_handler_exits_worker_and_job_is_recovered` |
  | EC-19 | `test_process.py::test_three_workers_complete_all_jobs_without_duplicate_claims`; `scripts/smoke.sh` step 10 |
  | EC-20 | Phase 6 (fresh-environment run) |

- **Manual Compose demo** (`docker compose down -v && docker compose up -d --build --wait && scripts/smoke.sh`): **ALL CHECKS PASSED** (30/30, exit 0, 2 min 18 s). It covered all job types, idempotency, cancel, failure → retries → FAILED → manual retry → 409 on COMPLETED, priority, scheduling, Redis down (`degraded` and still processed), SIGTERM drain (exit 0), SIGKILL recovery (attempt 2 with `lease_expired` logged), and 3 workers (15 jobs, max 1 claim per job, split 5/5/5).
- **Quality-gate review (whole codebase):**
  - *Dependencies:* runtime packages are fastapi, uvicorn, SQLAlchemy, psycopg, alembic, redis and python-json-logger; dev packages are pytest, pytest-cov and httpx. All are used. Pinning transitive dependencies remains a Phase 6 item.
  - *Module boundaries:* routes (`main`), schemas, persistence and all state transitions (`jobs`), handlers, worker loop (`worker`), notifier, config and logging. There is no duplicated transition logic: cancel and retry share `_transition`, and handler failure and lease expiry share `after_failed_attempt`.
  - *Transactions:* every write is one short `session.begin()` block. No transaction or connection is held while a handler runs, and the heartbeat uses its own sessions.
  - *Queries vs. indexes* (`EXPLAIN` with sequential scans disabled):
    - claim → `ix_jobs_pending_priority`; release → `ix_jobs_scheduled_at`; recovery → `ix_jobs_processing_lease`;
    - unfiltered list → `ix_jobs_created`; status/type lists → `ix_jobs_status_created` / `ix_jobs_type_created` plus an incremental sort for the id tie-break;
    - idempotency lookup → `idempotency_keys_pkey`; health counts → index-only scan on `ix_jobs_status_created`.
  - *Polling:* the idle wait is ≤ 2s or ends early on a Redis hint (stale hints are drained); recovery runs every 5s; the heartbeat waits on an Event. No busy loops.
  - *Exceptions:* DB unavailability → 503 in the API, and backoff in the worker. Handler errors → a failed attempt. Infrastructure errors inside handlers propagate to the loop and are left to lease recovery. Nothing is swallowed silently.
  - *Logs:* JSON with event, job_id and worker_id; payloads are never logged (asserted in `test_worker_logs_are_json_with_job_context_and_no_payload`).
  - *Test timing:* real sleeps exist only where the behavior is about time (heartbeat, timeout and process tests), with short configured intervals. Retry delays are skipped with `fast_forward`, never slept.
  - **Defects found in this pass:** none. No code changes beyond the approved 503 handler.
- **Known limitations (unchanged):**
  - At-least-once execution; no exactly-once side effects.
  - A due or retrying job can start up to the poll interval (≤ 2s) late.
  - A busy worker runs recovery only between jobs.
  - Low-priority jobs can starve under constant high-priority load.
  - `POST /jobs` waits up to `REDIS_TIMEOUT_SECONDS` (0.5s) per request while Redis is down.
  - The starlette TestClient `DeprecationWarning` is unchanged.
- **Deviations:** none from the approved plan.
- **Commit:** Phase 5 checkpoint `821b6ce`, committed on the human's instruction; rollback point `386668d`.
- **Approval:** human approval for Phase 5 granted on 2026-10-22; status set to COMPLETED. Phase 6 not started.

## Phase 6 — Documentation, Regression and Submission

_Not run. Commands/results/coverage/commit: pending._
