# AI Enterprise Platform

企业 AI 应用平台：帮助 FDE 和 AI PM 交付企业 AI 需求，并把交付成果沉淀为可复用的公司资产。

当前进度：M0 工程骨架已完成，M1 进行中。第一版范围与里程碑见 [AGENTS.md](AGENTS.md) 中的设计文档链接。

## 快速开始

需要 Docker。

```bash
make up
```

- Web：http://localhost:3000（显示后端和依赖服务的健康状态）
- API 文档：http://localhost:8000/docs
- 模型网关（LiteLLM）：http://localhost:4000。不填任何 Key 也能用 `mock` 模型；要用真实模型，把 `.env.example` 复制为 `.env` 并填入对应厂商的 Key，然后重新执行 `make up`。
- 健康检查：`/healthz`（进程存活）、`/readyz`（Postgres、Redis、模型网关均可用）

### ETL 应用

`make up` 会自动登记一个本地示例库（客户、商品、订单）。用 API 发起一次运行：

```bash
ds=$(curl -s localhost:8000/datasources | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["datasource_id"])')
curl -s localhost:8000/runs -H 'content-type: application/json' \
  -d "{\"app\":\"etl\",\"input\":{\"datasource_id\":\"$ds\",\"request\":\"做一张订单明细宽表\"}}"
```

工作流会先探查数据、给出方案，然后等待确认（`POST /runs/{id}/resume`）。确认后生成 Databricks bundle，用 `GET /runs/{id}/files.zip` 下载。`mock` 模型不会输出方案，这个应用要在 `.env` 里配好一个真实模型，并把 `AEP_DEFAULT_MODEL` 设成它的别名（例如 `qwen`）；也可以在 `input.models` 里为 `plan` 和 `codegen` 分别指定模型。

在 `.env` 里填了 `AEP_DATABRICKS_HOST` 和 `AEP_DATABRICKS_TOKEN` 之后，生成的 bundle 会在 API 容器里用 Databricks CLI 部署到 dev 目标并试跑一次（`bundle validate`、`deploy -t dev`、`run -t dev`），然后从管道事件日志读出每张表写入的行数和质量规则的结果，写进 run 结果的 `verification`。试跑失败时，报错会交回代码生成重写，最多试跑三次。目标 catalog、schema、源表所在的 catalog 和经典计算的节点类型见 `.env.example`。不填就跳过这一步，结果里会写明跳过的原因。没有建管道集群的权限时，可以填 `AEP_DATABRICKS_CLUSTER_ID`，改为在这台现有集群上执行生成的转换逻辑，核对行数和质量规则，不部署管道。

### 追踪（Langfuse）

```bash
make up-full
```

在上面的基础上再启动一套自托管 Langfuse：http://localhost:3100，账号 `admin@aep.local`，密码 `aep-local-dev`。每次 run 是一条 trace，`GET /runs/{id}` 返回的 `trace_id` 就是它的 id；里面能看到每个节点、人工确认前后的每一段，以及模型调用的输入、输出、耗时、token 和模型名。

想用 Langfuse Cloud 或其他 Langfuse 服务，在 `.env` 里填 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_HOST` 后用 `make up`。不填就不追踪。

停止：`make down`（包括 Langfuse，数据会保留）。

## 开发

开发约定、目录结构、协作流程都在 [AGENTS.md](AGENTS.md)（Claude 和 Codex 共用）。
