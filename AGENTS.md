# AGENTS.md

本仓库由多个 AI 编码助手（Claude、Codex）和人类共同开发。所有助手开工前先读完本文件。CLAUDE.md 直接引用本文件，规则只在这里维护。

## 项目是什么

企业 AI 应用平台：给 FDE（一线交付工程师）和 AI PM 用来交付企业 AI 需求，并把工具、提示词、知识库配置、评测集、应用模板沉淀成公司资产。

第一版场景：自然语言驱动的数据 ETL 工作流。用户连接一个关系型数据库，用自然语言描述需求，工作流探查数据、给出方案、经用户确认后生成 Databricks Lakeflow 管道代码（默认 PySpark），在 dev 目标验证，审批通过后部署到 prod，代码以 PR 提交到 Git。

设计文档：
- 架构选型：https://claude.ai/artifact/9rupEcoCYLnf1r5xj5zf2T
- 第一版范围：https://claude.ai/artifact/23y6HKPQxHKYSG2EnTV4Zp

## 已定的架构决定（改动前先在 PR 里说明理由）

- 编排内核：LangGraph（只用核心库，不依赖 LangSmith）。状态用 Postgres checkpointer，人工确认用 `interrupt`。
- 工具接入：一律做成 MCP Server。数据源连接器只读。
- 模型调用：一律经过 LiteLLM 网关（后端用 `aep.llm`），不在业务代码里直接调用某家 SDK。代码里只写模型别名（`mock`、`gpt`、`azure-gpt`、`claude-bedrock`、`qwen`、`deepseek`），别名在 `infra/litellm/config.yaml` 里维护；测试和 CI 一律用 `mock`。必须同时支持国产模型（通义、DeepSeek 等）和 Bedrock、OpenAI / Azure OpenAI。
- 平台应用：每个应用是 `backend/src/aep/runtime/apps/` 下的一张 LangGraph 图，在 `aep.runtime.registry.default_apps` 里注册。节点里调模型只能用 `AppContext.llm_client`。运行通过 Runs API（`/runs`）创建和恢复，接口契约见 `backend/src/aep/api/runs.py`。
- 观测：Langfuse 自托管。
- Databricks：查询走托管 MCP（dbsql），部署走 Declarative Automation Bundles（原 Asset Bundles），分 dev / prod 目标。
- 部署：本地 `docker compose`，生产 Azure（AKS + Helm，Terraform 建环境，Entra ID 登录）。本地和 Azure 用同一套镜像，差异只在配置。
- 第一版单租户、两个角色（使用者、审批者），但业务表都预留 `tenant_id` 列。
- 可视化画布放第二版。

## 目录

```
backend/    Python 3.12 + FastAPI，包名 aep（src 布局），uv 管理依赖
frontend/   Next.js（App Router）+ TypeScript
infra/      基础设施配置（litellm/ 为模型网关）
.github/    CI
```

## 本地运行

```bash
cp .env.example .env        # 可选
make up                     # 等同 docker compose up --build -d
# Web http://localhost:3000   API http://localhost:8000/docs   模型网关 http://localhost:4000
make test                   # 后端测试 + 前端类型检查
make lint
```

单独开发后端：`cd backend && uv sync && uv run uvicorn aep.main:app --reload`（需要本地 Postgres、Redis，可用 `docker compose up -d postgres redis`）。
单独开发前端：`cd frontend && npm ci && npm run dev`。

## 代码规范

- Python：ruff 检查和格式化（行宽 100），类型标注齐全。配置只从 `aep.config.Settings` 读，环境变量统一 `AEP_` 前缀。
- TypeScript：`strict` 模式，`npm run typecheck` 必须通过。
- 新功能必须带测试；后端测试放 `backend/tests/`，不得依赖外部网络或真实模型（用 `tests/conftest.py` 里的 `FakeGateway`）。运行时测试用 `MemoryRunStore` + `InMemorySaver`；需要真实 Postgres 的测试读取 `AEP_TEST_DATABASE_URL`，没设置时自动跳过（CI 会设置）。
- 不提交密钥、`.env`、客户数据。凭据在本地放 `.env`，在 Azure 放 Key Vault。
- 新依赖要在 PR 描述里写明用途。

## 协作流程

- 每个任务对应一个 GitHub Issue，Issue 里写验收标准和建议负责人（Claude / Codex）。
- 一个任务一个分支、一个 PR，PR 关联 Issue；PR 尽量小，只改任务需要的部分。
- 不同助手负责的模块尽量不交叉：默认 Codex 负责前端页面和数据库连接器，Claude 负责 LangGraph 运行时、模型网关和 Databricks 部署链路。需要改对方模块时，在 PR 里点名。
- CI 全绿才能合并；合并由人类决定。
- 修改本文件里的约定，需要单独开 PR。
