#!/usr/bin/env bash
# End-to-end check of the hello app on a running stack (docker compose up):
# start a run, restart the API while it waits for approval, approve, expect done.
set -euo pipefail
API=${API:-http://localhost:8000}

status() { curl -sf "$API/runs/$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'; }

wait_for() {  # run_id expected_status
  for _ in $(seq 1 60); do
    [ "$(status "$1")" = "$2" ] && return 0
    sleep 1
  done
  echo "run $1 never reached $2 (last: $(status "$1"))" >&2
  curl -s "$API/runs/$1" >&2
  return 1
}

run_id=$(curl -sf "$API/runs" -H 'content-type: application/json' \
  -d '{"app":"hello","input":{"text":"你好"}}' |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "run $run_id"
wait_for "$run_id" waiting

docker compose restart api
for _ in $(seq 1 60); do curl -sf "$API/healthz" >/dev/null && break; sleep 1; done
[ "$(status "$run_id")" = waiting ]
echo "still waiting after restart"

curl -sf "$API/runs/$run_id/resume" -H 'content-type: application/json' -d '{"decision":"approve"}' >/dev/null
wait_for "$run_id" done
curl -sf "$API/runs/$run_id"
echo
