# Job Queue Service

Distributed background job processing: FastAPI API, PostgreSQL as the authoritative queue/state store,
Redis as a best-effort wake-up channel, and separate Python worker processes.

> Work in progress (Phase 3 of 6). Execution, retry with backoff, priority and scheduling work;
> crash recovery (lease expiry) and graceful-shutdown hardening arrive in Phase 4. See `PLAN.md`.

## Run

```bash
docker compose up --build
```

This starts PostgreSQL, Redis, a one-shot `migrate` service (`alembic upgrade head`), the API on
http://localhost:8000 (OpenAPI docs at `/docs`) and the worker.

## Test

Tests run inside Docker against the Compose PostgreSQL (database `jobqueue_test`, created automatically)
and Redis:

```bash
docker compose --profile test run --rm --build tests pytest -v --cov=app --cov-report=term-missing
```

## Example requests

```bash
curl -s -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"type":"email","payload":{"to":"user@example.com","subject":"Hi"},"priority":5,"idempotency_key":"welcome-42"}'
curl -s localhost:8000/jobs/<id>
curl -s 'localhost:8000/jobs?status=PENDING&type=email&limit=20&offset=0'
curl -s -X POST localhost:8000/jobs/<id>/cancel
curl -s -X POST localhost:8000/jobs/<id>/retry     # FAILED jobs only
curl -s localhost:8000/health
```

Payloads per type: `email {to, subject, body?}`, `webhook {url, body?}` (simulated, never called),
`report {report_name}`, `batch {items: [...] (1..100)}`. `priority` is 0–10 (higher first).
`scheduled_at` must be timezone-aware: a future value schedules the job, a value up to 30 seconds in the
past runs it now, and anything older, or more than 365 days ahead, is rejected with 422. A repeated active `idempotency_key` returns the
original job with HTTP 200, even if the new payload differs.

## Worker

Each worker process runs one job at a time; scale with `docker compose up --scale worker=3`.
A worker claims the highest-priority eligible `PENDING` job with `SELECT ... FOR UPDATE SKIP LOCKED`
in a short transaction, runs the mock handler outside any transaction, and writes progress and the
result only while it still holds the job's claim token. When idle it waits for a Redis wake-up hint
for at most `WORKER_POLL_INTERVAL_SECONDS`, then polls PostgreSQL again, so jobs are found even if
Redis is down. On SIGTERM it finishes the current job before exiting.

Before each claim, a worker moves due `SCHEDULED` jobs to `PENDING` (never early; at most one poll
interval late). Higher `priority` is claimed first, oldest first within a priority; a running job is
never preempted.

A failed attempt is retried automatically: attempt 2 runs 30s after attempt 1 fails, attempt 3 runs
120s after attempt 2 fails (`RETRY_DELAYS_SECONDS`), and after attempt 3 the job is `FAILED`.
While waiting for a retry the job is `PENDING` and `last_error` shows the latest error; success clears
it. `POST /jobs/{id}/retry` re-opens a `FAILED` job with attempts reset to 0; every attempt stays in
the job log.

Mock handlers: email sleeps 1–3s, webhook 1–2s with a `WEBHOOK_FAILURE_RATE` chance of a simulated
failure (no HTTP request is made), report 3–5s, batch `BATCH_ITEM_DELAY_SECONDS` per item with
progress updates.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://jobqueue:jobqueue@localhost:5432/jobqueue` | PostgreSQL (SQLAlchemy URL) |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis for wake-up notifications |
| `IDEMPOTENCY_TTL_HOURS` | `24` | Idempotency key retention (minimum 24) |
| `SCHEDULE_PAST_TOLERANCE_SECONDS` | `30` | How far in the past `scheduled_at` may be |
| `SCHEDULE_MAX_AHEAD_DAYS` | `365` | How far in the future `scheduled_at` may be |
| `REDIS_TIMEOUT_SECONDS` | `0.5` | Socket timeout for best-effort Redis calls |
| `WORKER_ID` | `<hostname>-<pid>` | Worker identity stored on claimed jobs |
| `WORKER_POLL_INTERVAL_SECONDS` | `2` | Max idle wait before polling PostgreSQL (0 < x <= 2) |
| `LEASE_SECONDS` | `30` | Claim lease length (recovery arrives in Phase 4) |
| `WEBHOOK_FAILURE_RATE` | `0.2` | Probability of a simulated webhook failure (0..1) |
| `BATCH_ITEM_DELAY_SECONDS` | `0.2` | Simulated processing time per batch item |
| `RETRY_DELAYS_SECONDS` | `30,120` | Delays before automatic attempts 2 and 3 |

`docker-compose.yml` sets container values; `.env.example` lists them.
