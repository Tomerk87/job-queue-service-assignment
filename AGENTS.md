# AGENTS.md — Coding Agent Instructions (v1.1)

## Mission and source hierarchy
Read `TECHNICAL_SPEC.md` (authoritative requirements/design) and `PLAN.md` (checklists, tests, gates) before each phase. If documents conflict, stop and ask rather than guessing.

## Architecture — do not change without approval
- Python 3.11+, FastAPI, SQLAlchemy/Alembic, PostgreSQL, Redis, separate Python worker, Docker Compose.
- PostgreSQL is the authoritative logical queue/state store. Redis is only best-effort notification; bounded DB polling is the correctness fallback.
- Claim with `SELECT ... FOR UPDATE SKIP LOCKED` in short transactions; do not execute handlers under DB locks.
- Use token-fenced heartbeat, progress and completion; leases + recovery; 3 attempts, 30s then 120s retry; manual retry resets attempts.
- Priority among eligible jobs, scheduled execution, 24h race-safe idempotency, bounded handlers, graceful shutdown.
- At-least-once execution, **not** exactly-once external side effects. Do not introduce Celery, Kafka, RabbitMQ, Redis Streams, Kubernetes or new infrastructure.

## Simplicity, efficiency and code review
Choose the **simplest correct implementation**. Favor small explicit functions, clear module boundaries, typed interfaces, consistent naming, focused error handling and minimal dependencies. Avoid unnecessary classes, generic frameworks, needless async/sync bridging, premature caching, speculative abstractions, duplicate transition logic and speculative performance optimization. Optimize for interview explainability.

Keep transactions short; no DB session sharing across threads; indexed, bounded eligibility queries; bounded polling (no busy loops); pagination; UTC-aware timestamps and DB time for leases. Never swallow exceptions or log secrets/sensitive payloads. Every phase requires a documented Code Quality Gate review of readability, maintainability, efficiency, query behavior, dependencies and correctness.

## Tests, coverage and edge cases
- Implement and run relevant unit/integration tests **within the phase that introduces each feature**. Phase 5 is full integration/edge-case hardening; Phase 6 is fresh-environment regression.
- Use **real PostgreSQL** for concurrent claim, cancellation races, idempotency races, lease/recovery and fencing. Never substitute SQLite/mocks for claims of PostgreSQL concurrency correctness.
- Use deterministic handlers/configurable short test intervals; avoid relying on randomness or long sleeps. Test both positive and negative paths. Refer to `TECHNICAL_SPEC.md` section 14 (EC-01..EC-20).
- Final target >=85% application **line coverage** and aim >=90% for critical logic modules (claim, state transitions, retry, idempotency, recovery). Measure with `pytest-cov`, document exclusions and module breakdown. These are project goals and **not** per-phase blocking percentages. Meaningful assertions and correctness outrank numeric coverage.
- Never fabricate test results or coverage. Never mark a test passed unless actually executed successfully. Record exact commands and pass/fail/skip counts.

## Checkbox and status protocol (mandatory)
1. Before starting, read the current phase in `PLAN.md` and change its status from `NOT STARTED` to `IN PROGRESS` (or `BLOCKED` if unable to proceed).
2. Work only on that phase. Update `[ ]` to `[x]` **only when an implementation item is implemented and verified**, a test is **actually run and passes**, or a DoD/quality gate is actually satisfied.
3. If a test fails or cannot run, leave it `[ ]`, describe the failure, and mark `BLOCKED` when it prevents the gate. Do not skip, disable or weaken tests to make a checklist green.
4. Update **Execution Evidence** with commands, results, EC IDs, measured coverage (if any), known issues, deviations and commit hash. Keep unresolved items unchecked.
5. Set status `COMPLETED` only after all required gates are verified **and human approval for the next phase is recorded**. Stop and ask for approval; never autonomously begin the next phase.
6. Never pre-check future phases or invent evidence. The plan is a live execution record, not merely a wish list.
7. Before executing each phase, verify everything is understandable. If there are any unclear instructions, ask me to decide, and only after full approval execute the phase.

## Per-phase execution protocol
1. Read `CLAUDE.md`, `AGENTS.md`, `TECHNICAL_SPEC.md`, `PLAN.md`.
2. Summarize planned files, tests, edge cases and potential risks; request clarification only for blocking ambiguity.
3. Implement current phase with minimal scope; do not change approved contracts.
4. Run relevant tests, inspect actual outputs and fix failures in scope.
5. Review simplicity/efficiency, update checkboxes/evidence, report exact outcomes and stop for human review.

## Documentation and submission
Required artifacts: README.md, DECISIONS.md, AI_USAGE.md, Dockerfile, docker-compose.yml, tests and working code. Record **actual** AI tools, assistance, mistakes and fixes throughout; never fabricate examples. Explain concurrency invariants, crash windows, priority fairness and PostgreSQL/Redis tradeoffs. All repository docs/code in English.

## Explicit exclusions
No frontend, authentication, real email/webhook delivery, Kubernetes, broad event platform, generic plugin system, exactly-once claims or unrelated features. Preserve correctness over optional polish.
