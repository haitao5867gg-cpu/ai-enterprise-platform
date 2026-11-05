# 部署到单台 VPS

在上 Azure（AKS）之前，把整套平台跑在一台 Linux 服务器上给少数人试用。用的是和本地
`make up` 同一份 `docker-compose.yml` 和同一套镜像，只叠加一层 `infra/vps/compose.yml`：

- 对外只开 Caddy 的 80/443。Caddy 自动申请 HTTPS 证书，整站要求访问口令（平台本身还没有登录），
  再反代到 web；web 经 `/api/*` 转发到后端。数据库、Redis、模型网关、API 端口都只绑本机。
- 面向 4G 内存：MySQL 示例库关掉 performance_schema 并缩小缓冲池，代码沙箱限 768MB；
  不启动 Langfuse（它要 ClickHouse 等 6 个容器，4G 放不下），追踪关闭。
- 所有服务随机器重启自动拉起，容器日志每个最多 30MB。

## 配置要求

| | 最低 | 说明 |
|---|---|---|
| 内存 | 4G + 4G 交换区 | 空闲约 2–2.6G；跑任务、部署到 Databricks 时再多几百 MB；构建镜像最吃内存，靠交换区 |
| 磁盘 | 25G | 镜像约 8–10G，加数据库和上传文件 |
| 系统 | Debian 12 | 其他带 systemd 的 Debian/Ubuntu 也行，`setup-server.sh` 只在 Debian 12 上验证过 |
| 出网 | 模型网关、Databricks 工作区、Docker Hub、GitHub、PyPI、npm | 平台要直接访问它们 |

## 步骤

### 1. 在自己电脑上：把 SSH 公钥放上去

```bash
ssh-keygen -t ed25519            # 已有 ~/.ssh/id_ed25519.pub 就跳过
ssh-copy-id root@<VPS 的 IP>     # 最后一次输入 root 密码
ssh root@<VPS 的 IP>             # 确认不用密码就能登录
```

### 2. 在 VPS 上以 root：初始化服务器

```bash
curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/infra/vps/setup-server.sh -o setup-server.sh
bash setup-server.sh
```

仓库是私有的话，先把这个文件 `scp` 上去再运行。脚本会：建部署用户 `aep`（沿用 root 的公钥）、
关闭密码登录、防火墙只开 22/80/443、加 4G 交换区、装 Docker、开 fail2ban 和自动安全更新。

### 3. 以 `aep` 登录：拉代码并部署

```bash
ssh aep@<VPS 的 IP>
git clone https://github.com/<owner>/<repo>.git aep && cd aep   # 私有仓库用只读的 deploy key 或 fine-grained token
bash infra/vps/deploy.sh
```

第一次运行会依次问：

1. **域名**。有自己的域名就先把 A 记录指向 VPS 的 IP 再填；没有就回车，用 `<IP 用横线>.sslip.io`
   （免费的通配 DNS，证书照样能申请）。
2. **模型网关**地址、密钥（输入不显示）和模型 id。回车跳过则只能用 `mock` 模型。
3. **网站访问口令**（用户名和至少 12 位的口令），浏览器打开时要输入。

然后逐个构建镜像（第一次 15–30 分钟）、启动、打印每个容器的内存占用。完成后打开脚本最后给出的
`https://...` 地址。

密钥只写进服务器上的 `.env`（权限 600），不显示、不进仓库。

别的模型（比如 Claude）不用改 `.env`：部署好以后在平台的「模型」页面新增，key 粘贴在页面上，
由模型网关加密存进它自己的库（`docs/api/models.md`）。加密用的 `LITELLM_SALT_KEY` 由脚本第一次
运行时生成，**以后不要改**，改了已经存的 key 就解不开了，只能在页面上重新填。

### 4. 可选：Databricks、资产仓库、示例数据

在服务器上编辑 `.env`（变量说明见 `.env.example` 的 Databricks 和 Assets repository 两节），
例如 `AEP_DATABRICKS_HOST`、`AEP_DATABRICKS_TOKEN`、`AEP_DATABRICKS_CLUSTER_ID`、
`AEP_DATABRICKS_CATALOG`、`AEP_DATABRICKS_SCHEMA`，保存后再运行一次 `bash infra/vps/deploy.sh`。

