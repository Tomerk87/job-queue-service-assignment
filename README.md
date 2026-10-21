# Job Queue Service

Distributed background job processing: FastAPI API, PostgreSQL as the authoritative queue/state store,
Redis as a best-effort wake-up channel, and separate Python worker processes.

> Work in progress (Phase 1 of 6). The worker container is currently idle; job execution,
> retries, priority claiming and crash recovery arrive in later phases. See `PLAN.md`.

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
curl -s localhost:8000/health
```

Payloads per type: `email {to, subject, body?}`, `webhook {url, body?}` (simulated, never called),
`report {report_name}`, `batch {items: [...] (1..1000)}`. `priority` is 0–10 (higher first).
`scheduled_at` must be timezone-aware: a future value schedules the job, a value up to 30 seconds in the
past runs it now, and anything older is rejected with 422. A repeated active `idempotency_key` returns the
original job with HTTP 200, even if the new payload differs.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://jobqueue:jobqueue@localhost:5432/jobqueue` | PostgreSQL (SQLAlchemy URL) |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis for wake-up notifications |
| `IDEMPOTENCY_TTL_HOURS` | `24` | Idempotency key retention (minimum 24) |
| `SCHEDULE_PAST_TOLERANCE_SECONDS` | `30` | How far in the past `scheduled_at` may be |
| `REDIS_TIMEOUT_SECONDS` | `0.5` | Socket timeout for best-effort Redis calls |

`docker-compose.yml` sets container values; `.env.example` lists them.
