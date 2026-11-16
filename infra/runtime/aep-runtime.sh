#!/usr/bin/env bash
# 运行包的管理命令（docs/api/runtime.md「运行包这边的接口」）。在运行包这台机器上运行，
# 只连本机 127.0.0.1:8000，管理接口不对外。
#   bash infra/runtime/aep-runtime.sh install 包文件.aep-package.json
#   bash infra/runtime/aep-runtime.sh apps
#   bash infra/runtime/aep-runtime.sh check <应用> [--fresh]
#   bash infra/runtime/aep-runtime.sh bind <应用> <datasource_id> <连接器地址>
#   bash infra/runtime/aep-runtime.sh activate <应用> <版本>
#   bash infra/runtime/aep-runtime.sh secret <应用> <密钥名>      输入时不显示
#   bash infra/runtime/aep-runtime.sh key <应用> <名字>           建 API 密钥，只显示这一次
#   bash infra/runtime/aep-runtime.sh libraries                   看媒体库和它们的 id
#   bash infra/runtime/aep-runtime.sh describe <库 id> <看图模型> [提示]
#                                         打开自动写画面描述；模型写 off 关掉
set -euo pipefail

API=${AEP_RUNTIME_API:-http://127.0.0.1:8000}

show() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }

# 打印响应体；不是 2xx 时返回 1。
call() {
  local method=$1 path=$2
  shift 2
  local out code
  out=$(mktemp)
  code=$(curl -sS -o "$out" -w '%{http_code}' -X "$method" "$API$path" "$@") || {
    rm -f "$out"
    echo "✗ 连不上 $API，运行包启动了吗？（docker compose -f infra/runtime/compose.yml ps）" >&2
    return 1
  }
  show < "$out"
  rm -f "$out"
  [ "${code:0:1}" = 2 ] || { echo "✗ HTTP $code" >&2; return 1; }
}

json() { python3 -c 'import json, sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2])), ensure_ascii=False))' "$@"; }

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

cmd=${1:-}
[ -n "$cmd" ] || usage
shift
case "$cmd" in
  install)
    [ $# -eq 1 ] && [ -f "$1" ] || usage
    call POST /runtime/packages -H 'Content-Type: application/json' --data-binary "@$1"
    ;;
  apps)
    call GET /runtime/apps
    ;;
  check)
    [ $# -ge 1 ] || usage
    if [ "${2:-}" = --fresh ]; then
      call GET "/runtime/apps/$1/check?fresh=true"
    else
      call GET "/runtime/apps/$1/check"
    fi
    ;;
  bind)
    [ $# -eq 3 ] || usage
    call PUT "/runtime/apps/$1/bindings/$2" -H 'Content-Type: application/json' \
      --data-binary "$(json connector_url "$3")"
    ;;
  activate)
    [ $# -eq 2 ] || usage
    call POST "/runtime/apps/$1/activate" -H 'Content-Type: application/json' \
      --data-binary "{\"version\": $2}"
    ;;
  secret)
    [ $# -eq 2 ] || usage
    read -r -s -p "密钥 $2 的值（输入时不显示）: " value
    echo
    # 只经标准输入传给 python3 和 curl（printf 是内建命令），不出现在任何进程的参数里。
    printf '%s' "$value" \
      | python3 -c 'import json, sys; print(json.dumps({"value": sys.stdin.read()}))' \
      | call PUT "/apps/$1/secrets/$2" -H 'Content-Type: application/json' --data-binary @-
    unset value
    echo "✓ 已保存"
    ;;
  key)
    [ $# -eq 2 ] || usage
    echo "下面的 secret 只显示这一次，存到外部应用的服务器上，不要发在聊天里。" >&2
    call POST "/apps/$1/keys" -H 'Content-Type: application/json' \
      --data-binary "$(json name "$2")"
    ;;
  libraries)
    call GET /media-libraries
    ;;
  describe)
    [ $# -ge 2 ] && [ $# -le 3 ] || usage
    if [ "$2" = off ]; then
      body='{"describe": null}'
    else
      body=$(python3 -c '
import json, sys
describe = {"model": sys.argv[1], **({"hint": sys.argv[2]} if sys.argv[2:] else {})}
print(json.dumps({"describe": describe}, ensure_ascii=False))' "${@:2}")
    fi
    call PATCH "/media-libraries/$1" -H 'Content-Type: application/json' --data-binary "$body"
    ;;
  *)
    usage
    ;;
esac
