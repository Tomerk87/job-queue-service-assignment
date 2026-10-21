# Job Queue Service — Implementation Plan v1.1 (Six Phases)

**Status:** IMPLEMENTATION — Phase 1 completed; Phase 2 in progress. **Source of truth:** `TECHNICAL_SPEC.md`. **Time box:** 48 elapsed hours from assignment receipt; estimates are active engineering hours. **Coding agent:** Claude Code. **Current phase:** 2.

## Rules for tracking and verification
- Each phase has `NOT STARTED | IN PROGRESS | BLOCKED | COMPLETED`, implementation checklist, tests/edge cases, quality review, and Definition of Done (DoD).
- Claude Code updates `[ ]` to `[x]` **only after the item is actually implemented and verified**. For tests, verification means **executed and passed**, not merely authored. If a test fails or is skipped, leave unchecked and record why.
- Under each phase's **Execution Evidence**, record the exact commands, results (passed/failed/skipped), coverage numbers if measured, known defects, and commit hash. Do not invent evidence.
- Run tests in the same phase as the feature; phase 5 performs system-wide integration and edge-case hardening; phase 6 reruns everything in a clean environment.
- Every phase must pass a **Code Quality Gate**: clear responsibilities, no unnecessary dependencies/abstractions, short DB transactions, efficient bounded polling and indexed queries, deterministic tests, explainable code, no sensitive logs.
- Final coverage goals: >=85% measured application line coverage and aim >=90% in critical modules. **Not a per-phase percentage requirement**. Coverage never replaces meaningful concurrency tests. See the technical spec for measurement and exclusions.
- Stop after the current phase and request human approval before continuing. Never silently change architecture or API contracts.

## Phase 1 — Foundation and API
**Status:** COMPLETED | **Estimate:** 5–7 active hours

### Implementation
- [x] Establish Python 3.11+ project, minimal dependencies, typed configuration and directory structure.
- [x] Create Dockerfile and Compose services: API, idle worker entrypoint, PostgreSQL, Redis; document environment variables.
- [x] Define SQLAlchemy models and Alembic migrations for jobs, idempotency_keys and job_logs; add constraints/indexes.
- [x] Implement `POST /jobs`, `GET /jobs/{id}`, `GET /jobs`, `POST /jobs/{id}/cancel`, `GET /health` with validation and appropriate status codes.
- [x] Implement atomic active-key idempotency reservation and 24h expiry/reuse policy with old jobs retained.
- [x] Commit job before best-effort Redis notification; API correctness does not depend on Redis.
- [x] Keep `POST /jobs/{id}/retry` contract reserved; if endpoint stubbed, return explicit not-implemented behavior until Phase 3 (never pretend it works).

### Tests & verification
- [x] API create/get/list and validation/404/409 cases pass (EC-01, EC-02).
- [x] `scheduled_at` boundary tests pass: future, now, exactly 30s past (accepted), beyond 30s and far past (422), naive (422), UTC normalization (EC-01).
- [x] Concurrent same-key submissions create one job using real PostgreSQL (EC-03).
- [x] Expired-key reuse retains original job and handles races (EC-04).
- [x] Clean Compose build, health, migration and API smoke run successfully.

### Code Quality Gate
- [x] API, validation, persistence and config responsibilities are clear; no speculative abstraction or redundant dependency.
- [x] Transactions and idempotency conflict paths are understandable, atomic and covered by tests.

### Definition of Done
- [x] All Phase 1 tests and smoke checks pass with recorded commands/results.
- [x] No critical open defect; update evidence and Git checkpoint.
- [x] Human approves moving to Phase 2.

**Execution Evidence (2026-10-21):**
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
**Status:** IN PROGRESS | **Estimate:** 5–7 active hours

### Implementation
- [x] Implement atomic PostgreSQL claim using `FOR UPDATE SKIP LOCKED` and a fresh claim token.
- [x] Implement bounded worker polling, best-effort Redis wake-up, and worker ID.
- [x] Implement four mock handlers (email, webhook, report, batch) with deterministic test injection.
- [x] Persist result/error and token-fenced progress/completion; keep handler execution outside transactions.
- [x] Keep DB sessions isolated per worker/thread.

