import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type { AppDetail, FlowConfig } from "../lib/apps";

const id = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
async function detail(
  request: APIRequestContext,
  app: string,
): Promise<AppDetail> {
  const response = await request.get(`/api/apps/${app}`);
  expect(response.status()).toBe(200);
  return response.json();
}
async function create(
  request: APIRequestContext,
  app: string,
  from: object = { blank: true },
) {
  const response = await request.post("/api/apps", {
    data: { app, title: "测试应用", from },
  });
  expect(response.status()).toBe(201);
  return response.json() as Promise<AppDetail>;
}
async function fits(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
}

test("intent template → input → after-sales rejection and approval → result, with branch labels on mobile", async ({
  page,
  request,
}) => {
  const app = id("intent");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/apps");
  await page.getByRole("button", { name: "新建应用", exact: true }).click();
  await page.getByRole("radio", { name: /意图分类路由/ }).check();
  await page.getByRole("button", { name: "下一步", exact: true }).click();
  await page.getByLabel("标识", { exact: true }).fill("Invalid_ID");
  await page.getByRole("button", { name: "创建应用", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "标识须以小写字母开头",
  );
  await page.getByLabel("标识", { exact: true }).fill("hello");
  await page.getByRole("button", { name: "创建应用", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toHaveText(
    "应用标识已存在",
  );
  await page.getByLabel("标识", { exact: true }).fill(app);
  await page.getByLabel("名称", { exact: true }).fill("售后分流");
  await page.getByLabel("简介", { exact: true }).fill("退货问题交给售后处理");
  await fits(page);
  await page.getByRole("button", { name: "创建应用", exact: true }).click();
  await expect(page).toHaveURL(`/apps/${app}`);
  const diagram = page.getByRole("group", { name: "流程图", exact: true });
  await expect(diagram.locator(".flow-edge-label")).toHaveCount(5);
  // The app's own start and end nodes stand in for the graph's __start__ / __end__.
  await expect(diagram.getByText("开始", { exact: true })).toHaveCount(1);
  await expect(diagram.getByText("结束", { exact: true })).toHaveCount(1);
  expect(
    (await diagram.locator(".flow-edge-label").allTextContents()).sort(),
  ).toEqual(["售后", "产品咨询", "都不是", "驳回重写", "确认回复"].sort());
  await fits(page);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出", exact: true }).click();
  expect((await download).suggestedFilename()).toBe(`${app}-v1.yaml`);
  await page
    .getByRole("main")
    .getByRole("link", { name: "新建运行", exact: true })
    .click();
  await expect(page.getByLabel("应用", { exact: true })).toHaveValue(app);
  await page.getByLabel("客户问题").fill("我要退货");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  await expect(page.locator(".human-show")).toContainText(
    "（模拟回答）客户问：我要退货",
  );
  await expect(
    page.getByRole("button", { name: "驳回重写", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("驳回意见").fill("请补充退货时间");
  await page.getByRole("button", { name: "驳回重写", exact: true }).click();
  await expect(page.locator(".human-show")).toContainText("请补充退货时间");
  await page.getByRole("button", { name: "确认回复", exact: true }).click();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  await expect(page.locator(".result-json")).toContainText("reply");
  await expect(page.locator(".result-json")).toContainText("请补充退货时间");
  await fits(page);
  const runId = new URL(page.url()).pathname.split("/").at(-1);
  const flow = await (await request.get(`/api/runs/${runId}/flow`)).json();
  expect(flow.steps.map((step: { node: string }) => step.node)).toEqual([
    "start",
    "route",
    "draft",
    "review",
    "draft",
    "review",
    "end",
  ]);
});

test("form human node requires order ID, maps server validation to its field, submits values and actor", async ({
  page,
}) => {
  await page.goto("/runs/new?app=order-form");
  await page.getByLabel("客户问题").fill("查询订单状态");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(page.getByRole("heading", { name: "需要你填写" })).toBeVisible();
  let posts = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/resume"))
      posts++;
  });
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "订单号为必填项",
  );
  expect(posts).toBe(0);
  await page.getByRole("textbox", { name: /^订单号/ }).fill("A1001");
  await page.route("**/api/runs/*/resume", (route) =>
    route.fulfill({
      status: 422,
      json: {
        detail: [
          { loc: ["body", "values", "order_id"], msg: "订单号格式不正确" },
        ],
      },
    }),
  );
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.locator("#human-order_id-error")).toHaveText(
    "订单号格式不正确",
  );
  await page.unroute("**/api/runs/*/resume");
  await page.getByRole("textbox", { name: /^订单号/ }).fill("A2002");
  await page.getByLabel("审批人").fill("订单专员");
  const resumed = page.waitForRequest(
    (request) =>
      request.method() === "POST" && request.url().endsWith("/resume"),
  );
  await page.getByRole("button", { name: "提交", exact: true }).click();
  expect((await resumed).postDataJSON()).toEqual({
    decision: "submit",
    values: { order_id: "A2002" },
    actor: "订单专员",
  });
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  await expect(page.locator(".result-json")).toContainText(
    '"order_id": "A2002"',
  );
});

test("copy a hello version, rename and archive; historical runs remain readable", async ({
  page,
  request,
}) => {
  const app = id("copy");
  await page.goto("/apps");
  await page.getByRole("button", { name: "新建应用", exact: true }).click();
  await page.getByRole("radio", { name: /复制已有编排应用/ }).check();
  await page.getByLabel("来源应用").selectOption("hello");
  await page.getByLabel("来源版本").selectOption("1");
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByLabel("标识", { exact: true }).fill(app);
  await page.getByLabel("名称", { exact: true }).fill("问答副本");
  await page.getByRole("button", { name: "创建应用", exact: true }).click();
  await expect(page).toHaveURL(`/apps/${app}`);
  expect((await detail(request, app)).current.config).toEqual(
    (await (await request.get("/api/apps/hello/versions")).json()).find(
      (item: { version: number }) => item.version === 1,
    ).config,
  );
  await page.getByRole("button", { name: "修改名称和简介" }).click();
  await page.getByLabel("名称", { exact: true }).fill("改名后的问答");
  await page.getByLabel("简介", { exact: true }).fill("新的简介");
  await page.getByRole("button", { name: "保存应用信息" }).click();
  await expect(
    page.getByRole("heading", { name: "改名后的问答", exact: true }),
  ).toBeVisible();
  const run = await request.post("/api/runs", {
    data: { app, input: { text: "归档后保留记录" } },
  });
  const { run_id } = await run.json();
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "归档应用" }).click();
  expect((await request.get(`/api/apps/${app}`)).status()).toBe(200);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "归档应用" }).click();
  await expect(page).toHaveURL("/apps");
  await expect(page.getByRole("link", { name: /改名后的问答/ })).toHaveCount(0);
  expect((await request.get(`/api/apps/${app}`)).status()).toBe(404);
  expect(
    (
      await request.post("/api/runs", {
        data: { app, input: { text: "不能运行" } },
      })
    ).status(),
  ).toBe(404);
  expect((await request.get(`/api/runs/${run_id}`)).status()).toBe(200);
  await page.goto("/apps/etl");
  await expect(page.getByRole("button", { name: "归档应用" })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "导出", exact: true }),
  ).toHaveCount(0);
});

