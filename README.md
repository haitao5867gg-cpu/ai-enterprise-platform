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

### 追踪（Langfuse）

```bash
make up-full
```

在上面的基础上再启动一套自托管 Langfuse：http://localhost:3100，账号 `admin@aep.local`，密码 `aep-local-dev`。每次 run 是一条 trace，`GET /runs/{id}` 返回的 `trace_id` 就是它的 id；里面能看到每个节点、人工确认前后的每一段，以及模型调用的输入、输出、耗时、token 和模型名。

想用 Langfuse Cloud 或其他 Langfuse 服务，在 `.env` 里填 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_HOST` 后用 `make up`。不填就不追踪。

停止：`make down`（包括 Langfuse，数据会保留）。

## 开发

开发约定、目录结构、协作流程都在 [AGENTS.md](AGENTS.md)（Claude 和 Codex 共用）。
