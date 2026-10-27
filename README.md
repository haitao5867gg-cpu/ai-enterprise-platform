# AI Enterprise Platform

企业 AI 应用平台：帮助 FDE 和 AI PM 交付企业 AI 需求，并把交付成果沉淀为可复用的公司资产。

当前进度：M1、M2 已完成，M3 进行中。第一版范围与里程碑见 [AGENTS.md](AGENTS.md) 中的设计文档链接。

## 快速开始

需要 Docker（Mac / Windows 装 Docker Desktop）。没有 `make` 时用 `docker compose up --build -d` 代替 `make up`，`docker compose --profile observability down` 代替 `make down`。

```bash
make up
```

- Web：http://localhost:3000（显示后端和依赖服务的健康状态）
- API 文档：http://localhost:8000/docs
- 模型网关（LiteLLM）：http://localhost:4000。不填任何 Key 也能用 `mock` 模型；要用真实模型，把 `.env.example` 复制为 `.env` 并填入对应厂商的 Key，然后重新执行 `make up`。
- 健康检查：`/healthz`（进程存活）、`/readyz`（Postgres、Redis、模型网关均可用）

### ETL 应用

`make up` 会自动登记两个本地示例库：「示例库」（PostgreSQL）和「MySQL 示例库」。两者都有同样的客户、商品、订单及脏数据，连接器均只读。MySQL 的运行、权限和验证说明见 [connectors/mysql](connectors/mysql/README.md)。`mock` 模型不会输出方案，ETL 要用真实模型：把 `.env.example` 复制为 `.env`，填好一个模型并把 `AEP_DEFAULT_MODEL` 设成它的别名。例如用公司的 OpenAI 兼容网关：

```bash
AEP_DEFAULT_MODEL=company
COMPANY_API_BASE=https://<网关地址>/v1
COMPANY_API_KEY=<密钥>
# COMPANY_MODEL=openai/deepseek-v4.1-flash-new   # 默认值，模型 id 以网关 /v1/models 为准
```

改完 `.env` 后重新执行 `make up`，打开 http://localhost:3000 →「新建运行」→ 应用选 `etl`、数据源选「示例库」或「MySQL 示例库」、写下需求 →「开始运行」。工作流先探查数据、给出方案，在页面上确认（或写意见驳回）后生成 Databricks bundle，可以在页面上看代码、下载 ZIP。

也可以直接用 API：

```bash
ds=$(curl -s localhost:8000/datasources | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["datasource_id"])')
curl -s localhost:8000/runs -H 'content-type: application/json' \
  -d "{\"app\":\"etl\",\"input\":{\"datasource_id\":\"$ds\",\"request\":\"做一张订单明细宽表\"}}"
```

确认用 `POST /runs/{id}/resume`，下载用 `GET /runs/{id}/files.zip`；`input.models` 可以为 `plan` 和 `codegen` 分别指定模型。

#### 用上传的文件试跑

`make up` 后打开「新建运行」，应用选择 `etl`，点击数据源旁的「上传文件」。输入数据集名称，
选择或拖入 CSV / XLSX（每次最多 20 个、单文件最多 50 MB），点击「上传并创建数据源」。
上传完成会显示每张表的名称、列名、列数与行数，并自动选中这个数据源；填写需求后开始运行，
查看探查结果和方案，停在方案确认这一步即可。

CSV 支持 UTF-8、UTF-8 BOM 和 GBK，Excel 每个工作表生成一张表，第一行必须是非空且不重复的
表头，表头后至少有一行数据；类型自动推断。表名转小写、特殊字符替换为下划线，重名追加序号，
全中文名使用 `table` 等名称，原始文件名和工作表名保存在 manifest。数据持久保存在 `uploads`
卷中，API 读写、文件连接器只读；目录位置和连接器地址见 `.env.example`。
上传文件同步到 Databricks 不属于此功能范围。

```bash
curl -sSf localhost:8000/datasources/files \
  -F 'name=本月订单' -F 'files=@orders.csv' -F 'files=@customers.xlsx'
```

只想验证工作台交互和方案确认时，可在前端开发模式启用 `AEP_MOCK_RUNS=true npm run dev`。
本地模拟支持上传 CSV / XLSX、中文校验、自动选择和基于上传表的模拟方案；模拟数据仅保留在
前端进程内。Compose 默认的 LiteLLM `mock` 仅回复固定文本，不生成真实 ETL 方案；实际探查与
模型规划使用上文配置的模型，自动化测试使用脚本化 mock 模型且不依赖真实模型。

#### Databricks dev 试跑

在 `.env` 里填了 `AEP_DATABRICKS_HOST` 和 `AEP_DATABRICKS_TOKEN` 之后，生成的 bundle 会部署到 dev 目标并运行一次，每张表的行数和质量规则结果写进 run 结果的 `verification`，页面上也会显示。试跑失败时，报错交回代码生成重写，最多试跑三次。不填就跳过这一步，结果里写明原因。部署方式由 `AEP_DATABRICKS_DEPLOY` 决定（不填时：有 `AEP_DATABRICKS_CLUSTER_ID` 就用 `job`，否则用 `pipeline`）：

- `job`：bundle 是一个跑在已有集群上的 Notebook Job，通过 REST API 部署并运行，写入目标表。不需要建集群的权限。
- `pipeline`：Lakeflow 管道，在 API 容器里用 Databricks CLI `bundle deploy -t dev` 部署后运行；需要建集群的权限（或 `AEP_DATABRICKS_PIPELINE_POLICY_ID`），Azure 中国区没有 serverless 时还要填 `AEP_DATABRICKS_PIPELINE_NODE_TYPE`。
- `cluster`：只在已有集群上执行一次转换逻辑，核对行数和质量规则，不部署也不写表。

