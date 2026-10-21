# Job Queue Service — Technical Specification v1.1

Status: **Approved design baseline, revised for testing and quality; implementation not started**. This document describes planned behavior, not implemented functionality.

## 1. Objective and source
Build a Python 3.11+ distributed background job processing service for a backend engineering take-home assessment. The assignment explicitly requires an API, a queue/cache technology, separate worker processes, relational persistence, three-attempt exponential retry, priority, cancellation, Docker Compose, and at least six meaningful tests. Scheduled jobs, crash recovery, structured logs, graceful shutdown and health statistics are also targeted. The four job types are simulated email, webhook, report, and batch.

The assignment PDF is authoritative. This specification adds design decisions and testable contracts; where those decisions exceed the assignment, they are project choices, not employer requirements.

## 2. Architecture and invariants
Services: FastAPI API, Python worker, PostgreSQL, Redis. Docker Compose runs all four. API and worker can share one built image with distinct commands. Scale worker replicas with Compose.

- PostgreSQL is the **source of truth and authoritative logical queue**. Redis is a **best-effort wake-up notification channel**, not the durable job store. Workers periodically poll PostgreSQL even when Redis is healthy.
- Never execute a job inside an HTTP request.
- Never hold a database transaction or row lock during job execution or sleeps.
- All state transitions are conditional atomic updates, checked for affected-row count.
- Atomic claim uses `SELECT ... FOR UPDATE SKIP LOCKED` and an update in the same short transaction.
- Workers may execute a job more than once after failures. Claim tokens fence **database writes**, not external side effects. **At-least-once execution**, not exactly-once side effects.
- PostgreSQL downtime prevents reliable progress; Redis downtime must not lose jobs.

## 3. Job API contracts
- `POST /jobs`: validate type-specific payload, priority integer 0–10 (default 0), max_attempts fixed to 3, optional future scheduled_at (UTC timestamp), optional idempotency_key <=255 chars. New job: HTTP 201 with job representation; duplicate active key: HTTP 200 with original job; invalid: 422. Job persistence commits before best-effort Redis notification.
- `scheduled_at` rules (compared with DB now; timezone-aware input normalized to UTC): missing/null -> PENDING; future -> SCHEDULED; past or now within a configurable tolerance (default 30 seconds, inclusive) -> PENDING; more than the tolerance in the past -> 422; more than a configurable maximum in the future (default 365 days, inclusive) -> 422; unparseable or timezone-naive -> 422.
- `GET /jobs/{id}`: job status, attempts, progress, result/error, timestamps; 404 when absent.
- `GET /jobs?status=&type=&limit=&offset=`: filtered, paginated, newest first; validate bounds.
- `POST /jobs/{id}/cancel`: atomically cancel PENDING/SCHEDULED; already CANCELLED returns 200; PROCESSING/COMPLETED/FAILED returns 409; missing returns 404. Cancellation racing a claim has one winner.
- `POST /jobs/{id}/retry`: only FAILED can retry; atomically reset attempts to 0, error/result/progress as appropriate, next_run_at=now, status=PENDING; 409 otherwise, 404 missing. Retain history in job logs.
- `GET /health`: API/database availability and counts by job state, plus Redis status. Distinguish degraded Redis from failed database. Never report overall healthy if the database is unreachable.

Suggested request payloads (final validation schemas may be narrow and documented): email {to, subject, body?}; webhook {url, body?} **simulation only, never send an HTTP request**; report {report_name}; batch {items: array of 1..100 items}. Mock handlers return JSON results. Never expose internal claim_token in public responses.

## 4. Persistence
### jobs
id UUID PK; type; payload JSONB; status (SCHEDULED/PENDING/PROCESSING/COMPLETED/FAILED/CANCELLED); priority int; attempts int; max_attempts int default 3; scheduled_at nullable timestamptz; next_run_at timestamptz; lease_expires_at nullable timestamptz; claim_token UUID nullable; worker_id nullable; idempotency_key nullable; progress int 0..100; result JSONB nullable; last_error nullable; created_at, updated_at, started_at nullable, completed_at nullable timestamptz.

Indexes: eligibility `(priority DESC, created_at ASC)` filtered on PENDING plus next_run_at as appropriate; expired leases filtered on PROCESSING; filtering by status/type; newest-first listing `(created_at DESC, id DESC)`. Use UTC-aware datetimes and DB time for lease comparisons.

