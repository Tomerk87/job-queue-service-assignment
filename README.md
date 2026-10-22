# Job Queue Service

A distributed background job processing system: a FastAPI API accepts jobs, PostgreSQL stores them
and acts as the queue, separate Python worker processes claim and run them, and Redis delivers
best-effort wake-up hints. It supports priorities, scheduled jobs, retry with backoff, cancellation,
idempotent submission, crash recovery with leases and heartbeats, graceful shutdown and structured
JSON logs.

- Design rationale and trade-offs: [DECISIONS.md](DECISIONS.md)
- AI tool usage: [AI_USAGE.md](AI_USAGE.md)
- Specification, plan and verification record: [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md),
  [PLAN.md](PLAN.md), [EVIDENCE.md](EVIDENCE.md)

## Architecture

```
                    POST /jobs (commit first)             claim: UPDATE ... WHERE id =
 ┌────────┐  HTTP  ┌──────────┐ ───────────────► ┌──────────────┐ ◄──── (SELECT ... FOR UPDATE ┌──────────┐
 │ Client │ ─────► │ API      │                  │ PostgreSQL   │        SKIP LOCKED)           │ Worker   │ x N
 └────────┘        │ FastAPI  │ ◄─────────────── │ jobs (queue  │ ◄──── heartbeat, progress,    │ process  │
                   └────┬─────┘  status / list   │ + state),    │       result (token-fenced)  └────▲─────┘
                        │                        │ job_logs,    │                                   │
                        │ PUBLISH jobs:created   │ idempotency_ │                                   │
                        │ (after commit,         │ keys         │                                   │
                        ▼  best effort)          └──────────────┘                                   │
                   ┌──────────┐                                       SUBSCRIBE: wake-up hint only  │
                   │ Redis    │ ────────────────────────────────────────────────────────────────────┘
                   └──────────┘   (workers also poll PostgreSQL every ≤2s, so Redis is optional)
```

| Component | Responsibility | Code |
|---|---|---|
| API | Validate and accept jobs, read state, cancel, retry, health. Never executes jobs. | `app/main.py`, `app/schemas.py` |
| Job store | All state transitions as short, conditional SQL statements (claim, fence, retry rule, recovery, idempotency) | `app/jobs.py`, `app/models.py`, `alembic/` |
| Worker | Claim → run handler (no open transaction) → record result. Heartbeat, lease recovery, scheduled release, graceful shutdown. | `app/worker.py` |
| Handlers | Mock email / webhook / report / batch jobs | `app/handlers.py` |
| Notifier | Best-effort Redis publish after commit | `app/notifier.py` |
| Config, logging | Typed env settings, JSON logs | `app/config.py`, `app/logging_config.py` |

## Run

Requirements: Docker with Compose v2. No local Python is needed.

```bash
docker compose up -d --build --wait
```

This starts PostgreSQL, Redis, a one-shot `migrate` service (`alembic upgrade head`; the API and the
workers start only after it succeeds), the API on http://localhost:8000 (interactive docs at
http://localhost:8000/docs) and one worker.

```bash
docker compose up -d --scale worker=3   # more workers
docker compose logs -f worker           # JSON logs: event, job_id, worker_id
docker compose stop worker              # graceful: finishes the current job first
docker compose down -v                  # stop and delete the data volume
```

## Submit a test job

```bash
curl -s -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"type": "email", "payload": {"to": "user@example.com", "subject": "Hi"}, "priority": 5}'
```

The response is `201 Created` with the job, initially `"status": "PENDING"`. Poll it until it
completes:

```bash
curl -s localhost:8000/jobs/<id>
```

A completed job looks like this:

```json
{
  "id": "6f0c…", "type": "email", "status": "COMPLETED",
  "payload": {"to": "user@example.com", "subject": "Hi"},
  "priority": 5, "attempts": 1, "max_attempts": 3, "progress": 100,
  "result": {"message_id": "msg-3f9a…"}, "last_error": null,
  "idempotency_key": null, "worker_id": "a1b2c3d4e5f6-1",
  "scheduled_at": null, "next_run_at": "…", "created_at": "…", "updated_at": "…",
  "started_at": "…", "completed_at": "…"
}
```