要读 Databricks 上已有的表（数据源类型 `databricks`，连接器地址 `http://connector-databricks:8005/mcp`），
还要写 `AEP_DATABRICKS_READ_CATALOGS`（允许读的 catalog，JSON 列表，比如 `["icsd-ace-dev"]`）。
连接器默认借用上面的地址、令牌和集群。没人值守时集群闲置一段时间会自动停掉，可以设
`AEP_DATABRICKS_READ_START_CLUSTER=true` 让连接器替你启动它，同时设
`AEP_DATABRICKS_READ_TIMEOUT_SECONDS=600` 和 `AEP_DATABRICKS_TOOL_SECONDS=620`，
因为启动要几分钟（见 `docs/api/datasources.md` 的“Databricks 数据源”）。

演示数据（MySQL 示例库 retail、两个演示知识库和应用 retail-etl）在服务器上装好 uv 后用
`make demo` 导入，它走本机的 8000 端口，不经过访问口令：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.local/bin/env
make demo WEB=https://<域名>
```

## 日常操作

在仓库目录下：

```bash
DC="docker compose -f docker-compose.yml -f infra/vps/compose.yml"
git pull && bash infra/vps/deploy.sh     # 更新版本
bash infra/vps/deploy.sh --password      # 换访问口令
$DC ps                                   # 状态
$DC logs -f api                          # 看日志
docker stats --no-stream                 # 内存
$DC exec -T postgres pg_dump -U aep aep | gzip > ~/aep-$(date +%F).sql.gz   # 备份平台数据库
$DC down                                 # 停止（数据卷保留）
```

更新版本后，可以用平台的默认模型把智能体应用和对话验收一遍（大约 2–3 分钟，会调真实模型）：

```bash
python3 infra/vps/accept-agents.py ~/accept-agents.md
```

它直接调本机的 API（`127.0.0.1:8000`），新建几个名字以「验收·」开头的应用，检查多轮对话、智能体调用应用、存为评测用例和评测、入口开关、使用情况、编排应用打开对话、转成编排应用、只依据资料回答，全部通过时退出码为 0，报告写进给的文件。智能体应用、它调用的应用和那次会话留着，可以在页面上看；其余的归档。

## 安全和合规

- 访问口令（Caddy）是第一道门，口令不要发在聊天里。平台自己的登录默认关着，这时所有人共用口令，
  没有个人审计。要按人分角色、记录谁做了什么，在服务器 `.env` 里加 `AEP_AUTH_ENABLED=true` 和
  `AEP_COOKIE_SECURE=true`，再运行一次 `bash infra/vps/deploy.sh`，打开网页建第一个管理员，
  然后在「账号」页给每个人建账号（见 `docs/api/auth.md`）。打开之前，先给还在用这个站点的人
  （包括别的 AI 测试）建好账号。部署脚本不会替你打开。以后接 Entra ID 登录。
- 例外：应用 API（`/api/v1/*`）和应用的 MCP 入口（`/api/mcp/*`）不要求访问口令，给别的系统
  调用；它们只认应用的 API 密钥（搭建者在应用页「接入」里新建，见 `docs/api/app-access.md`），
  没有密钥一律 401。不用的密钥在应用页删掉，立即失效。
- 服务器上的 `.env` 有模型网关密钥、Databricks token 等，服务器被攻破就等于这些密钥泄露。
  Databricks 建议用只读或只有 dev schema 权限的 token。
- 发给模型和从数据源读出的数据都会经过这台服务器。公司数据能否放在境外的个人服务器上，按公司规定办；
  拿不准就只用示例库和上传的脱敏文件，并把数据外发策略设成 `masked` 或 `stats`
  （`.env` 里的 `AEP_DATA_SAMPLES`）。
- 代码节点的 Python 在沙箱容器里运行，沙箱没有外网，这一点和本地一致。
