#!/usr/bin/env bash
# 在仓库根目录以部署用户运行（infra/vps/README.md 第 3 步）：
#   bash infra/vps/deploy.sh          首次：问域名、访问口令、模型网关密钥，然后构建并启动
#   bash infra/vps/deploy.sh          以后：git pull 之后再运行，重新构建并启动
#   bash infra/vps/deploy.sh --password   换访问口令
# 密钥只写进本机 .env（权限 600），不会显示在屏幕上，也不进仓库。
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
AUTH=infra/vps/auth.caddy
DC=(docker compose -f docker-compose.yml -f infra/vps/compose.yml)

docker compose version >/dev/null 2>&1 || { echo "✗ 没有 docker compose，先运行 setup-server.sh"; exit 1; }

# .env 里设置 KEY=VALUE：替换已有的（包括注释掉的）第一行，没有就追加。
set_env() {
  V="$2" awk -v k="$1" '
    BEGIN { v = ENVIRON["V"]; done = 0 }
    !done && $0 ~ "^#? *" k "=" { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' .env > .env.tmp && cat .env.tmp > .env && rm -f .env.tmp
}
get_env() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
ask() { local v; read -r -p "$1" v; echo "$v"; }
ask_secret() { local v; read -r -s -p "$1" v; echo >&2; echo "$v"; }

if [ ! -f .env ]; then
  echo "== 首次部署：生成 .env"
  cp .env.example .env
  chmod 600 .env
  set_env AEP_ENV vps
  set_env LITELLM_MASTER_KEY "sk-$(openssl rand -hex 24)"
  # 示例源库的口令，只在第一次建库时生效。
  set_env AEP_SOURCE_MYSQL_ADMIN_PASSWORD "$(openssl rand -hex 16)"
  set_env AEP_MYSQL_PASSWORD "$(openssl rand -hex 16)"
  set_env AEP_SOURCE_POSTGRES_ADMIN_PASSWORD "$(openssl rand -hex 16)"
  set_env AEP_POSTGRES_PASSWORD "$(openssl rand -hex 16)"
  set_env AEP_LANGFUSE_UI_URL ""
fi
chmod 600 .env

if [ -z "$(get_env AEP_DOMAIN)" ]; then
  ip=$(curl -fsS -4 https://api.ipify.org || true)
  default=${ip:+${ip//./-}.sslip.io}
  echo "访问地址用的域名：有自己的域名就填（先把 A 记录指向本机 IP），"
  echo "没有就直接回车用 ${default:-<IP 用横线连接>.sslip.io}。"
  domain=$(ask "域名 [${default}]: ")
  set_env AEP_DOMAIN "${domain:-$default}"
fi

if [ -z "$(get_env COMPANY_API_KEY)" ]; then
  echo "== 模型网关（别名 company，OpenAI 兼容）。直接回车跳过，平台只能用 mock 模型。"
  base=$(ask "网关地址（以 /v1 结尾）: ")
  if [ -n "$base" ]; then
    set_env COMPANY_API_BASE "$base"
    set_env COMPANY_API_KEY "$(ask_secret "网关密钥（输入时不显示）: ")"
    model=$(ask "模型 id [deepseek-v4.1-flash-new]: ")
    set_env COMPANY_MODEL "openai/${model:-deepseek-v4.1-flash-new}"
    set_env AEP_DEFAULT_MODEL company
  fi
fi

if [ "${1:-}" = "--password" ] || [ ! -s "$AUTH" ]; then
  echo "== 网站访问口令（浏览器打开时要输入）"
  user=$(ask "用户名 [aep]: ")
  pass=$(ask_secret "口令（至少 12 位，输入时不显示）: ")
  [ "${#pass}" -ge 12 ] || { echo "✗ 口令太短"; exit 1; }
  # 口令经环境变量传进容器，不出现在本机的命令行里。
  hash=$(P="$pass" docker run --rm -e P caddy:2.10-alpine sh -c 'caddy hash-password --plaintext "$P"')
  umask 077
  printf 'basic_auth {\n\t%s %s\n}\n' "${user:-aep}" "$hash" > "$AUTH"
  unset pass
fi

echo "== 构建镜像（逐个构建，避免 4G 内存不够）"
"${DC[@]}" config --services | while read -r s; do
  COMPOSE_BAKE=false "${DC[@]}" build "$s" < /dev/null
done

echo "== 启动"
"${DC[@]}" up -d --remove-orphans --wait --wait-timeout 900
docker image prune -f >/dev/null

echo
"${DC[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
echo
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}'
echo
free -h
echo
echo "✓ 打开 https://$(get_env AEP_DOMAIN)（第一次申请证书可能要等半分钟）"