## API

| Method and path | Purpose | Status codes |
|---|---|---|
| `POST /jobs` | Submit a job | `201` created · `200` existing job for an active idempotency key · `422` invalid · `503` database unavailable |
| `GET /jobs/{id}` | Status, attempts, progress, result or error | `200` · `404` · `422` malformed id |
| `GET /jobs?status=&type=&limit=&offset=` | List, newest first; `limit` 1–100 (default 50) | `200` · `422` |
| `POST /jobs/{id}/cancel` | Cancel a `PENDING` or `SCHEDULED` job (repeating is a no-op) | `200` · `404` · `409` processing/completed/failed |
| `POST /jobs/{id}/retry` | Reopen a `FAILED` job with attempts reset to 0 | `200` · `404` · `409` not failed |
| `GET /health` | Database and Redis status, job counts per status | `200` `ok` or `degraded` (Redis down) · `503` `unhealthy` (database down) |

Any endpoint returns `503 {"detail": "database unavailable"}` while PostgreSQL is unreachable;
nothing is committed in that case.

**Submit body:** `type` (required), `payload` (required), `priority` 0–10 (default 0, higher runs
first), `scheduled_at` (optional, must include a timezone), `idempotency_key` (optional, 1–255
characters).

| Type | Payload | Simulated work | Result |
|---|---|---|---|
| `email` | `{to, subject, body?}` | 1–3 s | `{message_id}` |
| `webhook` | `{url, body?}`; no HTTP request is ever made | 1–2 s, fails 20% of the time | `{status_code: 200}` |
| `report` | `{report_name}` | 3–5 s | `{report_url}` |
| `batch` | `{items: [...]}` with 1–100 items | 0.2 s per item, progress after each | `{total, processed}` |

**Rules:**
- **Scheduling:** a future `scheduled_at` makes the job `SCHEDULED` until that time. A value up to
  30 s in the past runs it now. Anything older, or more than 365 days ahead, is rejected with `422`.
- **Idempotency:** resubmitting an active `idempotency_key` (kept for 24 h) returns the original job
  with `200`, even if the payload differs. After expiry the key creates a new job.

```bash
# idempotent submit: the second call returns 200 with the same job
curl -s -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"type": "report", "payload": {"report_name": "q3"}, "idempotency_key": "report-q3"}'
# scheduled in 1 minute (GNU date; on macOS: date -u -v+1M +%Y-%m-%dT%H:%M:%SZ)
curl -s -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d "{\"type\": \"email\", \"payload\": {\"to\": \"a@b.co\", \"subject\": \"later\"}, \"scheduled_at\": \"$(date -u -d '+1 min' +%Y-%m-%dT%H:%M:%SZ)\"}"
curl -s 'localhost:8000/jobs?status=COMPLETED&type=email&limit=20'
curl -s -X POST localhost:8000/jobs/<id>/cancel
curl -s -X POST localhost:8000/jobs/<id>/retry
curl -s localhost:8000/health
```

## Job lifecycle

```
                scheduled_at reached               claimed by a worker
 SCHEDULED ─────────────────────────► PENDING ─────────────────────► PROCESSING ── success ──► COMPLETED
     │                                 │   ▲                              │
     │ cancel                   cancel │   │                              │ error, timeout or expired lease
     ▼                                 ▼   │                              ▼
 CANCELLED                       CANCELLED │            attempts < 3 ──► back to PENDING (after 30 s, then 120 s)
                                           │            attempts = 3 ──► FAILED
                                           │                               │
                                           └───── POST /jobs/{id}/retry ───┘  (FAILED only; attempts reset to 0)
```

## Reliability

- **Atomic pickup:** workers claim with `SELECT ... FOR UPDATE SKIP LOCKED` in one short
  transaction, so two workers can never claim the same job. The highest priority goes first, oldest
  first within a priority, and a running job is never preempted.
