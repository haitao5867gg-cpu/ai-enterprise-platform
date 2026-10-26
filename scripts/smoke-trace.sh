#!/usr/bin/env bash
# End-to-end check of tracing on a stack started with `make up-full`: run hello
# through approval, then expect one Langfuse trace holding both segments of the
# run and the gateway's model call.
set -euo pipefail
API=${API:-http://localhost:8000}
LANGFUSE=${LANGFUSE:-http://localhost:3100}
AUTH=${LANGFUSE_AUTH:-pk-lf-local-dev:sk-lf-local-dev}

field() { python3 -c "import json,sys; print(json.load(sys.stdin)[\"$1\"])"; }

wait_for() {  # run_id expected_status
  for _ in $(seq 1 60); do
    [ "$(curl -sf "$API/runs/$1" | field status)" = "$2" ] && return 0
    sleep 1
  done
  echo "run $1 never reached $2" >&2
  curl -s "$API/runs/$1" >&2
  return 1
}

run_id=$(curl -sf "$API/runs" -H 'content-type: application/json' \
  -d '{"app":"hello","input":{"text":"你好"}}' | field run_id)
wait_for "$run_id" waiting
curl -sf "$API/runs/$run_id/resume" -H 'content-type: application/json' -d '{"decision":"approve"}' >/dev/null
wait_for "$run_id" done
trace_id=$(curl -sf "$API/runs/$run_id" | field trace_id)
echo "run $run_id → trace $trace_id"
[ "$trace_id" != None ] || { echo "API reports tracing off" >&2; exit 1; }

# Spans are batched by the SDKs and ingested asynchronously, so poll.
for _ in $(seq 1 90); do
  curl -sf -u "$AUTH" "$LANGFUSE/api/public/v2/observations?traceId=$trace_id&fields=core,basic,model,usage&limit=100" \
    > /tmp/observations.json || true
  if python3 - /tmp/observations.json <<'PY'
import json, sys
try:
    obs = json.load(open(sys.argv[1]))["data"]
except Exception:
    sys.exit(1)
by_name = {o.get("name"): o for o in obs}
gens = [o for o in obs if o["type"] == "GENERATION"]
ok = "start" in by_name and "resume" in by_name and gens
if ok:
    gen = gens[0]
    assert gen["parentObservationId"] == by_name["start"]["id"], gen
    for o in sorted(obs, key=lambda o: o["startTime"]):
        print(f'{o["type"]:<10} {o.get("name")!s:<12} model={o.get("model")} usage={o.get("usageDetails")}')
sys.exit(0 if ok else 1)
PY
  then
    echo "trace complete"
    exit 0
  fi
  sleep 2
done
echo "trace $trace_id incomplete in Langfuse:" >&2
cat /tmp/observations.json >&2
exit 1
