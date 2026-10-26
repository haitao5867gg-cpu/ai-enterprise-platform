# Issue #13 验收记录

本地验收日期：2026-10-26（America/Los_Angeles）。分支 `codex/issue-13-etl-workbench`，基于最新 main 的 `9fc705894aa8417d43ef15f1fbc1330c1202aedc`。

## 验证结果

- `npm run typecheck`：通过。
- `npm run lint`：通过。
- `npm run build`：通过，包含生产 standalone 构建。
- `npm test`：21 个用例通过，用时 53.1 秒。新增 8 个 ETL 工作台用例和 1 个生产转发用例，原有 12 个用例全部通过。
- 本地 Node.js `v26.3.1`；仓库 CI 使用 Node.js 22，远端检查结果见 PR 的 Checks。
- 未修改 `backend/`、CI 配置或依赖清单；原有真实后端 live 用例保持只运行 `hello`。

## 实际覆盖的流程

1. 选择 `etl` → 加载并选择“示例库” → 输入自然语言需求，默认语言为 PySpark。
2. 核对两张目标表的来源、做法、列映射、主键、全量 / 增量策略、增量键及告警 / 丢弃 / 停止管道三种质量处理方式。
3. 填写意见驳回一次，核对 POST 内容和修改后的方案；刷新后方案仍存在，再确认到完成。
4. 查看文件目录，切换 Python 与 YAML 文件并核对高亮；下载 ZIP，逐一核对全部 5 个文件的内容。另用 Python 标准库 `ZipFile.testzip()` 独立检查下载文件的 CRC，结果通过。
5. 可选 SQL 路径、切回 hello、数据源加载 / 失败 / 重试 / 空列表、未知 / 空 / 损坏 details 的通用确认、检查失败（含零文件）、生成文本中的 HTML 不执行、390px 手机布局均通过。
6. 生产模式忽略 mock 开关，实际请求本地契约服务；核对数据源转发、ZIP 中包含 NUL 与非 UTF-8 字节时仍逐字节一致、Content-Disposition 原样保留、404 和方法限制。

## 截图来源

截图来自开发 mock 的同一条 ETL 浏览器流程，全部使用合成数据。已检查方案表格、代码高亮、文件目录、审批区及手机页面。这里的 mock 是 Next.js 本地运行 mock，**不表示真实模型已完成 ETL 生成验收**；真实后端 CI 的模型别名 `mock` 不会生成 ETL 方案，因此 live 用例仍只运行 hello。

生成命令（在 frontend 目录）：

```bash
AEP_CAPTURE_SCREENSHOTS=1 npm test -- --project=workbench etl.spec.ts -g 'ETL: choose source'
```

| 截图 | 尺寸 | SHA-256 |
| --- | --- | --- |
| [方案页](screenshots/etl-plan.png) | 1360 × 1887 | `eb7ea454cdf2781a52b38241edca730403fa05aebc341648f199e6769cc27360` |
| [代码页](screenshots/etl-code.png) | 1360 × 1000 | `0b9e3f3302e1dd3ee4fbd6a92a642db6dd9ecbb99df5d14e9b6d8e8687b5608d` |
| [手机代码页](screenshots/etl-mobile-code.png) | 390 × 1652 | `833a8e6c6fee882fa5b5b77c578a3e91897702e6567cf40fb8f19f90c0f11046` |

原有 Issue #4 的 hello 截图和 [历史验收记录](VERIFICATION.md) 保持原样。
