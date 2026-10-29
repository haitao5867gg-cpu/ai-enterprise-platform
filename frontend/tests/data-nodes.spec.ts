import {
  expect,
  test,
  type APIRequestContext,
  type Page,
  type TestInfo,
} from "@playwright/test";
import { mkdir } from "node:fs/promises";
import type { AppDetail, FlowConfig, NodeType } from "../lib/apps";
import type { AppManual } from "../lib/app-manual";

test.describe.configure({ timeout: 90_000 });
const id = () => `data-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });
const manual = (page: Page) =>
  page.getByRole("group", { name: "应用说明书", exact: true });
async function detail(
  request: APIRequestContext,
  app: string,
): Promise<AppDetail> {
  return (await request.get(`/api/apps/${app}`)).json();
}
async function create(
  request: APIRequestContext,
  source: { blank: true } | { app: string } | { template: string } = {
    blank: true,
  },
) {
  const app = id();
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title: "数据工程测试", from: source },
      })
    ).status(),
  ).toBe(201);
  return app;
}
async function listView(page: Page) {
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
}
async function select(page: Page, nodeId: string) {
  await page
    .getByRole("complementary", { name: "节点列表" })
    .getByRole("button", { name: new RegExp(`（${nodeId}）$`) })
    .click();
}
async function next(page: Page, label: string, target: string) {
  await panel(page)
    .getByRole("group", { name: "下一步" })
    .getByLabel(label, { exact: true })
    .selectOption(target);
}
async function insert(page: Page, label: string, ref: string) {
  await panel(page).getByLabel(label, { exact: true }).fill("");
  await panel(page)
    .getByRole("button", { name: `插入变量：${label}`, exact: true })
    .click();
  await panel(page)
    .getByRole("group", { name: `可插入「${label}」的变量`, exact: true })
    .getByRole("button", { name: ref, exact: true })
    .click();
}
async function capture(page: Page, name: string, info: TestInfo) {
  const directory =
    process.env.AEP_ISSUE_84_SCREENSHOTS ??
    (process.env.CI || process.env.AEP_CAPTURE_SCREENSHOTS === "1"
      ? "docs/screenshots/issue-84"
      : undefined);
  if (!directory) return;
  await mkdir(directory, { recursive: true });
  const path = `${directory}/${name}.png`;
  const target = name.startsWith("form-")
    ? panel(page)
    : name.endsWith("manual")
      ? manual(page)
      : name === "run-report"
        ? page.locator(".result-field").first()
        : page.getByRole("region", { name: "节点 dev 运行", exact: true });
  await target.screenshot({ path, animations: "disabled" });
  await info.attach(name, { path, contentType: "image/png" });
}

test("add and save all five data nodes using the existing selectors and branch controls", async ({
  page,
  request,
}, info) => {
  const app = await create(request);
  const config = (await detail(request, app)).current.config as FlowConfig;
  const start = config.graph.nodes[0];
  if (start.type !== "start") throw new Error("missing start");
  start.inputs.push(
    { key: "datasource", label: "数据源", type: "datasource" },
    { key: "language", label: "语言", type: "text" },
    { key: "name", label: "bundle 名", type: "text" },
  );
  config.graph.nodes.splice(1, 0, {
    id: "glossary",
    title: "业务口径",
    type: "knowledge",
    knowledge_bases: ["policies"],
    query: "{{start.question}}",
    top_k: 5,
    min_score: 0.3,
  });
  config.graph.edges = [
    { from: "start", to: "glossary" },
    { from: "glossary", to: "answer" },
    { from: "answer", to: "end" },
  ];
  expect(
    (
      await request.put(`/api/apps/${app}/draft`, { data: { config } })
    ).status(),
  ).toBe(200);
  await listView(page);
  await page.goto(`/apps/${app}`);
  const types: NodeType[] = await (await request.get("/api/node-types")).json();
  const nodes = [
    ["探查数据", "etl_profile"],
    ["校验方案", "etl_plan_check"],
    ["抽取到湖仓", "databricks_ingest"],
    ["检查并打包", "etl_code_check"],
    ["dev 运行", "databricks_dev_run"],
  ] as const;
  for (const [label, nodeId] of nodes) {
    await page.getByRole("button", { name: "添加节点", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "添加节点" });
    const headings = await dialog
      .getByRole("heading", { level: 4 })
      .allTextContents();
    expect(headings.indexOf("数据工程")).toBe(headings.indexOf("工具") + 1);
    await dialog
      .getByRole("region", { name: "数据工程" })
      .getByRole("button", { name: label, exact: true })
      .click();
    await expect(panel(page).locator(".node-description")).toHaveText(
      types.find((type) => type.type === nodeId)!.description,
    );
    await expect(
      panel(page).getByLabel("讲解说明", { exact: true }),
    ).toBeVisible();
    await panel(page)
      .getByLabel("讲解说明", { exact: true })
      .fill(`${label}的讲解`);
  }
  await select(page, "answer");
  await next(page, "下一步", "etl_profile");
  await select(page, "etl_profile");
  await next(page, "下一步", "etl_plan_check");
  await panel(page)
    .getByLabel("数据源", { exact: true })
    .selectOption("{{start.datasource}}");
  await panel(page)
    .getByLabel("表范围", { exact: true })
    .fill("retail.orders,\nretail.customers");
  await expect(panel(page)).toContainText(
    "schema.table 或 schema.*，逗号或换行分隔；留空为全部",
  );
  await panel(page).getByLabel("样本行数", { exact: true }).fill("20");
  await select(page, "etl_plan_check");
  await next(page, "通过", "databricks_ingest");
  await next(page, "重写", "answer");
  await insert(page, "模型回答", "{{answer.text}}");
  await insert(page, "探查结果", "{{etl_profile.tables}}");
  await insert(page, "代码语言", "{{start.language}}");
  await panel(page)
    .getByRole("button", { name: "添加知识库命中", exact: true })
    .click();
  await insert(page, "命中引用", "{{glossary.hits}}");
  for (let i = 1; i < 5; i++)
    await panel(page)
      .getByRole("button", { name: "添加知识库命中", exact: true })
      .click();
  await expect(
    panel(page).getByRole("button", { name: "添加知识库命中", exact: true }),
  ).toBeDisabled();
  for (let i = 5; i > 1; i--)
    await panel(page)
      .getByRole("button", { name: `删除知识库命中 ${i}`, exact: true })
      .click();
  await panel(page).getByLabel("最多连续重写次数", { exact: true }).fill("5");
  await select(page, "databricks_ingest");
  await next(page, "下一步", "etl_code_check");
  await panel(page)
    .getByLabel("数据源", { exact: true })
    .selectOption("sample-postgres");
  await insert(page, "表", "{{etl_plan_check.sources}}");
  await panel(page).getByLabel("每表最多行数", { exact: true }).fill("1000000");
  await expect(
    panel(page).getByRole("heading", { name: "试跑这个节点" }),
  ).toHaveCount(0);
  await select(page, "etl_code_check");
  await next(page, "通过", "databricks_dev_run");
  await next(page, "重写", "answer");
  await insert(page, "模型回答", "{{answer.text}}");
  await insert(page, "方案", "{{etl_plan_check.plan}}");
  await insert(page, "数据源名称", "{{etl_profile.source_name}}");
  await insert(page, "bundle 名", "{{start.name}}");
  await panel(page).getByLabel("最多连续重写次数", { exact: true }).fill("1");
  await select(page, "databricks_dev_run");
  await next(page, "通过", "end");
  await next(page, "带着报错重写", "answer");
  await next(page, "失败", "end");
  await insert(page, "bundle", "{{etl_code_check.bundle}}");
  await panel(page).getByLabel("最多运行次数", { exact: true }).fill("2");
  await panel(page).getByLabel("预览行数", { exact: true }).fill("0");
  await expect(
    panel(page).getByRole("heading", { name: "试跑这个节点" }),
  ).toHaveCount(0);
  await select(page, "end");
  await insert(page, "变量", "{{databricks_dev_run.report}}");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "草稿已保存" }),
  ).toBeVisible();
  const saved = (await detail(request, app)).draft!.config as FlowConfig;
  expect(
    saved.graph.nodes.filter((node) =>
      nodes.some(([, type]) => type === node.type),
    ),
  ).toHaveLength(5);
  expect(
    saved.graph.nodes.find((node) => node.id === "etl_plan_check"),
  ).toMatchObject({ hits: ["{{glossary.hits}}"], max_attempts: 5 });
  expect(
    saved.graph.nodes.find((node) => node.id === "databricks_dev_run"),
  ).toMatchObject({
    bundle: "{{etl_code_check.bundle}}",
    max_runs: 2,
    preview_rows: 0,
    note: "dev 运行的讲解",
  });
  await page.reload();
  for (const [label, nodeId] of nodes) {
    await select(page, nodeId);
    await expect(
      panel(page).getByLabel("讲解说明", { exact: true }),
    ).toHaveValue(`${label}的讲解`);
    await capture(page, `form-${nodeId}`, info);
  }
});

test("note is limited to 500 characters, stays literal through renames and appears in run details", async ({
  page,
  request,
}) => {
  const app = await create(request);
  await listView(page);
  await page.goto(`/apps/${app}`);
  await select(page, "answer");
  const note = panel(page).getByLabel("讲解说明", { exact: true });
  await note.fill("讲".repeat(501));
  await expect(note).toHaveValue("讲".repeat(500));
  await expect(panel(page)).toContainText("500 / 500 字");
  const literal =
    "说明里的 {{answer.text}} 和 {{secret.token}} 只给人看。\n第二行讲解。";
  await note.fill(literal);
  await panel(page).getByLabel("标识", { exact: true }).fill("reply");
  await panel(page).getByLabel("标识", { exact: true }).press("Tab");
  await expect(note).toHaveValue(literal);
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "草稿已保存" }),
  ).toBeVisible();
  const created = await request.post("/api/runs", {
    data: { app, draft: true, input: { question: "说明测试" } },
  });
  const { run_id } = await created.json();
  await page.goto(`/runs/${run_id}`);
  const flow = page.getByRole("region", { name: "工作流程" });
  await flow
    .getByRole("group", { name: "流程图", exact: true })
    .getByRole("button", { name: /^回答/ })
    .click();
  await expect(
    flow.getByRole("region", { name: "讲解说明", exact: true }),
  ).toContainText(literal);
  await flow
    .getByRole("group", { name: "流程图", exact: true })
    .getByRole("button", { name: /^结束/ })
    .click();
  await expect(
    flow.getByRole("region", { name: "讲解说明", exact: true }),
  ).toHaveCount(0);
  const runManual: AppManual = await (
    await request.get(`/api/runs/${run_id}/manual`)
  ).json();
  expect(runManual.version).toBeNull();
  expect(runManual.steps.find((node) => node.id === "reply")?.note).toBe(
    literal,
  );
});

test("flow manual shows ordered steps, resource groups, documents and working resource links", async ({
  page,
  request,
}, info) => {
  const app = await create(request, { app: "retail-etl" });
  await page.goto(`/apps/${app}`);
  const book = manual(page);
  await expect(book).toHaveAttribute("open", "");
  await expect(book.locator(".manual-steps > li")).toHaveCount(12);
  await expect(book.locator(".manual-steps > li").first()).toContainText(
    "开始",
  );
  for (const name of [
    "节点讲解",
    "Skill",
    "知识库",
    "数据源",
    "模型",
    "外部系统",
    "人工确认点",
  ])
    await expect(book.getByRole("region", { name, exact: true })).toBeVisible();
  await book
    .getByRole("region", { name: "知识库", exact: true })
    .getByText("文档文件名列表")
    .first()
    .click();
  await expect(
    book.getByText("01-有效订单定义.md", { exact: true }),
  ).toBeVisible();
  await expect(book).toContainText("6 篇文档");
  await expect(book.getByRole("region", { name: "外部系统" })).toContainText(
    "源表 catalog demo_source；结果写到 demo.analytics\n运行方式：Job（模拟集群）",
  );
  await capture(page, "app-manual", info);
  await book.getByRole("link", { name: "etl-plan", exact: true }).click();
  await expect(page).toHaveURL("/skills/etl-plan");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "etl-plan",
  );
  await page.goto(`/apps/${app}`);
  await manual(page)
    .getByRole("link", { name: "零售数据口径", exact: true })
    .click();
  await expect(page).toHaveURL("/knowledge/retail-metrics");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "零售数据口径",
  );
});

test("manual switches between saved draft and effective config; code apps share the panel", async ({
  page,
  request,
}) => {
  const app = await create(request, { app: "retail-etl" });
  const config = (await detail(request, app)).current.config as FlowConfig;
  config.graph.nodes.find((node) => node.id === "profile")!.note =
    "只有草稿里的讲解";
  await request.put(`/api/apps/${app}/draft`, { data: { config } });
  await page.goto(`/apps/${app}`);
  const book = manual(page);
  await expect(book).toContainText("生效版本 v1");
  await expect(book).not.toContainText("只有草稿里的讲解");
  await book.getByRole("button", { name: "看草稿", exact: true }).click();
  await expect(book).toContainText("只有草稿里的讲解");
  await book.getByRole("button", { name: "看生效版本", exact: true }).click();
  await expect(book).not.toContainText("只有草稿里的讲解");
  await page.goto("/apps/etl");
  await expect(manual(page)).toHaveAttribute("open", "");
  await expect(
    manual(page).getByRole("region", { name: "Skill", exact: true }),
  ).toContainText("etl-codegen");
  await expect(
    manual(page).getByRole("region", { name: "外部系统", exact: true }),
  ).toContainText("Databricks");
  await page.goto(`/apps/${await create(request)}`);
  await expect(
    manual(page).getByRole("region", { name: "Skill", exact: true }),
  ).toHaveCount(0);
  await expect(
    manual(page).getByRole("region", { name: "知识库", exact: true }),
  ).toHaveCount(0);
});

test("run manual starts collapsed, links to resources and stays pinned to its version", async ({
  page,
  request,
}, info) => {
  await page.goto("/runs/retail-etl-demo");
  const book = manual(page);
  await expect(book).not.toHaveAttribute("open");
  await book.locator(":scope > summary").click();
  await expect(
    book.getByRole("region", { name: "节点讲解", exact: true }),
  ).toBeVisible();
  await capture(page, "run-manual", info);
  await book.getByRole("link", { name: "etl-codegen", exact: true }).click();
  await expect(page).toHaveURL("/skills/etl-codegen");
  await page.goto("/runs/retail-etl-demo");
  await manual(page).locator(":scope > summary").click();
  await manual(page)
    .getByRole("link", { name: "数仓开发规范", exact: true })
    .click();
  await expect(page).toHaveURL("/knowledge/dw-standards");
  const app = await create(request);
  const created = await request.post("/api/runs", {
    data: { app, input: { question: "固定版本" } },
  });
  const { run_id } = await created.json();
  const before = await (await request.get(`/api/runs/${run_id}/manual`)).json();
  const config = (await detail(request, app)).current.config as FlowConfig;
  config.graph.nodes[1].note = "后来的版本";
  await request.put(`/api/apps/${app}/draft`, { data: { config } });
  await request.post(`/api/apps/${app}/draft/publish`, {
    data: { note: "发布新版本" },
  });
  expect(
    await (await request.get(`/api/runs/${run_id}/manual`)).json(),
  ).toEqual(before);
});

test("Markdown report renders real tables with citations, and dev output has per-table previews above JSON", async ({
  page,
}, info) => {
  await page.goto("/runs/retail-etl-demo");
  const result = page.getByRole("region", { name: "运行结果", exact: true });
  expect(
    await result.evaluate((element) => Boolean(element.closest(".run-main"))),
  ).toBe(true);
  await expect(result.getByRole("table")).toHaveCount(3);
  await expect(
    result.getByRole("columnheader", { name: "销售额", exact: true }),
  ).toBeVisible();
  await expect(
    result.getByRole("cell", { name: "128560.50", exact: true }),
  ).toBeVisible();
  await result
    .getByRole("link", { name: "引用来源 1", exact: true })
    .first()
    .click();
  await expect(
    page.locator(".citation-source.highlighted").first(),
  ).toContainText("01-有效订单定义.md");
  await capture(page, "run-report", info);
  await page.setViewportSize({ width: 320, height: 840 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1360, height: 960 });
  const flow = page.getByRole("region", { name: "工作流程", exact: true });
  await flow
    .getByRole("group", { name: "流程图", exact: true })
    .getByRole("button", { name: /^开始/ })
    .click();
  await expect(
    flow.getByRole("region", { name: "讲解说明", exact: true }),
  ).toContainText("选择数据源，填写需求");
  await flow
    .getByRole("group", { name: "流程图", exact: true })
    .getByRole("button", { name: /^结束/ })
    .click();
  await expect(
    flow.getByRole("region", { name: "讲解说明", exact: true }),
  ).toContainText("返回运行报告");
  await flow
    .getByRole("list", { name: "本次运行的步骤" })
    .getByRole("button", { name: /dev 运行/ })
    .click();
  const step = flow.getByRole("region", { name: "节点 dev 运行", exact: true });
  await expect(step.getByRole("region", { name: "讲解说明" })).toContainText(
    "在 dev 验证结果行数",
  );
  const preview = step.getByRole("region", { name: "结果表预览", exact: true });
  await expect(preview.getByRole("table")).toHaveCount(2);
  await expect(preview).toContainText("共 96 行 · 预览 2 行");
  await expect(
    preview
      .getByRole("table", { name: "dim_retail_customers 预览" })
      .getByRole("cell", { name: "null", exact: true }),
  ).toBeVisible();
  await expect(step.locator(".flow-json").first()).toContainText('"report"');
  expect(
    await preview.evaluate((element) =>
      Boolean(
        element.compareDocumentPosition(
          element.parentElement!.querySelector("pre")!,
        ) & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ),
  ).toBe(true);
  await capture(page, "dev-preview", info);
});

test("canvas uses manual resource tags, limits them to three and displays note on selection", async ({
  page,
  request,
}) => {
  const app = await create(request, { app: "retail-etl" });
  await page.route(`**/api/apps/${app}/manual`, async (route) => {
    const response = await route.fetch();
    const book: AppManual = await response.json();
    book.steps.find((step) => step.id === "profile")!.uses = [
      "Skill etl-plan v1",
      "知识库 零售数据口径",
      "数据源 示例库",
      "Databricks",
      "模型 mock",
    ];
    await route.fulfill({ json: book });
  });
  await page.goto(`/apps/${app}`);
  const canvas = page.getByRole("group", { name: "编排画布", exact: true });
  const card = canvas.locator('.react-flow__node[data-id="profile"]');
  await expect(card.locator(".uses-tag")).toHaveText([
    "Skill etl-plan v1",
    "知识库 零售数据口径",
    "数据源 示例库",
    "+1",
  ]);
  await expect(
    canvas.locator('.react-flow__node[data-id="start"] .uses-tags'),
  ).toHaveCount(0);
  await card.click();
  await expect(panel(page).getByLabel("讲解说明", { exact: true })).toHaveValue(
    "读取表结构和样本，先了解真实数据中的空值和异常取值。",
  );
});

test("numeric limits, no-preview output and Markdown code/links retain their behavior", async ({
  page,
  request,
}) => {
  const app = await create(request, { app: "retail-etl" });
  const config = (await detail(request, app)).current.config as FlowConfig;
  const node = config.graph.nodes.find(
    (node) => node.type === "databricks_dev_run",
  )!;
  if (node.type !== "databricks_dev_run") throw new Error("missing dev run");
  node.max_runs = 4;
  node.preview_rows = 101;
  const bad = await request.put(`/api/apps/${app}/draft`, { data: { config } });
  expect(bad.status()).toBe(422);
  expect((await bad.json()).detail).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        loc: ["body", "config", "graph", "nodes", "dev_run", "max_runs"],
      }),
      expect.objectContaining({
        loc: ["body", "config", "graph", "nodes", "dev_run", "preview_rows"],
      }),
    ]),
  );
  for (const nodeId of ["ingest", "dev_run"])
    expect(
      (
        await request.post(`/api/apps/${app}/nodes/${nodeId}/test`, {
          data: { values: {} },
        })
      ).status(),
    ).toBe(400);
  await page.route("**/api/runs/retail-etl-demo/flow", async (route) => {
    const response = await route.fetch();
    const flow = await response.json();
    flow.steps.find(
      (step: { node: string }) => step.node === "dev_run",
    ).output.preview = [];
    await route.fulfill({ json: flow });
  });
  await page.route("**/api/runs/retail-etl-demo", async (route) => {
    const response = await route.fetch();
    const run = await response.json();
    run.result = {
      report:
        "| 指标 | 值 |\n| :--- | ---: |\n| **销售额** [1] | `a|b` |\n| 备注 | a\\|b |\n\n```text\n| 代码 | 原文 |\n| --- | --- |\n```\n\n[普通链接 [1]](https://example.com)\n\n<script>window.untrusted = true</script>",
      nested: { report: "**嵌套报告**" },
    };
    await route.fulfill({ json: run });
  });
  await page.goto("/runs/retail-etl-demo");
  const result = page.getByRole("region", { name: "运行结果", exact: true });
  await expect(result.getByRole("table")).toHaveCount(1);
  await expect(
    result.getByRole("cell", { name: "a|b", exact: true }),
  ).toHaveCount(2);
  await expect(
    result.getByRole("table").locator(".citation-marker"),
  ).toHaveCount(1);
  await expect(result.locator("pre code")).toContainText("| 代码 | 原文 |");
  await expect(result.locator('a[href="https://example.com"]')).toHaveText(
    "普通链接 [1]",
  );
  await expect(
    result.locator("strong").filter({ hasText: "嵌套报告" }),
  ).toBeVisible();
  expect(await page.evaluate(() => "untrusted" in window)).toBe(false);
  const flow = page.getByRole("region", { name: "工作流程", exact: true });
  await flow
    .getByRole("list", { name: "本次运行的步骤" })
    .getByRole("button", { name: /dev 运行/ })
    .click();
  await expect(
    flow.getByRole("region", { name: "结果表预览", exact: true }),
  ).toHaveCount(0);
});
