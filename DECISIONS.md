# Design Decisions

The guiding rule: **PostgreSQL is the single source of truth and the actual queue.** Every state
change is one short, conditional SQL statement whose affected-row count is checked. Redis is only a
wake-up hint, and the system stays correct without it. Execution is **at-least-once**; exactly-once
external side effects are not claimed anywhere.

## 1. Job Pickup Strategy

**Approach chosen:** atomic claim in PostgreSQL with `SELECT ... FOR UPDATE SKIP LOCKED`, inside one
short transaction:

```sql
UPDATE jobs
SET status = 'PROCESSING', attempts = attempts + 1, claim_token = :fresh_uuid,
    worker_id = :worker, started_at = now(), lease_expires_at = now() + :lease, progress = 0
WHERE id = (SELECT id FROM jobs
            WHERE status = 'PENDING' AND next_run_at <= now()
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED)
RETURNING id, type, payload, attempts;
```

A `claimed` job-log row is written in the same transaction, and the handler runs only after the
commit. Every later write by that worker (progress, completion, failure, heartbeat) is a conditional
UPDATE that requires `status = 'PROCESSING'`, the worker's `claim_token` and an unexpired lease.
Idle workers wait on a Redis pub/sub channel (`jobs:created`) for at most 2 seconds, then poll
PostgreSQL again.

**Why:**
- `SKIP LOCKED` makes concurrent workers pass over a row another transaction has locked. Two workers
  can never claim the same job, and they don't block each other.
- The job row is both the queue entry and the state. There is no dual write (DB commit plus queue
  push) that could leave a job lost or phantom when one side fails.
- The API commits the job *before* publishing to Redis, so a lost or failed publish only delays
  pickup until the next poll.

**Trade-offs:**
- *Gained:* a single consistent store, transactional cancel-vs-claim, simple crash recovery
  (everything is in one row), and correctness that doesn't depend on Redis delivery.
- *Gave up:* raw throughput and broker features. Each idle worker runs a few indexed queries every
  2 seconds, and claim throughput is bounded by PostgreSQL write capacity. That's fine for this scale
  (a partial index serves the claim query) but not a replacement for Kafka-scale streaming.
- The assignment's "queue/cache technology" is used only for wake-ups; the queue itself is a
  PostgreSQL table. That is a deliberate choice for correctness.

---

## 2. Worker Crash Recovery

**Approach chosen:** leases plus a heartbeat plus a recovery scan. The time-based mechanisms are all
in database time.
- **Lease and heartbeat:** a claim sets `lease_expires_at = now() + 30s`. While the handler runs, a
  heartbeat thread (with its own DB sessions) extends the lease every 5s with a fenced UPDATE.
- **Recovery scan:** every 5s each worker runs a bounded recovery UPDATE (`SKIP LOCKED`, at most
  100 rows) on `PROCESSING` jobs whose lease has expired.
- **Lease is authority:** every fenced write also requires `lease_expires_at > now()`. Recovery
  requires `lease_expires_at < now()`. The row lock serializes the two, so a job is either renewed
  or recovered, never both, and a late completion can never overwrite a recovered job.
- **Hung handlers:** `JOB_TIMEOUT_SECONDS` (60s) is enforced cooperatively, and the attempt fails and
  is retried. A handler that never yields stops being renewed at its deadline. If it is still
  running one lease later, the worker process exits (`os._exit(1)`), Docker's restart policy starts
  a fresh worker, and recovery requeues the job.

**Why:** a crashed or killed process can't clean up after itself, so ownership must expire on its
own. The heartbeat lets long jobs keep a short lease, so crashes are detected in about 30s without
capping job length at 30s. The claim token *fences* a worker that is merely slow or paused: it can
resume but can no longer change the job.

**What happens if a worker crashes mid-job:**
1. The worker held the job with `claim_token = T1` and a lease renewed every 5s.
2. It crashes (or receives SIGKILL). Renewals stop, and the lease expires at most 30s after the last
   renewal.