源表要在 Databricks 里也能读到（`AEP_DATABRICKS_SOURCE_CATALOG`，例如 Lakehouse Federation 的外部目录，或导入了同样数据的 catalog）。例如：

```bash
AEP_DATABRICKS_HOST=https://adb-<workspace-id>.<n>.databricks.azure.cn
AEP_DATABRICKS_TOKEN=<个人访问令牌>
AEP_DATABRICKS_CLUSTER_ID=<集群 id>
AEP_DATABRICKS_SOURCE_CATALOG=<源表所在 catalog>
AEP_DATABRICKS_CATALOG=<目标 catalog>
AEP_DATABRICKS_SCHEMA=<目标 schema>
```

#### 上线审批与 prod 部署

再填上 prod 目标后，dev 试跑通过的 run 会停下来等上线审批：页面（或 `POST /runs/{id}/resume`，可带 `actor` 写明审批人）确认后，bundle 部署到 prod 目标并按计划运行，部署时不立即运行；驳回时写下意见，代码按意见重写、再试跑、再等审批。每次确认和驳回都记在 run 结果的 `approvals` 里，部署结果在 `release` 里。`cluster` 模式只试跑不部署，不提供上线。

```bash
AEP_DATABRICKS_PROD_CATALOG=<prod catalog>
AEP_DATABRICKS_PROD_SCHEMA=<prod schema>
# AEP_DATABRICKS_PROD_CLUSTER_ID=<prod 用的集群，默认同 AEP_DATABRICKS_CLUSTER_ID>
# AEP_DATABRICKS_PROD_SCHEDULE=0 0 2 * * ?   # Quartz cron，默认每天 2 点；run 的 input.schedule 可单独指定
# AEP_DATABRICKS_PROD_TIMEZONE=Asia/Shanghai
```

#### 生成的代码以 PR 进资产仓库

配上资产仓库后，dev 试跑通过的 bundle 会提交到这个仓库的 `pipelines/<bundle 名>/` 下：每个版本新开一个分支（`aep/<bundle 名>/v<版本>-<run id 前 8 位>`）和一个 PR。PR 描述里写着原始需求、确认后的方案和假设、dev 试跑结果、审批记录、平台 run 链接和 Langfuse trace id；上线审批的结论（通过并部署、或驳回）会以评论补在 PR 上。PR 合不合并由人决定。驳回后重写的代码试跑通过，会作为下一个版本另开 PR。bundle 名取 run 输入里的 `name`，没给就用第一张目标表的表名，所以针对同一张表的多次运行会累加成同一个资产的不同版本。

平台在资产索引里记下每个版本（bundle 名、版本、PR、commit、来源 run、状态）。版本号在开 PR 之前就预留好：并发的 run 拿到不同版本，同一步骤重跑（比如进程重启后恢复）沿用原来的版本、分支和 PR；预留了但 PR 没开成的版本状态是 `pending`。`GET /assets?name=<bundle 名>` 可以按名称查。不配就跳过，run 结果的 `asset` 里写明原因；评测（`input.release = false`）不提交。

资产仓库要先有一个初始提交（新建时勾选「Add a README」）。令牌用 fine-grained token，只授权这一个仓库，给 Contents 和 Pull requests 读写：

```bash
AEP_ASSETS_REPO=<owner>/<仓库名>
AEP_ASSETS_GITHUB_TOKEN=<令牌>
# AEP_ASSETS_BASE_BRANCH=main
# AEP_WEB_URL=http://localhost:3000   # PR 里 run 链接的地址
```

### 评测

`evals/etl/cases.yaml` 是 ETL 评测集：示例库上的一组需求，以及好的结果要满足什么（目标表、列、假设、试跑行数等，写法见文件开头）。平台起来之后：

```bash
make eval MODEL=company                       # 全部用例
make eval MODEL=company ONLY="two-layer"      # 只跑某几个
```

每个用例起一次 etl run，自动确认方案，只在 Databricks dev 上试跑、不上线（`input.release = false`），然后按方案、代码、结果三层打分，打印表格，JSON 报告写到 `evals/reports/`。没配 Databricks 时结果层跳过。换模型或改提示词之前先跑一遍对比分数。

### 追踪（Langfuse）

```bash
make up-full
```

在上面的基础上再启动一套自托管 Langfuse：http://localhost:3100，账号 `admin@aep.local`，密码 `aep-local-dev`。每次 run 是一条 trace，`GET /runs/{id}` 返回的 `trace_id` 就是它的 id；里面能看到每个节点、人工确认前后的每一段，以及模型调用的输入、输出、耗时、token 和模型名。

想用 Langfuse Cloud 或其他 Langfuse 服务，在 `.env` 里填 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_HOST` 后用 `make up`。不填就不追踪。

工作台的「在 Langfuse 中查看」链接由 web 服务运行时的 `AEP_LANGFUSE_UI_URL`（默认 `http://localhost:3100`）和 `AEP_LANGFUSE_PROJECT_ID`（默认 `aep-local`）配置；地址填写浏览器可访问的 URL，设为 `AEP_LANGFUSE_UI_URL=` 时隐藏链接。修改配置后重建 web 容器即可，无需重新构建镜像。

停止：`make down`（包括 Langfuse，数据会保留）。

## 开发

开发约定、目录结构、协作流程都在 [AGENTS.md](AGENTS.md)（Claude 和 Codex 共用）。