### Tests & verification
- [x] Submit -> claim -> execute -> COMPLETED persists result via API (E2E happy path).
- [x] Two concurrent workers cannot claim the same job (EC-06, real PostgreSQL).
- [x] Cancellation and claim are atomic, at least baseline race test (EC-05).
- [x] Batch progress and summary are correct (EC-16 baseline).
- [x] Lost Redis wake-up does not strand committed jobs (EC-14 baseline).

### Code Quality Gate
- [x] Handler boundaries are explicit; no long transaction or busy loop; no duplicated persistence logic.
- [x] Code is readable and interview-explainable; worker and handler tests deterministic.

### Definition of Done
- [x] Worker happy path and concurrent claim integration pass with evidence.
- [x] No critical open defect; update evidence and Git checkpoint.
- [ ] Human approves moving to Phase 3.

**Execution Evidence (2026-10-21):**
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
- **Revision commit:** the commit that adds this entry (`git log --grep '^Phase 2 revision:'`). The phase stays IN PROGRESS until the human approves.

## Phase 3 — Retry, Priority and Scheduling
**Status:** NOT STARTED | **Estimate:** 5–7 active hours

### Implementation
- [ ] Release due SCHEDULED jobs atomically; no early execution.
- [ ] Order eligible claims by priority DESC and created_at ASC.
- [ ] Implement exactly three started attempts and 30s/120s production retry delays (test-configurable).
- [ ] Persist failure/error and terminal FAILED; implement manual retry reset from FAILED only.
- [ ] Keep shared retry logic usable by future crash recovery.

### Tests & verification
- [ ] High priority wins among eligible jobs; equal-priority tie break (EC-07).
- [ ] Scheduled job never runs early and runs when due (EC-08).
- [ ] Deterministic injected failures validate 30s/120s and terminal failure after attempt 3 (EC-09).
- [ ] Manual retry resets attempts, clears transient result/error, preserves logs (EC-10).
- [ ] Invalid retry states return 409 (EC-02 remainder).

### Code Quality Gate
- [ ] Retry logic is centralized, easy to reason about, and does not use production-duration sleeps in tests.
- [ ] Eligibility query and indexes align; no unnecessary scheduler service.

### Definition of Done
- [ ] All new transition and retry tests pass with evidence.
- [ ] No critical open defect; update evidence and Git checkpoint.
- [ ] Human approves moving to Phase 4.

**Execution Evidence:** _Not run. Commands/results/coverage/commit: pending._

## Phase 4 — Crash Recovery and Reliability
**Status:** NOT STARTED | **Estimate:** 6–8 active hours

### Implementation
- [ ] Add configurable lease (default 30s), independent heartbeat (5s) and recovery scan (5s).
- [ ] Fence heartbeat/progress/completion/failure by claim token and status.
- [ ] Recover expired leases with correct retry delays/attempt accounting; prevent recovery-vs-heartbeat races.
- [ ] Implement SIGTERM drain, bounded execution/timeout, and forced-kill recovery.
- [ ] Add structured JSON logs for worker and transition events without sensitive payloads.

### Tests & verification
- [ ] Crash/expired lease requeues or terminally fails according to attempt count (EC-11).
- [ ] Stale completion/progress/heartbeat are rejected (EC-12).
- [ ] Live heartbeat prevents premature reclaim (EC-13).
- [ ] Graceful shutdown and forced stop recovery work (EC-17).
- [ ] Hung handler cannot be renewed forever (EC-18).

### Code Quality Gate
- [ ] No shared DB session across threads; bounded resource usage; recovery logic remains explicit.
- [ ] Concurrency and failure windows documented, no false exactly-once claim.

### Definition of Done
- [ ] Real-Postgres recovery and fencing tests pass with evidence.
- [ ] No critical open defect; update evidence and Git checkpoint.
- [ ] Human approves moving to Phase 5.

**Execution Evidence:** _Not run. Commands/results/coverage/commit: pending._

## Phase 5 — Full Integration, Edge Cases and Hardening
**Status:** NOT STARTED | **Estimate:** 5–7 active hours

