# Git 连接器与提交 PR 节点

平台缺失功能计划第 9 项（第 5 批 5c）。代码仓库也走连接器（AGENTS.md「工具接入」）：Git 连接器是一个 MCP Server，支持 GitHub（含 GitHub Enterprise Server）和 Bitbucket Data Center。平台里登记成「代码仓库」，编排应用用「提交 PR」节点把文件以 PR 提交上去，资产仓库（`aep.assets`）也改走它，不再直连 GitHub。

落在哪一层：

| 改动 | 层 |
| --- | --- |
| `connectors/git`：读文件、开 PR、查 PR、评论 PR | 连接器（MCP） |
| 数据源登记新增类型 `git`（代码仓库） | 平台资源 |
| 「提交 PR」节点 `git_pr` | 节点类型 |
| `aep.assets` 经 Git 连接器开 PR | 平台底座（原来直连 GitHub） |

不在这次范围：项目绑定代码仓库和工程布局（`docs/api/projects.md` 第二期）、编码 Agent 节点。

## 连接器 `connectors/git`

独立的 Python 3.12 / uv 包，官方 MCP Python SDK，Streamable HTTP：`http://127.0.0.1:8006/mcp`（本地编排里是 `http://connector-git:8006/mcp`）。一个连接器对应一个 Git 服务（一种类型、一个令牌），可以允许多个仓库。

### 边界

- 只往 `AEP_GIT_BRANCH_PREFIX`（默认 `aep/`）开头的分支推送；默认分支和别的分支一律不动。PR 是普通差异，合不合并由人决定。
- 只能访问 `AEP_GIT_REPOS` 里列出的仓库；没列的仓库所有工具都拒绝。
- 令牌只配在连接器上，经环境变量交给 git（`http.extraHeader`），不出现在命令行、远端地址和克隆的配置里，也不在任何返回或报错里。
- 提交用 git 本身（浅克隆、一次一个提交、推送），所以两种服务的提交方式完全一样；默认分支和 PR 用各自的 REST API。
- 文件路径是仓库里的相对路径，用 `/` 分隔，不能有空段、`.`、`..`，不能在 `.git` 里。一次最多 500 个文件，每个不超过 1 MB，合计不超过 10 MB；只读写 UTF-8 文本。

### 工具

仓库写成 GitHub 的 `owner/name` 或 Bitbucket 的 `PROJECT/slug`。`ref` 是分支名或 40 位提交号，不写时用默认分支。

| 工具 | 只读 | 参数 | 返回 |
| --- | --- | --- | --- |
| `list_repos` | 是 | 无 | `{"provider": "github" \| "bitbucket", "branch_prefix": "aep/", "repos": [{"repo", "default_branch", "url"}]}` |
| `list_files` | 是 | `repo`、`path`?（目录，不写是整个仓库）、`ref`? | `{"repo", "ref", "commit", "path", "files": [{"path", "type": "file" \| "dir", "size": 字节 \| null}], "truncated"}`，最多 2000 项 |
| `read_file` | 是 | `repo`、`path`、`ref`? | `{"repo", "ref", "commit", "path", "content"}` |
| `open_pull_request` | 否 | `repo`、`branch`、`title`（1–200）、`files`（`{路径: 文本 \| null}`，null 删除）、`message`（提交信息，1–5000，可以带工单号）、`body`?、`base`?、`directory`?（文件放在哪个目录下，不写是仓库根）、`replace_directory`?（默认 false；true 时删掉 `directory` 下 `files` 里没有的文件） | `{"repo", "number", "url", "state", "title", "branch", "base", "commit", "changed": [改动的路径], "created": 是否新开}` |
| `get_pull_request` | 是 | `repo`、`number` | `{"repo", "number", "url", "state": "open" \| "merged" \| "closed", "title", "branch", "base"}`（Bitbucket 的 DECLINED 算 `closed`） |
| `comment_pull_request` | 否 | `repo`、`number`、`body` | `{"repo", "number", "ok": true}` |

`open_pull_request` 每次都从 `base` 的最新提交做一个新提交，强制推到 `branch`，再找这个分支开着的 PR：有就更新标题和说明，没有就新开。所以同一个分支调用多次（比如步骤重试），分支上始终只有一个提交，PR 也只有一个。和 `base` 比没有任何改动时报错「这些文件和 {base} 上的一样，没有可以提交的改动」，不开空 PR。

工具报错（`is_error`）是中文的一句话：没配置好（「Git 连接器还没配置好，缺少：…」）、仓库不允许、分支或路径不合法、取不到分支、令牌被拒（「GitHub 拒绝了访问（403），请检查令牌的权限：…」）、服务返回的其他错误（带状态码和对方的说明）、连不上。

### 设置