- **Lease and heartbeat:** a claim gives the worker a 30 s lease (`LEASE_SECONDS`), renewed every 5 s
  by a heartbeat thread. Every progress, completion or failure write requires the worker's claim
  token *and* an unexpired lease, so a paused or replaced worker can't overwrite newer state.
- **Crash recovery:** every worker scans for expired leases every 5 s. A crashed or killed worker's
  job counts as a failed attempt and follows the normal retry rule.
- **Retry with backoff:** attempt 2 runs 30 s after attempt 1 fails, attempt 3 runs 120 s after
  attempt 2 fails, and then the job is `FAILED`. While a job waits, `last_error` shows the latest
  error; every attempt stays in `job_logs`.
- **Timeouts:** an attempt running longer than `JOB_TIMEOUT_SECONDS` (60 s) fails and is retried. A
  handler that hangs without returning stops being renewed. One lease later the worker process
  exits, Docker restarts it (`restart: unless-stopped`), and recovery requeues the job.
- **Graceful shutdown:** on SIGTERM a worker stops claiming and finishes its current job (the
  heartbeat keeps running) within the 30 s stop grace period.
- **Outages:**
  - Redis down: `/health` reports `degraded` and jobs are still processed by polling.
  - PostgreSQL down: the API returns `503` and commits nothing, and workers back off and retry.
- **Delivery guarantee:** **at-least-once.** A job can run more than once (for example, when a worker
  crashes after a side effect but before saving completion). Real integrations need idempotent side
  effects.
- **Logs:** the API and the workers write one JSON object per line (`ts`, `level`, `logger`,
  `message`, plus `event`, `job_id`, `worker_id`). Payloads are never logged.

## Tests

All tests run in Docker against real PostgreSQL and Redis. The `jobqueue_test` database is created
automatically and migrated down and up at the start of each run:

```bash
docker compose --profile test run --rm --build tests pytest -v --cov=app --cov-report=term-missing
```

The suite covers the six assignment categories (submission and retrieval, completion, failure and
retry, cancellation, idempotency, priority ordering), concurrency races with real threads
(claim, cancel vs claim, idempotency, recovery), lease fencing and heartbeats, and real worker
subprocesses:
- SIGTERM drain;
- SIGKILL followed by recovery;
- a hung handler;
- three concurrent workers;
- a full Redis outage.

[EVIDENCE.md](EVIDENCE.md) maps every category and edge case (EC-01–EC-19) to its tests and records
the measured results and coverage. Only `app/` is measured; the single exclusion is
`if __name__ == "__main__":`.

### End-to-end demo (manual)

`scripts/smoke.sh` drives a running Compose stack through every scenario and prints PASS/FAIL for each
check (about 3 minutes, because crash recovery waits for a real 30 s lease and a 30 s retry delay):
1. all job types complete;
2. idempotency returns the original job;
3. a pending job is cancelled;
4. a failing job retries, ends `FAILED`, and completes after a manual retry;
5. priority order is respected;
6. a scheduled job starts on time, never early;
7. jobs still run while Redis is down;
8. SIGTERM drains the running job;
9. a job survives a SIGKILL through recovery;
10. three workers never claim the same job twice.

```bash
docker compose up -d --build --wait
scripts/smoke.sh
```

### Verify from a fresh clone

Every check from scratch, one command at a time (about 4 minutes in total). Results appear in the
terminal and are also saved to `test-results.txt` and `smoke-results.txt`.

0. **Prerequisites:** Docker is running and port 8000 is free. If another copy of this stack is
   running, stop it in that directory with `docker compose --profile test down -v`.
1. **Get the code:**
   ```bash
   git clone https://github.com/Tomerk87/job-queue-service-assignment.git
   cd job-queue-service-assignment
   ```
2. **Build all images from scratch** (usually under a minute; depends on download speed):
   ```bash
   docker compose --profile test build --no-cache
   ```
