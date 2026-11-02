# Git 连接器

独立的 Python 3.12 / uv 包，用官方 MCP Python SDK 提供 Streamable HTTP：`http://127.0.0.1:8006/mcp`（本地编排里是 `http://connector-git:8006/mcp`）。不依赖平台后端或模型网关。接口契约见 [docs/api/git.md](../../docs/api/git.md)。

## 能做什么

- 支持 GitHub（含 GitHub Enterprise Server）和 Bitbucket Data Center，一个连接器对应一个服务、一个令牌，可以允许多个仓库（`AEP_GIT_REPOS`）。
- 读：`list_repos`、`list_files`、`read_file`、`get_pull_request`。
- 提议改动：`open_pull_request` 在默认分支（或指定的 `base`）上做一个提交，推到 `aep/` 开头的分支，开 PR 或更新已有的 PR；`comment_pull_request` 给 PR 加评论。
- 只往 `aep/` 开头的分支推送，别的分支一律不动。合不合并由人决定。

提交用 git 本身（镜像里装了 git），PR 用各家的 REST API。令牌经环境变量交给 git，不出现在命令行、远端地址和克隆的配置里。

## 设置

见契约的「设置」一节。最少要配 `AEP_GIT_TOKEN` 和 `AEP_GIT_REPOS`；Bitbucket 还要 `AEP_GIT_PROVIDER=bitbucket` 和 `AEP_GIT_API_URL`。本地编排没配时沿用资产仓库的 `AEP_ASSETS_GITHUB_TOKEN` 和 `AEP_ASSETS_REPO`。

令牌权限：

- GitHub：细粒度令牌，只选这几个仓库，Contents 读写、Pull requests 读写。
- Bitbucket Data Center：仓库或项目的 HTTP access token，权限选“仓库写”。

## 健康检查

- `GET /healthz`：不访问 Git 服务，总是 200，说明配置齐不齐（`configured`、`missing`）。
- `GET /healthz?probe=1`：逐个读允许的仓库，通了返回 200，否则返回 503 并说明原因。

## 测试

```bash
cd connectors/git
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

测试用磁盘上的裸仓库（`file://` 克隆地址）真的提交和推送，PR 用假的服务；两家的 REST 调用用假的端点核对。不连真实的 GitHub 或 Bitbucket。