3. At the next recovery scan, some worker matches the job (`PROCESSING`, lease expired) and applies
   the same retry rule as a handler error:
   - attempts < 3 → `PENDING` with `next_run_at = now() + 30s` (or `+120s` after attempt 2);
   - attempt 3 → `FAILED`.

   `claim_token` is cleared, `last_error = "lease expired (worker <id>)"`, and `lease_expired` plus
   `retry_scheduled`/`failed` rows are logged.
4. After the retry delay, any worker claims the job with a fresh token `T2`.
5. If the "crashed" worker was only paused and wakes up, every write with `T1` matches no row and is
   discarded ("lost ownership"). The database is safe, but any external side effect it already
   performed may happen twice. That is why execution is at-least-once.

Detection takes at most *lease + recovery interval* (about 35s) when any worker is idle. A worker that is busy with a job scans only between jobs, so if every worker is busy, recovery waits for the first one to finish (jobs are bounded by the timeout). A graceful stop
(SIGTERM, or `docker compose stop`) doesn't use recovery at all: the worker stops claiming and
finishes its current job within the 30s grace period, with the heartbeat still running.

---

## 3. Priority Queue Implementation

**Approach chosen:** the claim query orders eligible jobs by `priority DESC, created_at ASC`
(priority 0–10, higher first; FIFO within a priority). It is served by the partial index
`ix_jobs_pending_priority (priority DESC, created_at ASC) WHERE status = 'PENDING'`.

**Why:** priority is just a property of the claim query, so it needs no separate structures,
per-priority queues or broker features. The partial index keeps the "next job" lookup cheap
because it only contains pending rows.

**Limits, stated honestly:**
- Priority applies to *eligible* jobs at claim time. A running job is never preempted.
- A high-priority job that is waiting for a retry delay or its schedule doesn't block lower-priority
  work that is eligible now.
- A continuous stream of high-priority jobs can starve low-priority ones. There is no aging. Adding
  it would mean ordering by an effective priority that grows with wait time.

---

## 4. Retry Backoff Strategy

**Approach chosen:** one SQL rule, `after_failed_attempt()` in `app/jobs.py`, used both for handler
errors and for expired leases:
- `attempts` is incremented at claim time.
- If `attempts < max_attempts` (3), the job goes back to `PENDING` with
  `next_run_at = now() + delay[attempts]`. Otherwise it becomes `FAILED`.
- `last_error` keeps the latest error while the job waits; it is cleared when the job completes.
  Every attempt's error remains in `job_logs`.
- `POST /jobs/{id}/retry` reopens a `FAILED` job as a new cycle (attempts reset to 0). Any other
  state gets 409.

**Timing:**
- Attempt 1 is immediate.
- Attempt 2 is **30 s** after attempt 1 fails.
- Attempt 3 is **120 s** after attempt 2 fails.
- After attempt 3 the job is `FAILED` permanently. There is no automatic retry of `FAILED`.

The delays are configurable (`RETRY_DELAYS_SECONDS`) so tests and demos run in seconds, and tests
check the delays against database time instead of sleeping. There is no jitter: with a fixed
schedule, many jobs failing together would retry together, and jitter would spread them out.

---

## 5. One Thing I Would Do Differently With More Time

**Run each job in a separate process instead of a thread.** Today a handler that hangs without
yielding can't be interrupted. The worker relies on the heartbeat cutoff and then exits the whole
process, so the hung job keeps that worker busy for about 90 seconds (timeout + lease). A
supervisor that runs each handler in a child process could kill just that child at the timeout,
giving a real hard limit and isolating crashes. The cost is complexity: payload, result and
progress would cross a process boundary.