test("import YAML with missing Skill/datasource/secret lists problems and keeps imported graph as draft", async ({
  page,
  request,
}) => {
  const app = id("import");
  const templates = await (await request.get("/api/app-templates")).json();
  const config = templates.find(
    (item: { id: string }) => item.id === "intent-routing",
  ).config as FlowConfig;
  const draft = config.graph.nodes.find((node) => node.type === "llm")!;
  draft.skills = [{ name: "missing-policy", version: 2 }];
  config.env.secrets = ["crm_token"];
  config.graph.nodes.push({
    id: "lookup",
    type: "tool",
    title: "查询订单",
    datasource: "missing-ds",
    tool: "run_query",
    args: {},
  });
  // Put the tool between the hand-off and the end so the graph stays valid.
  config.graph.edges.find((edge) => edge.from === "handoff")!.to = "lookup";
  config.graph.edges.push({ from: "lookup", to: "end" });
  await page.goto("/apps");
  await page.getByRole("button", { name: "导入", exact: true }).click();
  await page.getByLabel("应用文件").setInputFiles({
    name: "缺少依赖.yaml",
    mimeType: "text/yaml",
    buffer: Buffer.from(
      JSON.stringify({
        format: "aep-app/1",
        title: "导入的应用",
        summary: "检查缺失项",
        config,
      }),
    ),
  });
  await page.getByLabel("标识", { exact: true }).fill(app);
  await page.getByRole("button", { name: "导入应用", exact: true }).click();
  const problems = page.locator(".import-problems");
  await expect(problems).toContainText("missing-policy v2");
  await expect(problems).toContainText("missing-ds");
  await expect(problems).toContainText("crm_token");
  const data = await detail(request, app);
  expect(data.draft?.config).toEqual(config);
  expect(data.current.config).not.toEqual(config);
  await page.getByRole("link", { name: "查看应用详情" }).click();
  await expect(page).toHaveURL(`/apps/${app}`);
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toContainText("草稿");
});

