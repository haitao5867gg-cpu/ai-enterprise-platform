# 运行包接口（第一步：能在另一台机器上跑）

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串，错误格式同 [apps-and-skills.md](apps-and-skills.md)：`{"detail": "中文说明"}`，校验错误 422。

落在哪一层：**平台能力 + 部署**。任何发布过的编排应用、智能体应用都能打成流程包，交给外部应用在自己的机器上跑；应用本身不用写任何代码。方案见「编排对外提供：运行包和按运行收费」，这份契约只管其中第一步。授权、运行凭证、运行回报和账单是第二、三步，另写契约（见文末）。

## 概念

- **平台和运行包是同一个镜像**。`AEP_MODE=platform`（默认）是现在的平台：设计、发布、评测。`AEP_MODE=runtime` 是运行包：只装平台签过名的流程包，经 `/v1` 和 `/mcp/apps` 跑，不能在上面改流程。运行都在外部应用自己的机器上，模型走外部应用自己的网关和 key，运行记录、素材、生成的文件都留在那台机器上。
- **流程包**：一个已发布的应用版本，加上它用到的 Skill 版本和运行要求清单，由平台用私钥签名。运行包用平台的公钥验过才装。
- **运行要求清单**：外部机器要准备什么。一部分由平台从配置里推出来（模型别名、数据源和工具、密钥、Skill），一部分由发布的人在应用上填（机器、网络、安装说明）。
- **绑定**：清单里的数据源和工具服务，外部应用要在运行包上登记一个自己的连接器。登记时用清单里的同一个 `datasource_id`，所以流程配置不用改。
- **自检**：运行包按清单逐项检查。必需项缺一个，这个应用就不能经 `/v1` 发起运行。
- **第一步不支持**（打包时 422），按下面的顺序报第一条：
  1. 代码应用（如 `etl`）：「代码应用暂时不能打成流程包」
  2. 有确认模式的人工节点（`type: human`，`mode: approval`）：「有要审批者确认的步骤，运行包上没有审批的地方，暂时不能打成流程包」。表单模式可以，外部页面经 `/v1/runs/{run_id}/answer` 填
  3. 调用其他应用：编排里的调用应用节点（`type: app`），或智能体配置里的 `apps`：「调用其他应用的应用暂时不能打成流程包」
  4. 用到知识库：「用到知识库的应用暂时不能打成流程包」
  5. 用到上传文件数据源（`kind: file`）：「用到上传文件数据源的应用暂时不能打成流程包」
  6. 有数据工程节点（`etl_profile`、`etl_plan_check`、`etl_code_check`、`databricks_ingest`、`databricks_dev_run`、`databricks_release`、`data_check`、`git_pr`），它们要用平台的 Databricks 工作区和项目：「有数据工程节点的应用暂时不能打成流程包」
  7. 开始节点要求选项目（`project: required`）：运行包上没有项目：「要选择项目的应用暂时不能打成流程包」。`optional` 可以，运行时不带项目
  8. 有节点的数据源要到运行时才知道（引用其他节点的输出、没有默认值的输入、没设的常量）：运行包只能绑定清单里列出的数据源：「有节点的数据源要到运行时才知道是哪个，运行包上没法提前绑定，暂时不能打成流程包」

## 运行要求清单

```jsonc
// Requirements
{
  "platform": {"min_version": "0.1.0"},     // 打包时平台的版本（aep.__version__）；运行包不能比它旧
  "models": [{"alias": "company"}, {"alias": "vision"}],
                                            // 节点里写明的模型别名，按第一次出现的顺序
  "default_model": true,                    // 有调模型的节点没写 model，要用运行包的默认对话模型
  "datasources": [
    {"datasource_id": "uuid", "name": "素材搜索", "kind": "mcp",
     "tools": ["search_videos", "download_video"]}
                                            // tools：调用工具节点和智能体用到的工具名，按第一次出现的顺序；
                                            // 智能体那项选了「全部工具」时不加，连接器有什么就用什么
  ],
  "sandbox": false,                         // 有代码节点（type: code）：运行包要有代码沙箱（AEP_SANDBOX_URL）
  "skills": [{"name": "coach-video-style", "version": 2}],   // 内容随包带
  "secrets": ["crm_token"],                 // 只有名字
  "machine": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100} | null,
                                            // 发布的人填；自检只提示，不拦
  "network": ["api.pexels.com", "pixabay.com"],
                                            // 发布的人填：运行时要访问的外部主机名；自检逐个试连 443 端口，只提示
  "notes": "……"                             // 发布的人写的安装说明，最多 2000 字符
}
```

