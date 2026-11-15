# 装运行包（单机）

运行包是同一个镜像设 `AEP_MODE=runtime`：只装平台签过名的流程包，外部应用经 `/v1` 和
`/mcp/apps` 调用，运行记录、素材、生成的文件都留在这台机器上。接口契约见
[docs/api/runtime.md](../../docs/api/runtime.md)。

这里的文件把运行包装在外部应用自己的一台 Linux 服务器上：

| 服务 | 说明 |
| --- | --- |
| `caddy` | 对外唯一入口，自动 HTTPS，只转发 `/v1/*`、`/mcp/apps/*`，其余 404 |
| `api` | 运行模式的后端，管理接口只绑本机 `127.0.0.1:8000` |
| `litellm` | 外部应用自己的模型网关，key 在本机 `.env` |
| `postgres`、`redis` | 运行包自己的数据库，和平台的分开 |
| `connector-sandbox`、`sandbox-relay` | 代码节点的沙箱，不能上网 |
| `connector-media` | 媒体处理（ffmpeg），读媒体库、渲染视频；流程用到时绑定 `http://connector-media:8008/mcp` |
| `asr`（可选） | 本地语音转写（faster-whisper），模型网关的 `transcribe` 别名默认指向它。`.env` 里写 `COMPOSE_PROFILES=asr` 才启动，第一次下载模型约 500 MB，常驻内存约 1G（[docs/api/transcription.md](../../docs/api/transcription.md)） |

没有平台页面、示例源库和 Langfuse。外部应用自己的服务（页面、连接器）写在
`infra/runtime/compose.app.yml`，它自己的对外路由写在 `infra/runtime/sites/*.caddy`。这两样都不进仓库，`install.sh` 发现了就一起启动。

## 配置要求

以流程包运行要求清单里的「机器」为准。没有特别要求时：2 核、4G 内存（加 4G 交换区）、
40G 磁盘，Debian 12 或 Ubuntu 24.04，能访问模型网关、Docker Hub、GitHub、PyPI。空闲时整套服务约占 1.1G 内存（模型网关 730M、API 220M）。

## 步骤

### 1. 初始化服务器

和平台的 VPS 一样，见 [infra/vps/README.md](../vps/README.md) 第 1、2 步：放 SSH 公钥，
以 root 运行 `infra/vps/setup-server.sh`。它会建部署用户 `aep`、关掉密码登录、防火墙只开
22/80/443、装 Docker。

### 2. 以 `aep` 拉代码并安装

```bash
ssh aep@<IP>
git clone https://github.com/<owner>/<repo>.git aep && cd aep
bash infra/runtime/install.sh
```

第一次运行会问三件事，答案只写进 `infra/runtime/.env`（权限 600）：

1. **域名**：直接回车用 `<IP 用横线>.sslip.io`。
2. **平台公钥**：在平台上用搭建者账号打开 `GET /packages/public-key`，复制 `public_key`。
3. **模型网关**：地址、密钥（输入时不显示）和模型 id。流程用到的别名都要在
   `infra/litellm/config.yaml` 里有。

然后它会逐个构建镜像（第一次 15–30 分钟）并启动，最后打印每个服务的状态和 `/readyz`。
以后更新时先 `git pull`，再运行同一条命令。

### 3. 装流程包、绑定、自检

在平台上下载流程包（`GET /apps/{app}/versions/{version}/package`），拷到服务器上：

```bash
bash infra/runtime/aep-runtime.sh install coach-video-v3.aep-package.json
bash infra/runtime/aep-runtime.sh check coach-video
```

自检不通过的项按提示补，补完再查一次：

- **数据源**：外部应用自己的连接器先写进 `.env` 的 `AEP_CONNECTOR_HOSTS`，例如
  `["connector-stock:8010"]`，再重新运行 `install.sh`。然后绑定：
  `aep-runtime.sh bind <应用> <datasource_id> http://connector-stock:8010/mcp`
- **密钥**：`aep-runtime.sh secret <应用> <密钥名>`，输入时不显示。
- **模型**：别名不在网关上时，改 `infra/litellm/config.yaml` 或 `.env`，再运行 `install.sh`。
  `aep-runtime.sh check <应用> --fresh` 会真的调一次每个别名。

自检全部通过后，`/v1` 才接受运行。

### 4. 给外部应用建 API 密钥

```bash
bash infra/runtime/aep-runtime.sh key coach-video "教练页面"
```

`secret` 只显示这一次，存到外部应用的服务器上。外部应用用它调
`https://<域名>/v1/...`（[docs/api/app-access.md](../../docs/api/app-access.md)）。

## 管理接口

装包、绑定、自检、填密钥、建 API 密钥都只在本机 `127.0.0.1:8000`，不经 Caddy 对外。
`aep-runtime.sh` 就是调它们。在自己电脑上看的话，先开一个 SSH 隧道：
`ssh -L 8000:127.0.0.1:8000 aep@<IP>`。