test("all input types, defaults, client limits and loc-based API errors", async ({
  page,
  request,
}) => {
  const app = id("fields");
  await create(request, app);
  const base = await detail(request, app);
  const config = base.current.config as FlowConfig;
  const start = config.graph.nodes.find((node) => node.type === "start")!;
  start.inputs = [
    {
      key: "question",
      label: "问题",
      type: "text",
      required: true,
      max_length: 8,
    },
    { key: "notes", label: "补充说明", type: "textarea", max_length: 30 },
    { key: "count", label: "数量", type: "number", required: true, default: 0 },
    {
      key: "checked",
      label: "需要核对",
      type: "boolean",
      required: true,
      default: false,
    },
    {
      key: "priority",
      label: "优先级",
      type: "select",
      required: true,
      choices: [{ value: "high", label: "高" }],
    },
    { key: "source", label: "业务数据源", type: "datasource", required: true },
  ];
  expect(
    (
      await request.put(`/api/apps/${app}/draft`, { data: { config } })
    ).status(),
  ).toBe(200);
  expect(
    (
      await request.post(`/api/apps/${app}/draft/publish`, {
        data: { note: "通用输入" },
      })
    ).status(),
  ).toBe(200);
  await page.goto(`/runs/new?app=${app}`);
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(page.locator("#input-question-error")).toContainText("必填");
  await page.getByLabel("问题", { exact: false }).first().fill("123456789");
  await page.getByLabel("数量").fill("2.5");
  await page.getByLabel("优先级").selectOption("high");
  await page.getByLabel("业务数据源").selectOption("sample-postgres");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(page.locator("#input-question-error")).toContainText("最多 8");
  await page.getByLabel("问题", { exact: false }).first().fill("查订单");
  await page.getByLabel("补充说明").fill("今天的数据");
  await page.getByLabel("需要核对").check();
  await page.route("**/api/runs", (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          status: 422,
          json: {
            detail: [{ loc: ["body", "input", "count"], msg: "数量超出额度" }],
          },
        })
      : route.continue(),
  );
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(page.locator("#input-count-error")).toHaveText("数量超出额度");
  await page.unroute("**/api/runs");
  const submitted = page.waitForRequest(
    (request) =>
      request.method() === "POST" && request.url().endsWith("/api/runs"),
  );
  await page.getByRole("button", { name: "开始运行" }).click();
  expect((await submitted).postDataJSON()).toEqual({
    app,
    input: {
      question: "查订单",
      notes: "今天的数据",
      count: 2.5,
      checked: true,
      priority: "high",
      source: "sample-postgres",
    },
  });
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
});

