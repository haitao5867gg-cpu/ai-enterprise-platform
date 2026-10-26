# AEP 前端对话工作台

使用 Node.js 22，Next.js App Router + TypeScript strict。页面包括运行列表 `/`、新建运行 `/runs/new`、运行详情 `/runs/[id]`。

## 本地开发与 mock 开关

先在本目录运行 `npm ci`。不连接实际工作流时：

```bash
AEP_MOCK_RUNS=true npm run dev
```

打开 <http://localhost:3000>。顶部会显示“本地模拟模式”。mock 接管 Runs API、数据源列表和文件下载，返回 `201` 创建回执、运行列表、详情和 `202` 恢复回执。新建后从 `running` 进入 `waiting`；确认后从 `running` 进入 `done`；驳回后按意见重新生成，再次进入 `waiting`。支持 `hello` 和 `etl`；ETL mock 提供合成数据源、两张目标表的方案、PySpark / SQL 示例文件及有效 ZIP。示例代码仅用于验证界面，不代表真实 ETL 生成质量。消息在详情页每两秒更新。

mock 默认关闭，**仅 `NODE_ENV=development` 且 `AEP_MOCK_RUNS=true` 时启用**。`next build`、`next start` 和 Docker 的生产模式始终忽略此开关。数据只保存在开发服务端进程内，刷新页面仍可读取，重启服务后清空；不同开发进程互不共享。健康检查始终请求真实后端，不伪造就绪状态，因此单独运行 mock 时后端状态可能显示“不可用”。

切换到真实后端，停止开发服务并重启：

```bash
AEP_MOCK_RUNS=false AEP_API_INTERNAL_URL=http://localhost:8000 npm run dev
```

也可以 `cp .env.example .env.local` 后修改变量。修改环境变量后需重启 Next.js。不要提交 `.env.local`。`AEP_API_INTERNAL_URL` 必须是完整的 HTTP(S) 地址，可以带基础路径；未配置时返回 `503`，没有内置后端地址。仓库的 Compose 已在运行时传入 `http://api:8000`，不需要改后端或 Compose。

## 请求与状态处理

- 浏览器只访问同源 `/api/runs`、`/api/runs/{id}`、`/api/runs/{id}/resume`、`/api/runs/{id}/files.zip`、`/api/datasources`、`/api/readyz`。Next.js route handler 在服务端读取 `AEP_API_INTERNAL_URL` 并转发；地址不会进入浏览器包。转发仅允许这些路径及相应 HTTP 方法，不提供任意后端代理。数据源列表和 ZIP 下载只接受 GET；ZIP 直接转发二进制流，保留 `Content-Type` 与 `Content-Disposition`。
- 详情轮询间隔为上次请求完成后的 2 秒，避免慢请求重叠。`running`、`waiting` 持续轮询，`done`、`error` 和 `404` 停止；离开页面取消请求。临时读取失败保留上次消息并显示错误，继续自动重试，也可手动重试。
- 确认按钮遵守 `pending.options`，只有 `waiting` 且存在审批内容时显示操作。提交中锁定两个按钮并取消旧轮询，提交后立即重新读取状态。提交失败或冲突后也重新核对，避免在过期状态下重复提交。
- 消息以 API 原始顺序、安全的纯文本呈现。错误、空列表、创建失败、详情不存在、运行失败和未知格式均有明确反馈；创建失败保留输入，不自动重复发送写请求。
- 列表接受契约中的数组，也兼容 `{ "runs": [...] }`。每项必须有 `run_id`、`app`、`status`；`messages` 可选。详情验证基础审批字段，允许可选 `pending.details` 和 `trace_id`。未知或无法解析的 details 类型继续显示 prompt 和确认 / 驳回按钮。未在契约中定义的时间、进度等数据不自行推断。
- 转发不缓存，网络失败返回 `502`，10 秒上游超时返回 `504`，错误响应不暴露内部地址；后端的 `201`、`202`、`404`、`409`、`422` 等状态原样保留。

## ETL 工作流

