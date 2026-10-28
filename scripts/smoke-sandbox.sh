#!/usr/bin/env bash
# Code node acceptance against the running Compose stack: pandas works in the
# sandbox, while the internet, the platform's database and the image stay out of reach.
set -euo pipefail
app="sandbox-smoke-$(date +%s)-$$"
work=$(mktemp -d)
trap 'curl -fsS -X DELETE "http://localhost:8000/apps/$app" >/dev/null || true; rm -rf "$work"' EXIT

curl -fsS http://localhost:8000/apps -H 'content-type: application/json' \
  -d "{\"app\": \"$app\", \"title\": \"沙箱验收\"}" >/dev/null

python3 > "$work/request.json" <<'PY'
import json

code = '''
import os
import socket

import pandas as pd


def main(inputs):
    df = pd.DataFrame(inputs["rows"])
    blocked = {}
    for host, port in [("pypi.org", 443), ("1.1.1.1", 443), ("postgres", 5432), ("api", 8000), ("litellm", 4000)]:
        try:
            socket.create_connection((host, port), timeout=3).close()
            blocked[f"{host}:{port}"] = False
        except OSError:
            blocked[f"{host}:{port}"] = True
    try:
        open("/app/planted.py", "w")
        blocked["write /app"] = False
    except OSError:
        blocked["write /app"] = True
    blocked["no AEP_ settings"] = not [k for k in os.environ if k.startswith("AEP_")]
    return {"total": float(df["amount"].sum()), "blocked": blocked}
'''
calc = {
    "id": "calc",
    "type": "code",
    "title": "沙箱验收",
    "code": code,
    "inputs": {"rows": "{{start.rows}}"},
    "outputs": [{"key": "total", "type": "number"}, {"key": "blocked", "type": "object"}],
    "timeout_seconds": 20,
}
config = {
    "graph": {
        "nodes": [
            {"id": "start", "type": "start", "title": "开始",
             "inputs": [{"key": "rows", "label": "数据", "type": "textarea"}]},
            calc,
            {"id": "end", "type": "end", "title": "结束",
             "outputs": [{"key": "total", "from": ["{{calc.total}}"]}]},
        ],
        "edges": [{"from": "start", "to": "calc"}, {"from": "calc", "to": "end"}],
    }
}
rows = [{"amount": 12.5}, {"amount": 20}]
print(json.dumps({"config": config, "values": {"start.rows": rows}}))
PY

curl -fsS "http://localhost:8000/apps/$app/nodes/calc/test" -H 'content-type: application/json' \
  -d @"$work/request.json" > "$work/result.json"
python3 - "$work/result.json" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    body = json.load(stream)
print(json.dumps(body, ensure_ascii=False))
assert body["status"] == "done", body["error"]
assert body["outputs"]["total"] == 32.5
reached = [k for k, blocked in body["outputs"]["blocked"].items() if not blocked]
assert not reached, f"the sandbox reached: {reached}"
print("code node ran in the sandbox; network, database and image are out of reach")
PY