test("mock validates graph locations, draft snapshots, exports/imports, secrets and node tests", async ({
  request,
}) => {
  const types = await (await request.get("/api/node-types")).json();
  expect(types).toHaveLength(16);
  expect(
    types.filter((item: { category: string }) => item.category !== "data"),
  ).toHaveLength(11);
  expect(
    types.filter((item: { category: string }) => item.category === "data"),
  ).toHaveLength(5);
  expect(
    types.find((item: { type: string }) => item.type === "llm").presets[0]
      .label,
  ).toBe("抽取字段");
  const app = id("contract");
  const initial = await create(request, app);
  const config = structuredClone(initial.current.config) as FlowConfig;
  const duplicate = structuredClone(config);
  duplicate.graph.nodes.push(duplicate.graph.nodes[1]);
  const duplicateResponse = await request.put(`/api/apps/${app}/draft`, {
    data: { config: duplicate },
  });
  expect(duplicateResponse.status()).toBe(422);
  expect((await duplicateResponse.json()).detail).toContainEqual({
    loc: ["body", "config", "graph", "nodes", "answer", "id"],
    msg: "节点 id 重复",
  });
  const missing = structuredClone(config);
  missing.graph.edges.pop();
  const missingResponse = await request.put(`/api/apps/${app}/draft`, {
    data: { config: missing },
  });
  expect(missingResponse.status()).toBe(422);
  expect((await missingResponse.json()).detail[0].loc).toEqual([
    "body",
    "config",
    "graph",
    "nodes",
    "answer",
    "branches",
    "",
  ]);
  expect(
    (
      await request.post("/api/runs", {
        data: { app, input: { question: "草稿" }, draft: true },
      })
    ).status(),
  ).toBe(409);
  config.env.vars = [{ key: "refund_limit", type: "number", value: 500 }];
  config.env.secrets = ["crm_token"];
  config.graph.nodes.find((node) => node.type === "llm")!.prompt =
    "草稿专用：{{start.question}}";
  expect(
    (
      await request.put(`/api/apps/${app}/draft`, { data: { config } })
    ).status(),
  ).toBe(200);
  expect(
    (
      await request.post("/api/runs", {
        data: { app, input: { question: "冲突" }, draft: true, app_version: 1 },
      })
    ).status(),
  ).toBe(422);
  const invalid = await request.post("/api/runs", { data: { app, input: {} } });
  expect(invalid.status()).toBe(422);
  expect((await invalid.json()).detail[0].loc).toEqual([
    "body",
    "input",
    "question",
  ]);
  const created = await request.post("/api/runs", {
    data: { app, input: { question: "测试" }, draft: true },
  });
  expect(created.status()).toBe(201);
  const { run_id } = await created.json();
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/runs/${run_id}`)).json()).status,
    )
    .toBe("done");
  const run = await (await request.get(`/api/runs/${run_id}`)).json();
  expect(run).toMatchObject({
    draft: true,
    app_version: null,
    result: { answer: "（模拟回答）草稿专用：测试" },
  });
  expect((await detail(request, app)).current.config).toEqual(
    initial.current.config,
  );
  expect(
    (
      await request.put(`/api/apps/${app}/secrets/crm_token`, {
        data: { value: "never-export-me" },
      })
    ).status(),
  ).toBe(204);
  expect((await detail(request, app)).secrets).toContainEqual(
    expect.objectContaining({ name: "crm_token", set: true }),
  );
  const nodeTest = await request.post(`/api/apps/${app}/nodes/answer/test`, {
    data: { values: { "start.question": "单节点" } },
  });
  expect(await nodeTest.json()).toMatchObject({
    status: "done",
    outputs: { text: "（模拟回答）草稿专用：单节点" },
  });
  expect(
    (
      await request.post(`/api/apps/${app}/nodes/start/test`, {
        data: { values: {} },
      })
    ).status(),
  ).toBe(422);
  expect(
    (await request.delete(`/api/apps/${app}/secrets/crm_token`)).status(),
  ).toBe(204);
  expect(
    (await request.delete(`/api/apps/${app}/secrets/crm_token`)).status(),
  ).toBe(204);
  const exported = await request.get(`/api/apps/${app}/versions/1/export`);
  expect(exported.headers()["content-type"]).toContain("text/yaml");
  expect(await exported.text()).not.toContain("never-export-me");
  const imported = await request.post("/api/apps/import", {
    data: { app: id("roundtrip"), yaml: await exported.text() },
  });
  expect(imported.status()).toBe(201);
  expect((await imported.json()).problems).toEqual([]);
  const draftImported = await request.post(`/api/apps/${app}/draft/import`, {
    data: { yaml: await exported.text() },
  });
  expect(draftImported.status()).toBe(200);
  expect((await draftImported.json()).app.draft.config).toEqual(
    initial.current.config,
  );
  for (const [method, path, data] of [
    ["DELETE", "/apps/etl", undefined],
    ["PATCH", "/apps/etl", { title: "不可改" }],
    ["GET", "/apps/etl/versions/1/export", undefined],
    ["POST", "/apps/etl/draft/import", { yaml: await exported.text() }],
  ] as const)
    expect(
      (await request.fetch(`/api${path}`, { method, data })).status(),
    ).toBe(409);
  expect(
    (
      await request.post("/api/apps/import", {
        data: { app: id("bad"), yaml: "not an app" },
      })
    ).status(),
  ).toBe(422);
});