在新建页选择 `etl`，选择已登记的数据源，再描述自然语言需求；生成语言默认 PySpark，也可选择 SQL。数据源加载失败可重试，没有可用数据源时不能发起 ETL。`make up` 会登记“示例库”。

等待确认时，`details.type=etl_plan` 的方案展示每张目标表的来源、做法与清洗规则、列映射、主键、全量 / 增量策略（含增量键）和质量规则。可附意见驳回，修改后的方案由轮询重新读取；确认后继续生成文件。

完成页提供按目录组织的文件按钮、代码视图和 ZIP 下载。内置词法高亮支持 Python、SQL、YAML，其他文件按纯文本展示，不新增依赖。所有内容通过 React 文本节点呈现，不执行生成代码或 HTML。`checks.passed=false` 时列出全部问题；即使未生成文件，也会展示检查失败详情，不提供无效的下载链接。

`trace_id` 非空时显示“在 Langfuse 中查看”，指向本地 `make up-full` 的 `http://localhost:3100/project/aep-local/traces/{trace_id}`。

## 验证

```bash
npm run typecheck
npm run lint
npm run build
npx playwright install chromium
npm test
```

`npm test` 启动独立端口 3100–3102：3100 是开发 mock，3101 是生产前端，3102 是仅供测试的本地契约服务。请先完成 build 并确保端口空闲。测试不使用数据库、外部网络或真实模型；覆盖确认、带意见驳回、刷新详情、列表筛选、故障恢复、两秒轮询、重复提交保护、终态停止轮询、手机布局和键盘操作。生产前端测试故意传入 `AEP_MOCK_RUNS=true`，验证它仍向配置的后端基础路径转发。

`tests/etl.spec.ts` 额外覆盖 ETL 完整流程、默认 PySpark / 可选 SQL、数据源故障与空状态、未知 details、检查失败、纯文本安全呈现、手机布局及 ZIP 下载后的全部文件内容。生产转发测试核对 ZIP 原始字节（含非 UTF-8 字节）和下载响应头。

CI 的 frontend job 执行类型检查、ESLint、构建和以上测试，再启动真实 Compose 后端执行下文的两条浏览器联调路径，上传截图并清理测试容器。可用以下命令生成开发 mock 的页面截图（使用合成需求）：

```bash
AEP_CAPTURE_SCREENSHOTS=1 npm test -- --project=workbench
```

截图位于 `docs/screenshots/`，含运行列表、新建、等待确认、确认完成、驳回后再次确认和手机审批页。

只生成 ETL mock 截图（不会覆盖原有 hello 联调截图）：

```bash
AEP_CAPTURE_SCREENSHOTS=1 npm test -- --project=workbench etl.spec.ts
```

产物为 `etl-plan.png`、`etl-code.png`、`etl-mobile-code.png`，验收范围见 [Issue #13 验收记录](docs/ISSUE-13-VERIFICATION.md)。

## 真实 Compose 联调

在包含 Issue #3 后端的代码上运行 `make up`，确保前端 mock 未开启、后端模型别名为 `mock`。在 <http://localhost:3000> 新建 `hello` 运行，等待确认，确认后检查完成状态与结果；另建一条运行，填写驳回意见后驳回，检查修改后的消息和再次等待确认状态，再确认到完成。刷新详情并从列表重新进入，核对消息顺序与状态一致。

本地 mock 和契约服务测试证明前端行为及服务端转发；只有以上真实 Compose 联调完成后，才能标记 Issue #4 的后端集成验收通过。

可在本目录自动验收以上两条真实后端路径，并生成相同页面的实际 Compose 截图：

```bash
AEP_LIVE_BASE_URL=http://localhost:3000 AEP_CAPTURE_SCREENSHOTS=1 npm run test:live
```

此命令仅允许本机 HTTP 地址，会创建合成测试运行；不会替换或 mock 后端 API。

真实后端 live 用例只运行 `hello`。CI 的 `mock` 模型不会输出结构化 ETL 方案，ETL 界面验收使用开发 mock；两者的结果分别记录。

本次真实验收的 CI 运行、截图来源和完整性校验见 [验收记录](docs/VERIFICATION.md)。