Other things skipped or simplified:
- **Fairness aging** for starving low-priority jobs.
- **Dead-letter queue.** `FAILED` jobs remain queryable instead.
- **Cleanup of expired idempotency keys.** Keys are kept as long as their jobs.
- **Metrics** (queue depth, claim latency) beyond `/health` counts and JSON logs.
- **Jittered backoff.**
- **Removing Redis.** PostgreSQL `LISTEN/NOTIFY` could replace it for wake-ups.

---

## Additional decisions

### Idempotency

`idempotency_keys(key PK, job_id, expires_at)`, kept for 24 hours.

**How a keyed submission works:** one transaction inserts the job, then upserts the key:

```sql
INSERT INTO idempotency_keys (key, job_id, expires_at) VALUES (:key, :new_job, now() + interval '24 hours')
ON CONFLICT (key) DO UPDATE SET job_id = EXCLUDED.job_id, expires_at = EXCLUDED.expires_at
WHERE idempotency_keys.expires_at <= now()
RETURNING job_id;
```

- **A row is returned:** the key was new, or it had expired and was taken over. The job is committed
  and the response is 201.
- **No row is returned:** another job holds the active key. The transaction rolls back, which
  discards the new job row, and the existing job is returned with 200.

PostgreSQL locks the conflicting key row, so concurrent submissions with the same key serialize, and
exactly one creates a job. This is tested with 10 threads behind a barrier, both for a new key and
for an expired one.

**Policy:**
- The same active key returns the original job even if the payload differs.
- An expired key creates a new job, and the old job is kept.
- A client that got no response (timeout, or a 503 during a DB outage) can safely retry with the
  same key.

### Scheduling

- `scheduled_at` in the future creates a `SCHEDULED` job.
- A time up to 30s in the past runs it now, which tolerates client clock skew. Anything older, or
  more than 365 days ahead, gets 422. Values are normalized to UTC.
- Before each claim, a worker moves due jobs from `SCHEDULED` to `PENDING`, with one bounded
  `SKIP LOCKED` UPDATE. There is no separate scheduler service.
- A job never starts early. It can start up to one poll interval (2s) late.

### Crash windows (at-least-once)

| Failure | Outcome |
|---|---|
| API fails before the job commit | Nothing persisted; client gets 503 or no response and retries (safe with an idempotency key) |
| API commits, then crashes before the Redis publish | Job is persisted; workers find it by polling within 2s |
| Job committed but the response is lost | Client retries with the same key and gets 200 with the original job |
| Worker crashes after claim, before or during the handler | Lease expires; recovery counts the attempt and retries (30s/120s) or fails it after attempt 3 |
| Worker crashes after an external side effect, before the completion commit | Job runs again, so **the side effect may repeat**. Real integrations need idempotent receivers. |
| Worker pauses past its lease, then resumes | Job is recovered and maybe reclaimed; the paused worker's writes are rejected (token and lease), but its in-flight external work may overlap |
| Completion races recovery at the lease boundary | Row lock plus the lease predicate: exactly one applies |
| PostgreSQL down | API returns 503 and commits nothing; workers log and retry every poll interval; a heartbeat failing for a whole lease makes the worker abandon the job |
| Redis down | `/health` reports `degraded`; submissions still succeed; jobs are processed by polling |
| Handler hangs | Cooperative timeout (60s) fails the attempt; a non-yielding hang exits the worker after timeout + lease, then recovery |

### Alternatives considered

- **Celery / RQ:** these would hide exactly the concurrency and recovery logic this assignment
  evaluates. Their delivery semantics (acks, visibility timeouts) are also harder to reason about
  together with a separate database of job state.
- **Redis lists or Streams as the queue:** consumer groups and `XAUTOCLAIM` provide claim and
  recovery, but every job would be written to both Redis and PostgreSQL, with no atomic commit
  across them.
- **Kafka / RabbitMQ:** too heavy for this scope. They have no native per-message priority with
  delayed retry and scheduling, and they add infrastructure the assignment doesn't need.
