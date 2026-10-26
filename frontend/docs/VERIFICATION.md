# Issue #4 验收记录

真实后端浏览器验收于 2026-10-25 22:58（America/Los_Angeles）在 GitHub Actions 完成。

- 验收代码：`c19e0ca6c2cd65856c129f8dc434b5d5d76cb7ec`，包含 main 的 Issue #3 和观测更新。
- [CI 运行 #11](https://github.com/haitao5867gg-cpu/ai-enterprise-platform/actions/runs/40276809437)，frontend job：`121421180621`。
- Node.js 22 下的 `npm run typecheck`、`npm run lint`、`npm run build` 与 `npm test` 全部通过，12 个浏览器/API 测试通过。
- 同一 frontend job 执行 `make up`、等待 Compose 服务就绪，再运行 `npm run test:live`：2 个真实后端浏览器测试通过，用时 7.8 秒。

## 真实路径

1. 新建 hello → 等待确认 → 确认 → 完成。读取真实 Runs API 核对 `done` 和三条消息，刷新详情后结果仍然存在。
2. 新建 hello → 附意见驳回 → 后端重新生成 → 再次等待确认 → 确认 → 完成。核对驳回意见、四条中间消息、五条最终消息与最终 `done`，刷新和返回列表均正常。

验收使用实际 FastAPI、LangGraph、Postgres、Redis 和 LiteLLM 网关。**截图中的 mock 指 LiteLLM 的模型别名；Next.js 的本地 Runs mock 未开启。** 浏览器只访问同源 Next.js 路由，390px 手机视口没有页面横向溢出。

## 截图来源与完整性

六张截图均由上述真实 Compose 浏览器测试生成，已逐张检查布局与中文显示：

- [运行列表](screenshots/run-list.png)
- [新建运行](screenshots/new-run.png)
- [等待确认](screenshots/approval.png)
- [完成结果](screenshots/completed.png)
- [驳回后再次确认](screenshots/rejected.png)
- [手机审批](screenshots/mobile-approval.png)

[原始截图产物](https://github.com/haitao5867gg-cpu/ai-enterprise-platform/actions/runs/40276809437/artifacts/12630507676) ID：`12630507676`。下载后已核对 ZIP SHA-256，再解压保存到本目录：

```text
16ffe0af233540cb59ac7846daf192110040e4e105c98bd2c391a83c1f75076a
```

所有需求和意见均为合成测试内容。CI 在验收后清理了本次测试容器及数据卷。
