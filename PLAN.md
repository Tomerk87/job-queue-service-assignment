# Job Queue Service — Implementation Plan v1.1 (Six Phases)

**Status:** IMPLEMENTATION — Phases 1–4 completed; Phase 5 not started (awaiting execution instruction). **Source of truth:** `TECHNICAL_SPEC.md`. **Time box:** 48 elapsed hours from assignment receipt. **Coding agent:** Claude Code. **Current phase:** 4.

## Rules for tracking and verification
- Each phase has `NOT STARTED | IN PROGRESS | BLOCKED | COMPLETED`, implementation checklist, tests/edge cases, quality review, and Definition of Done (DoD).
- Claude Code updates `[ ]` to `[x]` **only after the item is actually implemented and verified**. For tests, verification means **executed and passed**, not merely authored. If a test fails or is skipped, leave unchecked and record why.
- In `EVIDENCE.md`, under the phase's section, record the exact commands, results (passed/failed/skipped), coverage numbers if measured, known defects, and commit hash. Do not invent evidence.
- Run tests in the same phase as the feature; phase 5 performs system-wide integration and edge-case hardening; phase 6 reruns everything in a clean environment.
- Every phase must pass a **Code Quality Gate**: clear responsibilities, no unnecessary dependencies/abstractions, short DB transactions, efficient bounded polling and indexed queries, deterministic tests, explainable code, no sensitive logs.
- Final coverage goals: >=85% measured application line coverage and aim >=90% in critical modules. **Not a per-phase percentage requirement**. Coverage never replaces meaningful concurrency tests. See the technical spec for measurement and exclusions.
- Stop after the current phase and request human approval before continuing. Never silently change architecture or API contracts.

## Phase 1 — Foundation and API
**Status:** COMPLETED

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

**Execution Evidence:** see [EVIDENCE.md](EVIDENCE.md#phase-1--foundation-and-api).

## Phase 2 — Worker and Mock Execution
**Status:** COMPLETED

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
- [x] Human approves moving to Phase 3.

**Execution Evidence:** see [EVIDENCE.md](EVIDENCE.md#phase-2--worker-and-mock-execution).

## Phase 3 — Retry, Priority and Scheduling
**Status:** COMPLETED

### Implementation
- [x] Release due SCHEDULED jobs atomically; no early execution.
- [x] Order eligible claims by priority DESC and created_at ASC.
- [x] Implement exactly three started attempts and 30s/120s production retry delays (test-configurable).
- [x] Persist failure/error and terminal FAILED; implement manual retry reset from FAILED only.
- [x] Keep shared retry logic usable by future crash recovery.

### Tests & verification
- [x] High priority wins among eligible jobs; equal-priority tie break (EC-07).
- [x] Scheduled job never runs early and runs when due (EC-08).
- [x] Deterministic injected failures validate 30s/120s and terminal failure after attempt 3 (EC-09).
- [x] Manual retry resets attempts, clears transient result/error, preserves logs (EC-10).
- [x] Invalid retry states return 409 (EC-02 remainder).

### Code Quality Gate
- [x] Retry logic is centralized, easy to reason about, and does not use production-duration sleeps in tests.
- [x] Eligibility query and indexes align; no unnecessary scheduler service.

### Definition of Done
- [x] All new transition and retry tests pass with evidence.
- [x] No critical open defect; update evidence and Git checkpoint.
- [x] Human approves moving to Phase 4.

**Execution Evidence:** see [EVIDENCE.md](EVIDENCE.md#phase-3--retry-priority-and-scheduling).

## Phase 4 — Crash Recovery and Reliability
**Status:** COMPLETED

### Implementation
- [x] Add configurable lease (default 30s), independent heartbeat (5s) and recovery scan (5s).
- [x] Fence heartbeat/progress/completion/failure by claim token and status.
- [x] Recover expired leases with correct retry delays/attempt accounting; prevent recovery-vs-heartbeat races.
- [x] Implement SIGTERM drain, bounded execution/timeout, and forced-kill recovery.
- [x] Add structured JSON logs for worker and transition events without sensitive payloads.

### Tests & verification
- [x] Crash/expired lease requeues or terminally fails according to attempt count (EC-11).
- [x] Stale completion/progress/heartbeat are rejected (EC-12).
- [x] Live heartbeat prevents premature reclaim (EC-13).
- [x] Graceful shutdown and forced stop recovery work (EC-17).
- [x] Hung handler cannot be renewed forever (EC-18).

### Code Quality Gate
- [x] No shared DB session across threads; bounded resource usage; recovery logic remains explicit.
- [x] Concurrency and failure windows documented, no false exactly-once claim.

### Definition of Done
- [x] Real-Postgres recovery and fencing tests pass with evidence.
- [x] No critical open defect; update evidence and Git checkpoint.
- [x] Human approves moving to Phase 5.

**Execution Evidence:** see [EVIDENCE.md](EVIDENCE.md#phase-4--crash-recovery-and-reliability).

## Phase 5 — Full Integration, Edge Cases and Hardening
**Status:** NOT STARTED

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

**Execution Evidence:** see [EVIDENCE.md](EVIDENCE.md#phase-5--full-integration-edge-cases-and-hardening).

## Phase 6 — Documentation, Regression and Submission
**Status:** NOT STARTED

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

**Execution Evidence:** see [EVIDENCE.md](EVIDENCE.md#phase-6--documentation-regression-and-submission).

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
