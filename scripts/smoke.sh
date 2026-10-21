#!/usr/bin/env bash
# Manual end-to-end demo against the real Compose stack (not part of the automated test suite).
#
# Usage (from the repository root, on a fresh stack):
#   docker compose up -d --build --wait
#   scripts/smoke.sh
#   docker compose down -v        # optional cleanup
#
# Each check prints PASS/FAIL; the script exits non-zero if any check failed. It stops and
# starts services (worker, redis) and takes about 3 minutes because the crash-recovery step
# waits for a real 30s lease and a 30s retry delay.
set -uo pipefail

API=${API:-http://localhost:8000}
FAILURES=0

check() {  # check <description> <expected> <actual>
  if [[ "$3" == "$2" ]]; then echo "  PASS  $1: $3"; else echo "  FAIL  $1: expected '$2', got '$3'"; FAILURES=$((FAILURES + 1)); fi
}
post() { curl -s -X POST "$API/jobs" -H 'content-type: application/json' -d "$1"; }
submit() { post "$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'; }
field() { curl -s "$API/jobs/$1" | python3 -c "import json,sys; print(json.load(sys.stdin)['$2'])"; }
http_code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
sql() { docker compose exec -T postgres psql -U jobqueue -d jobqueue -tAc "$1"; }
wait_status() {  # wait_status <job_id> <status> <timeout_seconds>
  local deadline=$((SECONDS + $3))
  while ((SECONDS < deadline)); do [[ "$(field "$1" status)" == "$2" ]] && break; sleep 0.5; done
  field "$1" status
}
at() { python3 -c "import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(seconds=$1)).isoformat())"; }
EMAIL='{"type":"email","payload":{"to":"user@example.com","subject":"Hi"}'
one_off_worker() {  # one_off_worker <name> <env...>: a temporary extra worker with overrides
  local name=$1; shift
  docker compose run -d --rm --name "$name" "${@/#/-e}" worker >/dev/null 2>&1
}

echo "== 0. stack is up"
check "GET /health" "200" "$(http_code "$API/health")"

echo "== 1. every job type completes"
for body in "$EMAIL}" '{"type":"report","payload":{"report_name":"monthly"}}' \
            '{"type":"batch","payload":{"items":[1,2,3,4,5]}}'; do
  id=$(submit "$body"); check "$(field "$id" type) job" "COMPLETED" "$(wait_status "$id" COMPLETED 30)"
done

echo "== 2. idempotency: same key returns the original job"
first=$(post "$EMAIL,\"idempotency_key\":\"smoke-key\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
check "repeat submit status" "200" "$(http_code -X POST "$API/jobs" -H 'content-type: application/json' -d "$EMAIL,\"idempotency_key\":\"smoke-key\"}")"
second=$(post "$EMAIL,\"idempotency_key\":\"smoke-key\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
check "same job id" "$first" "$second"

docker compose stop worker >/dev/null 2>&1
echo "== 3. cancel a pending job (no worker running)"
id=$(submit "$EMAIL}")
check "cancel" "200" "$(http_code -X POST "$API/jobs/$id/cancel")"
check "cancel again" "200" "$(http_code -X POST "$API/jobs/$id/cancel")"
CANCELLED=$id

echo "== 4. failure -> automatic retries -> FAILED -> manual retry (one-off workers, delays 1s/2s)"
id=$(submit '{"type":"webhook","payload":{"url":"https://example.com/hook"}}')
one_off_worker smoke-failing WEBHOOK_FAILURE_RATE=1 RETRY_DELAYS_SECONDS=1,2
check "after 3 attempts" "FAILED" "$(wait_status "$id" FAILED 40)"
check "attempts" "3" "$(field "$id" attempts)"
docker stop smoke-failing >/dev/null 2>&1
check "manual retry" "200" "$(http_code -X POST "$API/jobs/$id/retry")"
check "attempts reset" "0" "$(field "$id" attempts)"
one_off_worker smoke-healthy WEBHOOK_FAILURE_RATE=0
check "retried job" "COMPLETED" "$(wait_status "$id" COMPLETED 30)"
check "retry a COMPLETED job" "409" "$(http_code -X POST "$API/jobs/$id/retry")"
docker stop smoke-healthy >/dev/null 2>&1
check "cancelled job untouched by workers" "CANCELLED" "$(field "$CANCELLED" status)"

echo "== 5. priority: high (9) submitted after low (1) still starts first"
low=$(submit '{"type":"email","payload":{"to":"user@example.com","subject":"low"},"priority":1}')
high=$(submit '{"type":"email","payload":{"to":"user@example.com","subject":"high"},"priority":9}')
docker compose start worker >/dev/null 2>&1
wait_status "$low" COMPLETED 30 >/dev/null
check "high started before low" "True" "$(python3 -c "print('$(field "$high" started_at)' < '$(field "$low" started_at)')")"

echo "== 6. scheduled job never starts early"
id=$(submit "$EMAIL,\"scheduled_at\":\"$(at 6)\"}")
sleep 2; check "2s before due" "SCHEDULED" "$(field "$id" status)"
check "after due" "COMPLETED" "$(wait_status "$id" COMPLETED 20)"
check "started at or after scheduled_at" "True" "$(python3 -c "print('$(field "$id" started_at)' >= '$(field "$id" scheduled_at)')")"

echo "== 7. Redis down: health degraded, jobs still processed by polling"
docker compose stop redis >/dev/null 2>&1
check "health status" "degraded" "$(curl -s "$API/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
id=$(submit "$EMAIL}")
check "job without Redis" "COMPLETED" "$(wait_status "$id" COMPLETED 30)"
docker compose start redis >/dev/null 2>&1

echo "== 8. SIGTERM drains the running job"
id=$(submit '{"type":"batch","payload":{"items":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]}}')
until [[ "$(field "$id" status)" == "PROCESSING" ]]; do sleep 0.2; done
docker compose stop worker >/dev/null 2>&1
check "job after graceful stop" "COMPLETED" "$(field "$id" status)"
check "worker exit code" "0" "$(docker inspect -f '{{.State.ExitCode}}' "$(docker compose ps -aq worker)")"

echo "== 9. SIGKILL mid-job: lease expires, job is recovered and retried (~70s)"
docker compose start worker >/dev/null 2>&1
id=$(submit '{"type":"batch","payload":{"items":[1,2,3,4,5,6,7,8,9,10]}}')
until [[ "$(field "$id" status)" == "PROCESSING" ]]; do sleep 0.2; done
docker compose kill -s SIGKILL worker >/dev/null 2>&1
check "right after kill" "PROCESSING" "$(field "$id" status)"
docker compose start worker >/dev/null 2>&1
check "recovered job" "COMPLETED" "$(wait_status "$id" COMPLETED 120)"
check "attempts" "2" "$(field "$id" attempts)"
check "recovery logged" "1" "$(sql "SELECT count(*) FROM job_logs WHERE job_id='$id' AND event='lease_expired'")"

echo "== 10. three workers: no job claimed twice"
docker compose up -d --scale worker=3 --wait >/dev/null 2>&1
ids=$(for _ in $(seq 15); do submit "$EMAIL}"; done)
for id in $ids; do wait_status "$id" COMPLETED 60 >/dev/null; done
in_list="'$(echo $ids | sed "s/ /','/g")'"
check "completed" "15" "$(sql "SELECT count(*) FROM jobs WHERE id IN ($in_list) AND status='COMPLETED'")"
check "max claims per job" "1" "$(sql "SELECT max(c) FROM (SELECT count(*) c FROM job_logs WHERE job_id IN ($in_list) AND event='claimed' GROUP BY job_id) t")"
echo "  info  jobs per worker: $(sql "SELECT string_agg(worker_id || '=' || n, ', ') FROM (SELECT worker_id, count(*) n FROM jobs WHERE id IN ($in_list) GROUP BY worker_id) t")"
docker compose up -d --scale worker=1 >/dev/null 2>&1

echo
if ((FAILURES == 0)); then echo "ALL CHECKS PASSED"; else echo "$FAILURES CHECK(S) FAILED"; fi
exit $((FAILURES > 0))