### Implementation
- [ ] Review all EC-01..EC-19 and fill actual test gaps; do not duplicate existing tests without reason.
- [ ] Verify multi-worker Compose scaling (3 replicas) and Redis outage/fallback behavior.
- [ ] Verify concurrent cancellation/claim, idempotency races/expiry, process crash/restart, batch progress and DB-failure handling.
- [ ] Fix demonstrated defects and simplify only clearly unnecessary complexity.
- [ ] Configure `pytest-cov` and document exclusions/commands; capture measured line coverage and critical-module breakdown.

### Tests & verification
- [ ] All six employer-required test categories pass (submission, completion, failure/retry, cancellation, idempotency, priority).
- [ ] PostgreSQL-backed concurrency, lease, fencing and race tests pass (EC-03..EC-13 as relevant).
- [ ] Redis outage and lost notification recovery pass (EC-14).
- [ ] PostgreSQL failure before commit never produces false success (EC-15).
- [ ] Batch progress, shutdown, timeout and three-worker E2E pass (EC-16..EC-19).
- [ ] Full suite passes and actual coverage report is captured; target >=85% overall and aim >=90% critical modules, or document shortfall with rationale.

### Code Quality Gate
- [ ] Review every dependency, module boundary, transaction, query/index, polling strategy, exception and log.
- [ ] No needless framework, unused code, test bypass, flaky sleeps or speculative refactor.

### Definition of Done
- [ ] Full integration suite green; no unresolved critical consistency defect.
- [ ] Evidence includes test counts, coverage, failures fixed and Git checkpoint.
- [ ] Human approves moving to Phase 6.

**Execution Evidence:** _Not run. Commands/results/coverage/commit: pending._

## Phase 6 — Documentation, Regression and Submission
**Status:** NOT STARTED | **Estimate:** 4–6 active hours

### Implementation
- [ ] Write accurate README.md: architecture, setup, migrations, API examples, tests and demo scenarios.
- [ ] Write DECISIONS.md: atomic claim, crash windows, priority/fairness, retry, idempotency, at-least-once and alternatives.
- [ ] Write AI_USAGE.md based on **actual** tool use, help, mistakes and corrections; invent nothing.
- [ ] Verify Dockerfile, Compose, environment examples, dependency lock/pinning and repository cleanliness.
- [ ] Perform final code simplicity/efficiency/readability review and remove obsolete scaffolding.

### Tests & verification
- [ ] From a fresh checkout/environment, `docker compose up --build` and migrations work (EC-20).
- [ ] Full automated suite passes; record exact pass/fail/skip counts.
- [ ] Final coverage report captured and compared to goals; disclose any shortfall.
- [ ] Demonstrate submit -> status -> completion, failure/retry, cancel, schedule, multi-worker and recovery.
- [ ] Required deliverables exist; all docs match actual behavior; no uncommitted changes.

### Code Quality Gate
- [ ] Every important component, lock and failure path can be explained in an interview.
- [ ] No unsupported performance, delivery or exactly-once guarantees in documentation.

### Definition of Done
- [ ] All mandatory employer requirements are satisfied and evidenced.
- [ ] No critical open defects; final Git commit and submission reference recorded.
- [ ] Human approves submission.

**Execution Evidence:** _Not run. Commands/results/coverage/commit: pending._

## Edge-case tracking index
See `TECHNICAL_SPEC.md` section 14 for the full EC-01..EC-20 matrix and expected test types. Each EC ID must be mapped to a test or a clearly documented manual verification; unchecked items remain open. In particular, PostgreSQL concurrency tests must not be replaced with SQLite or mocks.

## Time-budget priority
**P0:** Compose, API, persistence, separate worker, four mock handlers, atomic claim, retry, priority, cancellation, idempotency, >=6 meaningful tests, required docs. **P1:** scheduled jobs, crash recovery, structured logs, graceful shutdown, health. **P2:** optional polish. Do not cut already-integrated correctness protections. Test coverage goals do not justify omitting P0 requirements.

## Per-phase report format (required)
- Phase and status:
- Files changed and why:
- Implemented checklist items:
- Test commands actually executed:
- Results: passed / failed / skipped; integration environment:
- Coverage measured (if run), including critical modules:
- Edge cases verified (EC IDs):
- Simplicity/efficiency review findings:
- Open defects, deviations, risk and follow-up:
- Commit hash / rollback point:
- Human approval for next phase: pending / granted:
