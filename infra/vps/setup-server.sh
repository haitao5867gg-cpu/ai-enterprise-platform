#!/usr/bin/env bash
# 新装的 Debian 12 上以 root 运行一次（infra/vps/README.md 第 2 步）：
#   建部署用户 aep（沿用 root 的 SSH 公钥）、关闭密码登录、防火墙只开 22/80/443、
#   加 4G 交换区、装 Docker 并限制容器日志大小、开启自动安全更新和 fail2ban。
# 前提：已经在自己电脑上 ssh-copy-id 过，root 能用密钥登录。可以重复运行。
set -euo pipefail
trap 'echo "✗ 第 $LINENO 行出错，脚本停止。把上面的输出贴出来；修好后可以直接重新运行。"' ERR

[ "$(id -u)" -eq 0 ] || { echo "✗ 请用 root 运行"; exit 1; }
. /etc/os-release
[ "${ID:-}" = debian ] || echo "! 只在 Debian 12 上验证过，当前是 ${PRETTY_NAME:-未知系统}"
if ! grep -qsE '^(ssh-|ecdsa-|sk-)' /root/.ssh/authorized_keys; then
  echo "✗ /root/.ssh/authorized_keys 里没有公钥。先在自己电脑上运行："
  echo "    ssh-copy-id root@<VPS 的 IP>"
  echo "  确认能免密登录后再运行本脚本，否则关掉密码登录后会进不去。"
  exit 1
fi

DEPLOY_USER=aep
SWAP_SIZE=${SWAP_SIZE:-4G}

echo "== 系统更新和基础软件"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl git make ufw fail2ban unattended-upgrades openssl sudo

echo "== 交换区 $SWAP_SIZE（4G 内存构建镜像时需要）"
# 有的 VPS（OpenVZ/LXC 容器）不允许自己加交换区或改内核参数：只提示，不中断。
if ! swapon --show | grep -q .; then
  if { fallocate -l "$SWAP_SIZE" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096; } \
    && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile; then
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  else
    rm -f /swapfile
    echo "! 加不了交换区（$(systemd-detect-virt 2>/dev/null || echo 未知虚拟化)），构建镜像时可能内存不够"
  fi
fi
echo 'vm.swappiness=20' > /etc/sysctl.d/90-aep.conf
sysctl -q -w vm.swappiness=20 || echo "! 改不了 vm.swappiness，跳过"

echo "== 部署用户 $DEPLOY_USER"
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /root/.ssh/authorized_keys \
  "/home/$DEPLOY_USER/.ssh/authorized_keys"
echo "$DEPLOY_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$DEPLOY_USER"
chmod 440 "/etc/sudoers.d/90-$DEPLOY_USER"

echo "== SSH：只允许密钥登录"
cat > /etc/ssh/sshd_config.d/10-aep.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
# 有的 VPS 镜像改过 sshd_config，去掉了读 sshd_config.d 的那一行。
grep -qE '^Include /etc/ssh/sshd_config.d/' /etc/ssh/sshd_config \
  || sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
sshd -t
systemctl reload ssh
sshd -T | grep -qi '^passwordauthentication no' || echo "! 密码登录没有关掉，请检查 /etc/ssh/sshd_config"

echo "== 防火墙：22、80、443"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

echo "== fail2ban 和自动安全更新"
systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== Docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
    "https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
    docker-compose-plugin
fi
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'CONF'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
CONF
systemctl restart docker
usermod -aG docker "$DEPLOY_USER"

echo
echo "✓ 完成。以后用 ssh $DEPLOY_USER@<IP> 登录（root 也只能用密钥）。"
echo "  下一步见 infra/vps/README.md 第 3 步：以 $DEPLOY_USER 身份拉代码并运行 deploy.sh。"