### idempotency_keys
`key` unique PK; `job_id` FK; `expires_at` timestamptz. Job rows persist beyond key expiry. The API atomically reserves a key and creates its job in one DB transaction; on conflict, read the still-active mapping and return the existing job. Expired-key replacement must serialize competing requests (e.g. row locking/atomic upsert with a carefully tested conflict path). No two concurrent submissions with the same active key may create distinct jobs. Retain keys for **at least** 24 hours. Cleanup must not remove unexpired keys. Policy: same active key returns the original job even if the new payload differs; document this explicitly.

### job_logs
id PK, job_id FK, level, event, message, metadata JSONB, created_at. Record claim, retry, recovery, manual retry, completion and terminal failure; avoid sensitive payload logging.

## 5. State machine
- On creation: future scheduled_at -> SCHEDULED; missing, or past/now within the tolerance window (section 3) -> PENDING; older than the tolerance -> rejected with 422.
- Scheduler: SCHEDULED -> PENDING only when scheduled_at <= DB now, via atomic conditional update.
- Claim: eligible PENDING (`next_run_at <= DB now`) -> PROCESSING; increment attempts; assign fresh claim_token, worker_id, started_at, lease_expires_at.
- Successful execution: PROCESSING -> COMPLETED only with matching token; persist result, progress=100, completed_at, clear lease/token.
- Execution error: if attempts < max_attempts -> PENDING with next_run_at = DB now + retry delay; else FAILED; clear lease/token.
- Expired lease: recovery atomically applies same retry/terminal logic based on attempts and invalidates prior token. Recovery must not race with a valid heartbeat or completion: use conditional row update and verify row count.
- PENDING/SCHEDULED -> CANCELLED only via conditional update.
- FAILED -> PENDING via manual retry, attempts reset to 0.

## 6. Retry timing
Max 3 started attempts per cycle. Attempt 1 immediately; on failure or lease expiry after attempt 1, delay 30 seconds before attempt 2; after attempt 2, delay 120 seconds before attempt 3; after attempt 3, terminal FAILED. Manual retry opens a new cycle by resetting attempts. Delays should be configurable for fast automated tests without changing production defaults. No automatic retry of terminal FAILED jobs.

## 7. Worker algorithm and concurrency
Claim in a short PostgreSQL transaction using `FOR UPDATE SKIP LOCKED`, order by priority DESC, created_at ASC, with eligibility filters. Only one valid claim token per job. Run handler after commit. Finish/heartbeat/progress operations require `(id, status=PROCESSING, claim_token=current_token)` and must not change state if no rows match.

Default lease=30s, heartbeat every 5s, recovery scan every 5s, idle DB polling <=2s. Values configurable. Heartbeat operates independently of blocking handler work (e.g. dedicated thread or async-safe execution strategy), with its own DB session. Handler must never reuse a session across threads. If heartbeat fails repeatedly or token is lost, do not write completion; log lost ownership. This cannot forcibly prevent already-started external side effects.

Graceful shutdown: stop claiming on SIGTERM, keep heartbeat for active job, finish current job if possible, then exit. Docker stop grace period must exceed the longest mock job plus overhead; forced kill relies on lease recovery.

Redis notification is only a hint; use polling as the correctness fallback. No reliance on pub/sub delivery guarantees. Priority ordering applies to **eligible jobs at claim time**, not preemption of jobs already processing. Continuous high-priority arrivals can starve lower-priority jobs (documented limitation).

## 8. Mock job handlers
- Email: sleep 1–3s; return mock message ID; no real email.
- Webhook: sleep 1–2s; approximately 80% success, 20% simulated failure; no real HTTP call. Tests inject deterministic success/failure behavior.
- Report: sleep 3–5s; return mock report URL; no real file needed.
- Batch: process each item with short delay; report progress via token-fenced updates; return summary. Re-execution after a crash may reprocess items; mock processing has no external side effects.

## 9. Reliability scenarios and guarantees
- Concurrent workers: only one valid DB claim at a time.
- API crash after commit before Redis notification: periodic DB polling discovers the job.
- Redis unavailable: DB polling continues; API may report degraded status.
- Worker crash before completion: expired lease recovered and job retried or terminally failed.
- Worker pauses past lease and resumes: stale token cannot overwrite DB state; external work could overlap.
- Crash after external side effect before completion commit: side effect may repeat. Exactly-once external execution is **not guaranteed**; downstream idempotency is needed in real integrations.
- Concurrent cancel and claim: atomic transitions ensure only one succeeds.
- PostgreSQL outage: API submission cannot be acknowledged as successful unless committed; workers wait/retry connection.
- A permanently stuck handler may outlive its lease if heartbeat continues: enforce handler timeout or ensure heartbeat stops on timeout/loss of liveness. **Explicit acceptance requirement:** bounded handler execution or an effective timeout; do not allow indefinite heartbeat for a hung handler. This is a practical refinement of the earlier design.