3. **Run the automated test suite** (about 40 s; it starts PostgreSQL and Redis itself):
   ```bash
   docker compose --profile test run --rm tests pytest -v --cov=app --cov-report=term-missing | tee test-results.txt
   ```
   Expected: a coverage table and a final line such as `177 passed, 1 warning in 38.52s`.
4. **Start the application** (migrations run first, then the API and the worker):
   ```bash
   docker compose up -d --wait
   ```
5. **Check the migrations:**
   ```bash
   docker compose logs migrate
   ```
   Expected: `Running upgrade  -> 0001` and `Running upgrade 0001 -> 0002`.
6. **Run the end-to-end demo** (about 2.5 min):
   ```bash
   scripts/smoke.sh | tee smoke-results.txt
   ```
   Expected: `ALL CHECKS PASSED` as the last line.
7. **Clean up** (stops everything and deletes the data volume):
   ```bash
   docker compose --profile test down -v
   ```

[EVIDENCE.md](EVIDENCE.md) is a hand-written record of verification runs. Running these commands
doesn't update it automatically.

## Configuration

All settings are environment variables with working defaults. `docker-compose.yml` sets them for the
containers, and [.env.example](.env.example) documents them.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://jobqueue:jobqueue@localhost:5432/jobqueue` | PostgreSQL (SQLAlchemy URL) |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis for wake-up hints |
| `IDEMPOTENCY_TTL_HOURS` | `24` | Idempotency key retention (minimum 24) |
| `SCHEDULE_PAST_TOLERANCE_SECONDS` | `30` | How far in the past `scheduled_at` may be |
| `SCHEDULE_MAX_AHEAD_DAYS` | `365` | How far in the future `scheduled_at` may be |
| `REDIS_TIMEOUT_SECONDS` | `0.5` | Socket timeout for best-effort Redis calls |
| `WORKER_ID` | `<hostname>-<pid>` | Worker identity stored on claimed jobs |
| `WORKER_POLL_INTERVAL_SECONDS` | `2` | Max idle wait before polling PostgreSQL (0 < x ≤ 2) |
| `LEASE_SECONDS` | `30` | Claim lease length, renewed by the heartbeat |
| `HEARTBEAT_INTERVAL_SECONDS` | `5` | Lease renewal interval (must be < lease) |
| `RECOVERY_INTERVAL_SECONDS` | `5` | How often each worker recovers expired leases |
| `JOB_TIMEOUT_SECONDS` | `60` | Maximum run time of one attempt |
| `RETRY_DELAYS_SECONDS` | `30,120` | Delays before automatic attempts 2 and 3 |
| `WEBHOOK_FAILURE_RATE` | `0.2` | Probability of a simulated webhook failure (0–1) |
| `BATCH_ITEM_DELAY_SECONDS` | `0.2` | Simulated processing time per batch item |

## Dependencies

`requirements.txt` (runtime) and `requirements-dev.txt` (tests) list the direct dependencies.
[constraints.txt](constraints.txt) locks every installed package, transitive ones included, to the
exact tested versions, and the Dockerfile installs with `-c constraints.txt`. To regenerate it after
changing requirements:

```bash
docker compose --profile test build tests
docker run --rm job-queue-service:test pip freeze --all | grep -viE '^(pip|setuptools|wheel)==' > constraints.txt
```

Then put the explanatory header comment back at the top of the file.

## Project layout

```
app/
  main.py            API routes, 503 on database outage
  schemas.py         request/response models, per-type payload validation
  jobs.py            persistence and every state transition (claim, fencing, retry rule, recovery)
  worker.py          worker loop, heartbeat, recovery timer, Redis wake-ups, shutdown
  handlers.py        mock job handlers
  models.py          SQLAlchemy models
  notifier.py        best-effort Redis publish
  config.py          typed settings from environment variables
  logging_config.py  JSON logging
  db.py              engine and sessions
alembic/             migrations (0001 schema, 0002 listing index)
tests/               pytest suite (real PostgreSQL/Redis, worker subprocesses)
scripts/smoke.sh     manual end-to-end demo against Compose
```
