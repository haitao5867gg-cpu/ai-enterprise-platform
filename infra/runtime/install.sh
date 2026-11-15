#!/usr/bin/env bash
# 装运行包（infra/runtime/README.md）。在仓库根目录以部署用户运行：
#   bash infra/runtime/install.sh     首次：问域名、平台公钥、模型网关，然后构建并启动
#   bash infra/runtime/install.sh     以后：git pull 之后再运行，重新构建并启动
# 口令和密钥只写进 infra/runtime/.env（权限 600），不显示在屏幕上，也不进仓库。
# 有 infra/runtime/compose.app.yml（外部应用自己的服务）时一起构建、启动。
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/infra/runtime"
ENV="$HERE/.env"
cd "$ROOT"
DC=(docker compose -f infra/runtime/compose.yml)
[ -f infra/runtime/compose.app.yml ] && DC+=(-f infra/runtime/compose.app.yml)

docker compose version >/dev/null 2>&1 || { echo "✗ 没有 docker compose，先运行 infra/vps/setup-server.sh"; exit 1; }

# .env 里设置 KEY=VALUE：替换已有的（包括注释掉的）第一行，没有就追加。
# 临时文件用 mktemp 建（权限 600），和 .env 一样别人读不到。
set_env() {
  local tmp rc=0
  tmp=$(mktemp "$ENV.XXXXXX")
  V="$2" awk -v k="$1" '
    BEGIN { v = ENVIRON["V"]; done = 0 }
    !done && $0 ~ "^#? *" k "=" { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$ENV" > "$tmp" && cat "$tmp" > "$ENV" || rc=$?
  rm -f "$tmp"
  return "$rc"
}
get_env() { grep -E "^$1=" "$ENV" | tail -1 | cut -d= -f2- || true; }
ask() { local v; read -r -p "$1" v; echo "$v"; }
ask_secret() { local v; read -r -s -p "$1" v; echo >&2; echo "$v"; }

if [ ! -f "$ENV" ]; then
  echo "== 首次安装：生成 infra/runtime/.env"
  (umask 077 && cp infra/runtime/env.example "$ENV")
fi
chmod 600 "$ENV"
for k in POSTGRES_PASSWORD LITELLM_MASTER_KEY LITELLM_SALT_KEY; do
  if [ -z "$(get_env "$k")" ]; then
    set_env "$k" "sk-$(openssl rand -hex 24)"
  fi
done

if [ -z "$(get_env AEP_DOMAIN)" ]; then
  ip=$(curl -fsS -4 https://api.ipify.org || true)
  default=${ip:+${ip//./-}.sslip.io}
  echo "对外的域名：有自己的域名就填（先把 A 记录指向本机 IP），"
  echo "没有就直接回车用 ${default:-<IP 用横线连接>.sslip.io}。"
  domain=$(ask "域名 [${default}]: ")
  set_env AEP_DOMAIN "${domain:-$default}"
fi

if [ "$(get_env AEP_PLATFORM_PUBLIC_KEYS)" = "[]" ] || [ -z "$(get_env AEP_PLATFORM_PUBLIC_KEYS)" ]; then
  echo "== 平台公钥：在平台上打开 GET /packages/public-key，复制 public_key 的值（一串 base64）。"
  echo "   直接回车跳过，之后装包会返回 503，补上后重新运行本脚本。"
  key=$(ask "平台公钥: ")
  if [ -n "$key" ]; then
    keys=$(python3 -c 'import json, sys; print(json.dumps([k.strip() for k in sys.argv[1].split(",") if k.strip()]))' "$key")
    set_env AEP_PLATFORM_PUBLIC_KEYS "$keys"
  fi
fi

if [ -z "$(get_env COMPANY_API_KEY)" ]; then
  echo "== 模型网关（别名 company，OpenAI 兼容）。直接回车跳过，只能用 mock 模型。"
  base=$(ask "网关地址（以 /v1 结尾）: ")
  if [ -n "$base" ]; then
    set_env COMPANY_API_BASE "$base"
    set_env COMPANY_API_KEY "$(ask_secret "网关密钥（输入时不显示）: ")"
    model=$(ask "模型 id [deepseek-v4.1-flash-new]: ")
    set_env COMPANY_MODEL "openai/${model:-deepseek-v4.1-flash-new}"
    set_env AEP_DEFAULT_MODEL company
  fi
fi

mkdir -p infra/runtime/sites

echo "== 构建镜像（逐个构建，省内存）"
"${DC[@]}" config --services | while read -r s; do
  COMPOSE_BAKE=false "${DC[@]}" build "$s" < /dev/null
done

echo "== 启动"
"${DC[@]}" up -d --remove-orphans --wait --wait-timeout 900
# Caddyfile 是挂载进去的，git pull 换成新文件后运行中的容器还看着旧的，要重启才能挂上。
# 主文件没变时也让 Caddy 重新读一遍配置：sites/*.caddy 可能加了或改了。
if [ "$("${DC[@]}" exec -T caddy cat /etc/caddy/Caddyfile | sha256sum)" != "$(sha256sum < infra/runtime/Caddyfile)" ]; then
  "${DC[@]}" restart caddy
else
  "${DC[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
fi
docker image prune -f >/dev/null

echo
"${DC[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
echo
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}'
echo
curl -fsS http://127.0.0.1:8000/readyz | python3 -m json.tool --no-ensure-ascii || true
echo
echo "✓ 运行包已启动：对外 https://$(get_env AEP_DOMAIN)/v1/（第一次申请证书可能要等半分钟）"
echo "  下一步装流程包：bash infra/runtime/aep-runtime.sh install <包文件>"
