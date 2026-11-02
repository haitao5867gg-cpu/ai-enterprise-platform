# 资产目录接口

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串（UTC）。错误格式同 [datasources.md](datasources.md)：`{"detail": "中文说明"}`；请求体不合格返回 422。

落在哪一层：**平台资源**。资产目录不新建资产，只把散在各页的平台资产（应用模板、应用、Skill、知识库、数据源和工具、评测集）列在一处，补上它们各自页面没有的三样东西：负责人和标签、被哪些应用用、质量分；再加一个动作，把资产以 PR 提交到资产仓库（经 Git 连接器，见 [git.md](git.md)）。应用一行不改。

参考了主流平台的做法：AWS AgentCore 和 Gemini Enterprise 的 Agent 登记表（统一列出、标负责人、能看依赖）、腾讯云 ADP 的 Skill 广场、Microsoft Foundry 的资产目录（每项带评测结果）。共同点是一处列全、每项有负责人、能看谁在用、质量有据可查；我们不做市场和跨租户共享（第一版单租户）。

已有的 `GET /assets`（ETL 应用生成的代码包索引，见 [apps-and-skills.md](apps-and-skills.md)）不变；资产目录用新路径 `/catalog`。

## 数据结构

```jsonc
// CatalogItem
{
  "kind": "skill",                  // template | app | skill | knowledge | tool | eval_set，见下表
  "id": "etl-plan",                 // 在这一类里唯一：模板 id、应用名、Skill 名、知识库 id、数据源 id、评测集所属的应用名
  "title": "ETL 方案",               // 显示名
  "summary": "…",                   // 一句话说明，可能为空
  "link": "/skills/etl-plan",       // 它自己的页面（前端路径）：模板 /apps，应用 /apps/{app}，Skill /skills/{name}，
                                    // 知识库 /knowledge/{id}，数据源 /datasources，评测集 /apps/{app}#release
  "owner": "张三" | null,           // 负责人：目录里设过就用设的，否则是新建的人（模板是“平台内置”），都没有为 null
  "owner_set": true,                // owner 是不是在目录里设的（false：取自新建的人或为空）
  "tags": ["零售", "ETL"],           // 目录里设的标签，最多 10 个，每个 1–20 个字符
  "version": 3 | null,              // 当前版本：应用和 Skill 有，其它为 null
  "updated_at": "…" | null,         // 最近一次改动（发布、保存、上传文档等）
  "used_by": [                      // 当前版本用到它的应用，按应用名排，见「被谁用」
    {"app": "retail-etl", "title": "零售 ETL", "version": 4, "via": "方案节点"}
  ],
  "quality": {                      // 见「质量分」；没有可用数据时 score 为 null
    "score": 0.86 | null,           // 0–1
    "basis": "评测通过率",           // 分数怎么来的，给页面直接显示
    "detail": "12 条用例，门槛 0.8"  // 补充说明，可能为空
  },
  "facts": {"documents": 18},       // 每类自己的几项事实，见下表；页面按键名显示
  "repo": {                         // 最近一次提交到资产仓库的 PR，没有为 null
    "number": 7, "url": "https://github.com/acme/aep-assets/pull/7",
    "at": "…", "by": "张三"
  } | null,
  "can_propose": true               // 这一类能不能提交到资产仓库（见「提交到资产仓库」）
}
```

| kind | 页面上叫 | 包括什么 | facts |
| --- | --- | --- | --- |
| `template` | 应用模板 | 平台内置的编排应用模板（`GET /app-templates`） | `needs`：新建时要补的东西 |
| `app` | 应用 | 没归档的应用（编排应用和代码应用） | `kind`（flow / code）、`runs_30d`、`success_rate` |
| `skill` | Skill | 所有 Skill | `versions`：已发布几个版本 |
| `knowledge` | 知识库 | 所有知识库 | `documents`、`chunks`、`embedding_model` |
| `tool` | 数据源和工具 | 所有登记的数据源、工具服务和代码仓库 | `type`（数据源类型，同 datasources.md 的 `kind`） |
| `eval_set` | 评测集 | 评测集里至少有一条用例的应用，每个应用一个（代码应用的评测集文件也列出） | `cases`、`threshold` |

### 被谁用

都只算应用的**当前版本**，不算草稿、旧版本和已归档的应用；读不出配置的应用跳过（记日志），不影响其它项。`via` 是用到它的节点标题，多个用顿号连起来。

| kind | 怎么算 |
| --- | --- |
| `template` | 从这个模板新建的应用（按版本 1 的说明「从模板「…」新建」认，新建时平台自动写的）；`version` 为当前版本，`via` 为“从模板新建” |
| `app` | 在「调用应用」节点里调用它的编排应用 |
| `skill` | 节点里挂了它的应用（同 `GET /skills` 的 `used_by`） |
| `knowledge` | 知识检索节点或 Agent 节点用到它的应用 |
| `tool` | 节点里选了它的编排应用（同 `GET /datasources/{id}/usage`）；代码应用的数据源每次运行时选，不算 |
| `eval_set` | 它所属的应用本身，`via` 为“评测集” |

### 质量分

分数都取自平台已有的记录，不另外打分。页面只显示 `score`（百分比）和 `basis`，没有数据时显示“暂无”。