## 10. Acceptance criteria
1. `docker compose up --build` starts API, worker, PostgreSQL and Redis with documented setup/migrations.
2. Submit/get/list/cancel/retry/health endpoints work with specified status codes.
3. All four mock job types run asynchronously and store expected JSON result/error.
4. Priority higher-first among simultaneously eligible pending jobs; no preemption promised.
5. Scheduled jobs never start early; retries respect 30s and 120s production defaults.
6. Max 3 attempts per automatic cycle; manual retry resets attempts.
7. Same active idempotency key under concurrent requests creates exactly one job; retained >=24h.
8. Concurrent workers cannot both hold valid claim for one job.
9. Expired worker lease recovers; stale token cannot overwrite new ownership.
10. Redis notification loss does not strand jobs.
11. Pending/scheduled cancellation racing claim behaves atomically.
12. Graceful stop finishes bounded active job; forced stop eventually recovers.
13. Structured logs carry job_id, worker_id, event; do not leak sensitive data.
14. Automated tests include the six assignment categories and real-Postgres concurrency/recovery integration tests.
15. README.md, DECISIONS.md and AI_USAGE.md are accurate and complete; no fabricated AI-use anecdotes.

## 11. Scope exclusions and tradeoffs
No UI, auth, production email/webhook integrations, Kubernetes, Kafka, Celery, distributed tracing, guaranteed exactly-once external side effects, fairness aging, or large-scale load benchmark. Dead-letter queue is out of scope; FAILED records remain queryable. Preserve simplicity and explain choices in DECISIONS.md.

## 12. Submission requirements
Git repository with README.md (run, tests, example submit, architecture), DECISIONS.md (claim strategy, crash recovery, priority, backoff, what to improve), AI_USAGE.md (actual tools, helpful cases, actual corrections/limitations), Dockerfile, docker-compose.yml, source, tests. Every line must be explainable in follow-up interview.


## 13. Quality requirements and simplicity constraints (v1.1)
The objective is the **simplest correct, defensible solution**, not minimum lines of code. Keep modules small with explicit ownership (API/routes, schemas, DB/repository, worker orchestration, handlers, configuration). Avoid premature dependency-injection frameworks, elaborate inheritance, generic task DSLs, unnecessary async/sync bridges, and abstractions with only one concrete use unless they isolate a real boundary. Prefer existing standard-library features and a small dependency set.

- **Readability:** meaningful names, typed public interfaces, predictable control flow, concise comments only for non-obvious concurrency/failure behavior.
- **Efficiency:** no busy-wait loops; bounded polling, indexed eligibility queries, short transactions, no DB session sharing across threads, no N+1 queries in list/status paths, pagination with bounded limit. Do not optimize without a measured or obvious bottleneck.
- **Correctness:** concurrency invariants outrank elegance. Every state-changing query has explicit eligibility predicates and checked affected-row count. Database time governs eligibility/leases.
- **Maintainability:** avoid duplicate transition rules across API, worker and recovery; centralize shared retry timing and transition logic without introducing a general-purpose workflow engine.
- **Explainability:** candidate must be able to describe each module, critical query, transaction boundary, recovery window, and design tradeoff in an interview.
- **Review gate each phase:** inspect new dependencies, unused code, layering, exception handling, transaction lifetimes, query/index alignment, log safety and deterministic testability. Remove unnecessary complexity only when doing so does not destabilize verified behavior.

## 14. Test strategy and coverage policy (v1.1)
Testing is incremental: write/run relevant tests in the same phase as each feature. Phase 5 is cross-component integration/hardening, **not** the first testing phase; Phase 6 is full regression and fresh-environment verification.