- `models`、`default_model`、`datasources`、`sandbox`、`skills`、`secrets` 由平台从这个版本的配置推出，和导出文件（`aep-app/1`）的 `requires` 用同一套规则。导出文件的 `requires` 也加上 `models` 和每个数据源的 `tools`；导入时不认识的键照旧忽略，旧文件照旧能导入。
- `machine`、`network`、`notes` 存在应用上，不跟版本走；打包时拷进包里。

平台这边的接口（搭建者）：

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/apps/{app}/runtime-requirements` | | `{"machine", "network", "notes"}`，没填过时是 `{"machine": null, "network": [], "notes": ""}` |
| PUT | `/apps/{app}/runtime-requirements` | `{"machine"?, "network"?, "notes"?}` | 同上。整份替换，没给的键回到空值 |
| GET | `/apps/{app}/versions/{version}/requirements` | | `Requirements`（推出来的部分 + 上面填的部分）。第一步不支持的应用也返回，另带 `"unsupported": "用到知识库的应用暂时不能打成流程包"`；支持时为 null |

校验：`cpu_cores` 1–256，`memory_gb` 1–4096，`disk_gb` 1–100000，都是整数，`machine` 可以整个为 null；`network` 最多 20 个，每个是主机名（字母、数字、`-`、`.`，1–253 字符，不带协议、端口和路径），去重，报错「「网络」里要写主机名，比如 api.pexels.com」；`notes` 最多 2000 字符。

## 流程包

```jsonc
// Package（文件名 <app>-v<version>.aep-package.json，UTF-8 JSON，最多 5 MB）
{
  "format": "aep-package/1",
  "package_id": "uuid",                     // 每次打包新生成
  "issued_at": "……",
  "issuer": {"platform_version": "0.1.0", "key_id": "3f9a0c1d2b4e5f60"},
  "app": "coach-video",
  "version": 3,                             // 平台上的版本号，运行包里沿用
  "kind": "flow" | "agent",
  "title": "教练做视频",
  "summary": "……",
  "app_file": "format: aep-app/1\n……",      // 这个版本的导出文件，原文
  "skills": [{"name": "coach-video-style", "version": 2, "description": "……",
              "body": "……", "references": {"cases.md": "……"}}],
  "requirements": Requirements,
  "signature": "base64"                     // Ed25519 签名
}
```

- **签名**：对去掉 `signature` 之后的对象签。签名的字节是 `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`。`key_id` 是公钥原始 32 字节的 SHA-256 的前 16 个十六进制字符。
- **密钥**：平台的私钥放 `AEP_PACKAGE_SIGNING_KEY`（Ed25519 私钥的 32 字节种子，base64），本地放 `.env`，Azure 放 Key Vault。没设时平台不能打包。运行包把平台的公钥写进 `AEP_PLATFORM_PUBLIC_KEYS`（JSON 数组，base64 原始公钥），换钥匙时可以同时放新旧两把。
- 包里的配置是明文。签名只保证包是平台发的、没被改过，不防别人看。

平台这边的接口（搭建者）：

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| GET | `/apps/{app}/versions/{version}/package` | 200 `Package`，`Content-Disposition: attachment; filename*=UTF-8''<app>-v<version>.aep-package.json` |
| GET | `/packages/public-key` | `{"key_id", "public_key"}`，拿去写进运行包的 `AEP_PLATFORM_PUBLIC_KEYS` |

- 没设 `AEP_PACKAGE_SIGNING_KEY`：503「平台没有配置流程包签名密钥（AEP_PACKAGE_SIGNING_KEY）」，两个接口都是。设了但不是合法的 32 字节 base64：启动时报错退出。
- 第一步不支持的应用：422，`detail` 同上面的 `unsupported`。不存在的应用、版本：404，同导出。
- 包按下载的样子（UTF-8，缩进 2 格）超过 5 MB：422「流程包超过 5 MB（6.2 MB），请减少 Skill 的内容或个数」。

## 运行模式（`AEP_MODE=runtime`）

只开下面这些接口，别的一律 404「运行包里没有这个接口」，在登录和权限检查之前返回：

| 接口 | 用途 |
| --- | --- |
| `GET /healthz`、`GET /readyz`、`/auth/*`、`/users*` | 健康检查；登录照旧（`AEP_AUTH_ENABLED`） |
| `/v1/*`、`/mcp/apps/*` | 外部应用的页面和系统调用，只认 API 密钥，同 [app-access.md](app-access.md) |
| `/runtime/*` | 装包、绑定、自检，见下面 |
| `GET/POST /apps/{app}/keys`、`DELETE /apps/{app}/keys/{key_id}` | 给本机页面建 API 密钥 |
| `PUT/DELETE /apps/{app}/secrets/{name}` | 填清单里的密钥 |
| `GET /apps/{app}/usage` | 用量 |
| `/media-libraries/*` | 媒体库，同 [media.md](media.md) |
| `GET /datasources`、`GET /datasources/{id}`、`/health`、`/tools`、`POST /datasources/test` | 看绑定的连接器；登记走 `/runtime/apps/{app}/bindings` |
| `GET /models` | 本机网关上的别名 |

- 运行包启动时不登记内置应用（`hello`、`etl` 等），只认装进来的包。定时任务、知识库处理、评测都不启动。
- 运行包和平台的数据库分开，各用各的 Postgres。

## 运行包这边的接口

权限：装包、切换版本、绑定要搭建者；看清单和自检要登录（`AEP_AUTH_ENABLED` 关着时是本地用户）。平台模式下 `/runtime/*` 一律 404「这台机器不是运行包（AEP_MODE=runtime）」；没装的应用 404「这台运行包没有装应用 xxx」；切换到没装过的版本 404「没有装过第 9 版」。

```jsonc
// InstalledApp
{
  "app": "coach-video",
  "title": "教练做视频",
  "kind": "flow",
  "version": 3,                             // 现在用的版本
  "versions": [{"version": 3, "package_id": "uuid", "issued_at": "……", "installed_at": "……"}],
                                            // 装过的版本，新的在前
  "requirements": Requirements,             // 现在用的版本的清单
  "check": Check                            // 最近一次自检
}

// Check
{
  "ok": false,                              // 必需项都通过才是 true
  "checked_at": "……",
  "items": [
    {"kind": "platform", "name": "0.1.0", "required": true, "ok": true, "message": null},
    {"kind": "model", "name": "vision", "required": true, "ok": false,
     "message": "模型网关上没有这个别名，请在 LiteLLM 配置里加上"},
    {"kind": "datasource", "name": "素材搜索", "datasource_id": "uuid", "required": true, "ok": false,
     "message": "还没有绑定连接器"},
    {"kind": "secret", "name": "crm_token", "required": true, "ok": true, "message": null},
    {"kind": "skill", "name": "coach-video-style", "version": 2, "required": true, "ok": true, "message": null},
    {"kind": "machine", "name": "内存", "required": false, "ok": false,
     "message": "要求 8 GB，这台机器 4 GB"},
    {"kind": "network", "name": "api.pexels.com", "required": false, "ok": true, "message": null}
  ]
}
```

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| POST | `/runtime/packages` | `Package`（最多 5 MB） | 201 `InstalledApp`；同一个 `package_id` 已经装过时 200，什么都不改 |
| GET | `/runtime/apps` | | `InstalledApp[]`，按 `app` 排序 |
| GET | `/runtime/apps/{app}` | | `InstalledApp` |
| POST | `/runtime/apps/{app}/activate` | `{"version": 2}` | `InstalledApp`，切回装过的另一个版本 |
| PUT | `/runtime/apps/{app}/bindings/{datasource_id}` | `{"connector_url": "http://connector-stock:8010/mcp"}` | 201（新登记）或 200（改地址）`DataSource` |
| GET | `/runtime/apps/{app}/check` | 查询参数 `fresh=true` 可选 | `Check` |

装包（`POST /runtime/packages`，请求体是包文件原文），按顺序检查，第一条不过就停，什么都不装：

1. 超过 5 MB：413「流程包不能超过 5 MB」。不是 JSON、不是 `aep-package/1` 或缺字段，或 `app_file` 读不出来、和 `kind` 对不上：422「不是流程包文件」。
2. `AEP_PLATFORM_PUBLIC_KEYS` 没设：503「运行包没有配置平台公钥（AEP_PLATFORM_PUBLIC_KEYS）」。`key_id` 不是其中任何一把：422「这个包不是用这台运行包认识的平台密钥签的」。签名不对：422「流程包的签名不对，可能被改过」。
3. `requirements.platform.min_version` 比运行包新：422「这个包要求平台 0.2.0 以上，这台运行包是 0.1.0，请先升级镜像」。
4. 包里的配置这台运行包按导入文件的规则检查不过（比如运行包的镜像不认识某个节点）：422「这台运行包读不了包里的应用配置：……」。
5. 包里的 Skill 版本：运行包上没有就按原版本号装上；已有同名同版本但内容不同：409「Skill「x」第 2 版和这台机器上已有的不一样」。
6. 应用：没装过就新建；装过同类型的就加一个版本；同名但类型不同：409「这台机器上已经有一个同名的智能体应用」。同一个版本号已经装过、`package_id` 不同：409「第 3 版已经装过了，平台重新打的包要用新版本」。上次装到一半停了（版本已经加上、配置和包里的一样，但没有安装记录）时，再装同一个包会接着装完，返回 201。
7. 新版本立即成为现在用的版本；已经开始的运行按原来的版本跑完。装完马上自检一次。

绑定（`PUT /runtime/apps/{app}/bindings/{datasource_id}`）：

- `datasource_id` 不在这个应用现在版本的清单里：404「这个应用不需要这个数据源」。
- 没登记过就新建一个数据源：id、名称、类型取清单里的。登记过就只改地址。
- 先测连接，规则同 `POST /datasources/test`；另外要有清单列出的全部工具，缺了 422「连接器缺少工具：download_video」。测不通 422，原记录不变。
- 名称和本机别的数据源重名：409「这台机器上已经有名为「素材搜索」的数据源」。
- 成功后对用到它的应用重新自检。

自检：

- **必需项**：平台版本、包里的 Skill 已装、每个模型别名在本机网关上（`default_model` 为 true 时还要有默认对话模型）、每个数据源已绑定且连得上且有清单列出的工具、`sandbox` 为 true 时代码沙箱连得上且有 `run_python`、每个密钥有值。
- **只提示的**：机器（CPU 核数、内存、`AEP_MEDIA_DIR` 所在磁盘的总空间，低于要求就不通过）、网络（每个主机名 5 秒内能连上 443 端口）。
- 什么时候查：启动时、装包后、绑定或改密钥后、每 24 小时、`fresh=true` 时。其余时候返回上一次的结果。`fresh=true` 时每个模型别名再发一个最小的请求（最多 1 个 token），不通过的写「别名在，但调用失败：……」。调用失败过的别名，之后每次自检（不管带不带 `fresh`）都再调一次，调通为止。

`/v1` 和 `/mcp/apps` 在运行模式下多两条规则：

- 应用不是装进来的：404「没有应用 xxx」（和平台上不存在的应用一样）。
- 现在用的版本没有安装记录（装到一半停了）：503「这个应用在运行包上没有装完，请重新装一次流程包」。
- 现在用的版本最近一次自检有必需项不通过，或者还没查过（自检结果只算它查的那个版本）：先重新自检一次，还不通过就 503「这个应用还不能运行：缺 vision 模型别名、素材搜索连接器（运行包自检没通过）」，最多列 5 项。

## 设置

| 变量 | 默认 | 用在哪 |
| --- | --- | --- |
| `AEP_MODE` | `platform` | `platform` 或 `runtime` |
| `AEP_PACKAGE_SIGNING_KEY` | 空 | 平台：打包的私钥（密钥，只放 `.env` / Key Vault） |
| `AEP_PLATFORM_PUBLIC_KEYS` | `[]` | 运行包：认哪些平台公钥 |

新依赖：无（签名用 `cryptography`，已经随 `mcp` 装上，改为直接依赖并在 `pyproject.toml` 写明）。

## 安装文件

第一步先给固定的安装文件 `infra/runtime/`，装法见 [infra/runtime/README.md](../../infra/runtime/README.md)：

- `compose.yml`：运行模式的 API、Postgres、Redis、模型网关、代码沙箱和它的中转，加上对外的 Caddy。Caddy 只转发 `/v1/*` 和 `/mcp/apps/*`，管理接口只绑本机 `127.0.0.1:8000`。
- `env.example`、`install.sh`（首次问域名、平台公钥、模型网关，然后构建并启动；以后 `git pull` 后再运行）。
- `aep-runtime.sh`：在运行包机器上调本节的管理接口（装包、看应用、自检、绑定、切换版本、填密钥、建 API 密钥）。

清单里的连接器由外部应用写进 `infra/runtime/compose.app.yml`（不进仓库），`install.sh` 发现了就一起启动；连接器地址要加进 `AEP_CONNECTOR_HOSTS`。按清单自动生成安装文件放到后面。

## 后续（另写契约）

- 第二步：外部应用和授权（`/external-apps`、`/licences`）、每次运行前领运行凭证、运行结束回报、平台的「外部运行」页。到时运行包装包时还要检查授权，`/v1` 发起运行前要领凭证。
- 第三步：计量和账单。
- 页面：平台应用页的「对外提供」（填机器、网络、说明，看清单，下载包）和运行包的管理页，由前端另排。

## 测试

- 清单：模型别名和 `default_model`、数据源和用到的工具、`sandbox`、Skill、密钥、发布的人填的部分；第一步不支持的八种情况和它们的顺序；包超过 5 MB。
- 打包和验签：签名能被公钥验过；改动包里任何一个字段都验不过；`key_id` 不认识、没设密钥、`min_version` 太新。
- 运行模式：放行的接口能用，其余 404；内置应用不登记。
- 装包：新装、同一个包再装（200 不改）、加版本、切换版本、Skill 冲突、类型冲突、版本号冲突。
- 绑定和自检：绑定新建和改地址、缺工具、重名、不在清单里；自检各项；自检没过时 `/v1` 503，绑好后能跑。
- 全程用 `FakeGateway` 和 `tests/fake_connector.py` 的进程内连接器，不连外网。
