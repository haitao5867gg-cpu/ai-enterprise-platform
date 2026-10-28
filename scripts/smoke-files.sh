#!/usr/bin/env bash
# Synthetic upload acceptance against the running Compose stack.
set -euo pipefail
upload_tmp=$(mktemp -d)
trap 'rm -rf "$upload_tmp"' EXIT
printf 'id,status,amount\n1,paid,12.5\n2,open,20\n' > "$upload_tmp/orders.csv"
curl -fsS http://localhost:8000/datasources/files \
  -F "name=文件上传验收-$(date +%s)-$$" -F "files=@$upload_tmp/orders.csv" > "$upload_tmp/result.json"
dataset_id=$(python3 - "$upload_tmp/result.json" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    body = json.load(stream)
assert body['datasource']['kind'] == 'file'
assert body['tables'][0]['table'] == 'orders'
assert body['tables'][0]['row_count'] == 2
assert len(body['tables'][0]['columns']) == 3
print(body['dataset_id'])
PY
)
curl -fsS http://localhost:8000/datasources > "$upload_tmp/sources.json"
python3 - "$upload_tmp/result.json" "$upload_tmp/sources.json" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    uploaded = json.load(stream)['datasource']
with open(sys.argv[2]) as stream:
    assert uploaded in json.load(stream)
print('file datasource registered')
PY
docker compose exec -T connector-files aep-files-smoke \
  --url "http://127.0.0.1:8003/datasets/$dataset_id/mcp"
# The OS mount is read-only in addition to DuckDB's locked read-only sessions.
docker compose exec -T connector-files python -c \
  "from pathlib import Path; from aep_files.config import Settings; p=Settings().uploads_dir/'$dataset_id'/'write-probe';
try: p.write_text('must fail')
except OSError: print('uploads mount is read-only')
else: p.unlink(); raise AssertionError('uploads mount is writable')"