**Targets for final submission (project quality goals, not assignment mandates):**
- >= **85% line coverage** of measured application code (`pytest --cov=app --cov-report=term-missing --cov-report=xml`), with migrations, tests, generated files, and pure entrypoint/bootstrap boilerplate excluded via a documented configuration.
- Aim for >= **90% line coverage in critical business logic modules**: claiming, state transitions, retry scheduling, idempotency and lease recovery. Report module-specific measurements; do not fabricate or claim these numbers until measured.
- Coverage is a **supporting metric**, never a substitute for meaningful assertions. Prioritize concurrency correctness, failure paths and integration evidence over chasing a percentage. Early phases have **no artificial global percentage gate**; new behavior must have meaningful tests.
- Unit tests: payload validation, retry calculations, transitions, deterministic handlers and error mapping.
- Integration tests: real PostgreSQL for atomic claim, idempotency races, cancellation races, leases and fencing. SQLite/mocks are not valid substitutes for these semantics.
- End-to-end tests: Compose API -> DB -> worker -> DB, multiple workers, Redis unavailable, restart and shutdown. Avoid flaky sleep-based tests where possible; use controlled clocks/configurable short intervals, barriers and deterministic handlers. Distinguish automated tests from manual smoke demonstrations.
- CI is optional for the 48-hour task; tests must be runnable with a documented local command.
- Record exact commands, passed/failed/skipped counts, actual coverage output and known flaky tests in the implementation plan. A checkbox may be checked only when verified.

### Edge-case matrix (test IDs are stable across plan and test names)
| ID | Area | Required scenario | Earliest phase | Test type |
|---|---|---|---|---|
| EC-01 | API | invalid/missing type-specific payload; invalid priority/date/limit; scheduled_at boundaries (exactly 30s past accepted, beyond 30s/far past/naive rejected) | 1 | Unit + API |
| EC-02 | API | missing job ID -> 404; invalid state action -> 409 | 1/3 | API |
| EC-03 | Idempotency | two simultaneous submissions, same active key -> one job | 1 | PostgreSQL integration |
| EC-04 | Idempotency | expired key reused atomically; old job retained | 1/5 | PostgreSQL integration |
| EC-05 | Cancellation | cancel vs claim race has one winner | 2/5 | PostgreSQL integration |
| EC-06 | Claim | two workers cannot claim same eligible job | 2 | PostgreSQL integration |
| EC-07 | Priority | high before low; FIFO tie-break; no preemption | 3 | DB integration |
| EC-08 | Scheduling | not early; exactly due becomes eligible | 3 | DB integration |
| EC-09 | Retry | 30/120 backoff; terminal after attempt 3 | 3 | Unit + DB integration |
| EC-10 | Manual retry | FAILED only; reset attempts and preserve history | 3 | API + DB integration |
| EC-11 | Crash recovery | lease expiry requeues with correct attempt/backoff | 4 | PostgreSQL integration |
| EC-12 | Fencing | stale worker completion/progress/heartbeat rejected | 4 | PostgreSQL integration |
| EC-13 | Heartbeat | active job with live heartbeat not reclaimed | 4 | PostgreSQL integration |
| EC-14 | Redis | missed publish or Redis outage -> polling still processes | 2/5 | End-to-end |
| EC-15 | PostgreSQL | failure before commit never acknowledges successful submission | 5 | Integration/manual fault injection |
| EC-16 | Batch | progress monotonic; completion summary correct | 2/5 | Unit + integration |
| EC-17 | Shutdown | SIGTERM drains active bounded job; SIGKILL recovers | 4/5 | Process integration |
| EC-18 | Timeout | stuck handler cannot heartbeat forever; recovery is possible | 4/5 | Process integration |
| EC-19 | Multi-worker | three replicas complete eligible jobs without duplicate valid claims | 5 | End-to-end |
| EC-20 | Submission | fresh Compose boot, migrations, all tests and docs work | 6 | Clean-environment smoke |

## 15. Definition of done and traceability (v1.1)
A feature is complete only when implementation, positive-path test, relevant negative/edge-case test, and an actual executed verification are documented. Each phase has its own implementation, tests, and quality checkboxes in `PLAN.md`. Mark `[x]` **only after** successful verification, attach exact commands/results and record deviations; never infer success from generated code or a tool's unsupported claim. Do not start the next phase without human approval. `AGENTS.md` defines the agent workflow.

**Priority if time is constrained:** preserve all mandatory assignment behavior and correctness of claims/retries/idempotency. Coverage targets are quality goals, not a reason to skip essential functionality or fabricate results. Optional polish is reduced before correctness testing. A missing coverage target must be reported transparently.