| kind | `score` | `basis` |
| --- | --- | --- |
| `template` | null | “模板本身不评测” |
| `app` | 当前版本发布时的评测得分；没评测过时用近 30 天运行成功率（`done` / 结束了的运行，至少 3 次才算） | “评测通过率”或“近 30 天运行成功率” |
| `skill` | 用到它、且当前版本评测过的应用的评测得分的平均 | “N 个应用的评测平均分” |
| `knowledge` | 处理成功的文档 / 全部文档（第 6 批的检索回归上线后改用回归通过率） | “文档处理成功率” |
| `tool` | 用到它的应用近 30 天运行成功率的平均（至少 3 次运行） | “用到它的应用的运行成功率” |
| `eval_set` | 应用当前版本的评测得分 | “最近一次评测通过率” |

`detail` 举例：应用“12 条用例，门槛 0.8”；Skill“retail-etl 0.9、jade-t2 0.82”；知识库“18 个文档，1 个处理失败”。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/catalog` | `{"items": [CatalogItem], "repo_ready": true}`：全部资产，按 kind（上表顺序）再按 `title` 排；`repo_ready` 是配没配资产仓库。可带 `kind=`（一类，不认识 422）和 `q=`（在 `title`、`summary`、`owner`、`tags` 里找，不分大小写） |
| GET | `/catalog/{kind}/{id}` | 一项；kind 不认识或不存在 404“没有这项资产” |
| PUT | `/catalog/{kind}/{id}` | 设负责人和标签：`{"owner": "张三" \| null, "tags": ["零售"], "actor": "张三"}`，返回 `CatalogItem`。`owner` 最多 50 个字符，null 或空表示改回默认（新建的人）；标签去掉首尾空格和重复；`actor` 同 runs 的 `requester`，登录打开时用登录人 |
| POST | `/catalog/{kind}/{id}/pull-request` | 把资产提交到资产仓库，见下 |

资产被删掉（应用归档）后，它在目录里设的负责人、标签和 PR 记录在下一次列目录时一并删掉，同名新建的不会继承。

### 提交到资产仓库

把资产当前的内容以一个 PR 提交到资产仓库（`AEP_ASSETS_GIT_URL` 指向的 Git 连接器，仓库和 base 分支同 [git.md](git.md) 的「资产仓库」）。请求体可选：`{"note": "这次改了什么", "actor": "张三"}`（`note` 最多 500 个字符，写进 PR 说明）。

每类放在仓库里的位置，整个目录替换（`replace_directory`）：

| kind | 目录 | 文件 |
| --- | --- | --- |
| `app` | `apps/{app}/` | `app.yaml`（当前版本的导出，同 `GET /apps/{app}/versions/{version}/export`）；有评测集时加 `eval-set.yaml` |
| `skill` | `skills/{name}/` | `SKILL.md`（当前版本：开头是 name、description、version 的 YAML 头，后面是正文）；有参考资料时每个一个 `references/{文件名}` |
| `knowledge` | `knowledge/{id}/` | `README.md`（名称、说明、向量模型、切块设置、文档清单）；md 和 txt 文档原文放 `docs/{文件名}`，PDF 和 Word 只列在清单里 |
| `eval_set` | `evals/{app}/` | `eval-set.yaml`（门槛、评判模型、全部用例） |

- 分支固定为 `aep/catalog/{kind}/{id}`：同一项再提交时更新同一个 PR，不重复开。
- PR 标题“资产：{页面上叫的类名}「{title}」”，说明里写版本、负责人、`note`、提交的人和平台链接。
- 返回 201 和 `{"number", "url", "branch", "created", "changed"}`（同 `git_pr` 节点的输出），并记到这项的 `repo` 上。
- `template` 和 `tool` 不提交（模板随平台代码走，数据源只有地址没有内容），代码应用没有导出格式也不提交，代码应用的评测集文件本来就在代码仓库里：409“{类名}不提交到资产仓库”“「…」是代码应用，不提交到资产仓库”“「…」放在代码里，不提交到资产仓库”；`can_propose` 对它们为 false。
- 应用或 Skill 还没有已发布的版本：409“「…」还没有发布的版本”。
- 没配资产仓库：409“没配资产仓库（AEP_ASSETS_GIT_URL）”；连接器报错：502，`detail` 是连接器的说明（同 git.md）。

## 权限

登录打开时，`/catalog` 下的接口都要**搭建者**；两个写操作记进操作记录：“修改资产信息”“把资产提交到资产仓库”。

## 页面

导航新增「资产目录」，放在「应用」前面（搭建者可见）。

- 顶部：类别筛选（全部、应用模板、应用、Skill、知识库、数据源和工具、评测集，各带数量）和搜索框。
- 表格每行：名称（链到它自己的页面）、类别、负责人（没设时灰字显示新建的人）、版本、被谁用（“3 个应用”，点开列出应用和节点）、质量分（百分比 + `basis`，鼠标移上去看 `detail`）、资产仓库（PR 链接或“未提交”）。
- 每行的操作：「编辑」（负责人和标签）、「提交到资产仓库」（`can_propose` 为 false 时不显示；提交前弹框写说明，成功后显示 PR 链接，同一项再提交时提示“会更新已有的 PR #n”）。
- 没配资产仓库时「提交到资产仓库」按钮置灰，提示“没配资产仓库”。
