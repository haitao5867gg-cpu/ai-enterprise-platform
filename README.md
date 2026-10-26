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

停止：`make down`。

## 开发

开发约定、目录结构、协作流程都在 [AGENTS.md](AGENTS.md)（Claude 和 Codex 共用）。