| 变量 | 说明 |
| --- | --- |
| `AEP_GIT_PROVIDER` | `github`（默认）或 `bitbucket` |
| `AEP_GIT_API_URL` | GitHub 默认 `https://api.github.com`，GitHub Enterprise 写 `https://<主机>/api/v3`；Bitbucket 写服务地址，比如 `https://bitbucket.example.com`（连接器自己加 `/rest/api/1.0`） |
| `AEP_GIT_CLONE_URL` | 克隆地址，`{repo}` 换成仓库名；不写时从 API 地址推出：`https://github.com/{repo}.git`、`https://<主机>/{repo}.git`、`<Bitbucket>/scm/{repo}.git` |
| `AEP_GIT_TOKEN` | GitHub：细粒度令牌，Contents 和 Pull requests 读写；Bitbucket：HTTP access token，仓库写权限 |
| `AEP_GIT_REPOS` | 允许的仓库，JSON 列表或逗号分隔 |
| `AEP_GIT_BRANCH_PREFIX` | 默认 `aep/` |
| `AEP_GIT_AUTHOR_NAME`、`AEP_GIT_AUTHOR_EMAIL` | 提交人，默认 `AEP`、`aep@localhost` |
| `AEP_GIT_WORK_DIR`、`AEP_GIT_TIMEOUT_SECONDS` | 克隆放哪里（默认 `/tmp/aep-git`），每个 git 命令的超时（默认 120 秒） |
| `AEP_GIT_MCP_HOST`、`AEP_GIT_MCP_PORT`、`AEP_GIT_MCP_ALLOWED_HOSTS` | MCP 服务 |

本地编排没设 `AEP_GIT_TOKEN`、`AEP_GIT_REPOS` 时沿用资产仓库的 `AEP_ASSETS_GITHUB_TOKEN`、`AEP_ASSETS_REPO`，原来配好的资产仓库不用改设置。

### 健康检查

- `GET /healthz`：不访问 Git 服务，总是 200，说明配置齐不齐（`configured`、`missing`）。
- `GET /healthz?probe=1`：逐个读允许的仓库，都通了返回 200，否则 503 并说明原因。

## 平台这边

### 登记

`/datasources` 新增类型 `git`，页面上叫「代码仓库」。测试连接时要求连接器提供上面 6 个工具，缺了列在 `missing_tools` 里，问题文字是「缺少代码仓库需要的工具：{工具名}。代码仓库连接器要提供 list_repos、list_files、read_file、open_pull_request、get_pull_request、comment_pull_request 这 6 个工具。」代码仓库不是数据源：不受数据外发策略管（它的内容不发给模型），`etl_profile`、`databricks_ingest`、`tool`、`agent` 节点和 `etl` 应用都不接受它（保存时报「「{name}」是代码仓库，只能用在提交 PR 节点里」）。代码只经提交 PR 节点离开平台。

新增 `GET /datasources/{id}/repos`：调连接器的 `list_repos`，返回它的结果。不是代码仓库时 409，连接器报错时 502（`detail` 是连接器的话）。页面用它给节点列仓库。

### 「提交 PR」节点 `git_pr`

分类「工具」，可以设错误处理（`on_error`），不能单独试跑（会真的开 PR）。只有一个出口。

```jsonc
{
  "id": "pr",
  "type": "git_pr",
  "title": "提交 PR",
  "datasource": "<代码仓库 id>",        // 必填，要是 git 类型
  "repo": "acme/etl",                   // 必填，可以引用变量
  "base": "",                           // 目标分支，不写用默认分支
  "directory": "pipelines/{{check_code.bundle.name}}",
  "files": "{{check_code.bundle.files}}", // 必填，引用一个 {路径: 文本 | null} 对象
  "replace_directory": true,            // 默认 false
  "branch": "",                         // 不写时 aep/runs/<运行 id 前 8 位>/<节点 id>
  "pr_title": "新增 {{check_code.bundle.name}}",  // PR 标题，必填，最长 200（title 是节点自己的名字）
  "body": "",                           // 不写时平台写上运行链接和应用
  "message": "{{start.ticket}} 新增管道"          // 提交信息，不写时用 PR 标题
}
```

- 保存时检查：`datasource` 是登记过的代码仓库；`repo`、`files`、`pr_title` 不能为空；`branch` 写了的话不能有变量以外的非法字符（真正的分支规则由连接器检查）。
- 运行时 `files` 渲染出来要是对象，值是文本或 null，否则 `input_error`「files 要引用一个 {路径: 文本} 对象」。连接器报错时 `tool_error`（可以重试），说明里是连接器的话。
- 产出：`number`、`url`、`branch`、`commit`、`created`、`changed`、`summary`。运行过程里说「已在 {repo} 开 PR #{number}：{url}」，更新已有 PR 时说「已更新 {repo} 的 PR #{number}：{url}」。
- 同一次运行重试这一步用同一个分支，所以还是同一个 PR。

### 资产仓库改走连接器

`aep.assets.AssetRepo` 改为调 Git 连接器的 `open_pull_request`（`replace_directory: true`）和 `comment_pull_request`，对外不变：分支、PR、资产登记和 ETL 应用的 `result.asset` 都和原来一样。设置：

- `AEP_ASSETS_REPO`：资产仓库，不变。
- `AEP_ASSETS_GIT_URL`：Git 连接器地址，本地编排默认 `http://connector-git:8006/mcp`。两项都设了才开 PR，否则 ETL 应用的资产一步照旧跳过并说明原因。
- `AEP_ASSETS_GITHUB_TOKEN`、`AEP_ASSETS_API_URL` 不再由平台使用；令牌改配在连接器上（本地编排自动沿用）。
