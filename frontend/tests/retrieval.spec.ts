import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { mkdir } from "node:fs/promises";
import type {
  AgentNode,
  AppDetail,
  FlowConfig,
  KnowledgeNode,
} from "../lib/apps";
import type { Hit, KnowledgeOptions, Retrieval } from "../lib/knowledge";

test.describe.configure({ timeout: 90_000 });
test.use({ actionTimeout: 15_000 });
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
});

const unique = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });
const nodeButton = (page: Page, title: string, id: string) =>
  page
    .getByRole("complementary", { name: "节点列表" })
    .getByRole("button", { name: `${title}（${id}）`, exact: true });
const searchPath = "/api/knowledge-bases/policies/search";
const hybrid: Retrieval = { mode: "hybrid", vector_weight: 0.7, rerank: null };
async function details(
  request: APIRequestContext,
  app: string,
): Promise<AppDetail> {
  return (await request.get(`/api/apps/${app}`)).json();
}
async function createTemplate(request: APIRequestContext, template: string) {
  const app = unique(template);
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title: "检索验收", from: { template } },
      })
    ).status(),
  ).toBe(201);
  const data = await details(request, app);
  return { app, config: data.draft!.config as FlowConfig };
}
async function save(page: Page, app: string) {
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/apps/${app}/draft`) &&
      response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  expect((await saved).status()).toBe(200);
  await expect(
    page.getByText("草稿已保存，上次评测结果已清空。", { exact: true }),
  ).toBeVisible();
}
async function capture(page: Page, name: string) {
  // CI's existing screenshot artifact collects these; generated images stay out of Git.
  await mkdir("docs/screenshots/issue-77", { recursive: true });
  const target =
    name === "retrieval-test"
      ? page.getByRole("region", { name: "检索测试", exact: true })
      : panel(page);
  await target.screenshot({
    path: `docs/screenshots/issue-77/${name}.png`,
    animations: "disabled",
  });
}
async function openSearch(page: Page) {
  await page.goto("/knowledge/policies");
  await page.getByRole("tab", { name: "检索测试" }).click();
  await page.getByLabel("问题", { exact: true }).fill("退换货期限");
}

test("检索测试：options 默认值、三种方式、权重、两类重排模型与分数明细", async ({
  page,
  request,
}) => {
  const options: KnowledgeOptions = await (
    await request.get("/api/knowledge-bases/options")
  ).json();
  expect(options.retrieval).toEqual(hybrid);
  expect(options.rerank_models).toEqual(
    expect.arrayContaining([
      { model: "mock-rerank", kind: "rerank" },
      { model: "mock", kind: "llm" },
    ]),
  );
  await openSearch(page);
  const form = page.getByRole("region", { name: "检索测试", exact: true });
  const results = page.getByRole("region", { name: "检索结果", exact: true });
  await expect(
    form.getByRole("button", { name: "混合", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  const slider = form.getByRole("slider", { name: "向量权重" });
  await expect(slider).toHaveAttribute("min", "0");
  await expect(slider).toHaveAttribute("max", "1");
  await expect(slider).toHaveAttribute("step", "0.05");
  await slider.focus();
  await slider.press("ArrowRight");
  await expect(form).toContainText("向量 0.75 · 关键词 0.25");
  await slider.press("ArrowLeft");
  await expect(form).toContainText("向量 0.70 · 关键词 0.30");

  for (const [mode, label, help] of [
    ["hybrid", "混合", "向量和关键词加权"],
    ["vector", "向量", "按意思找"],
    ["keyword", "关键词", "按词找，不调向量模型"],
  ] as const) {
    await form.getByRole("button", { name: label, exact: true }).click();
    await expect(form).toContainText(help);
    await expect(form.getByRole("slider")).toHaveCount(
      mode === "hybrid" ? 1 : 0,
    );
    const response = page.waitForResponse((res) =>
      res.url().endsWith(searchPath),
    );
    await form.getByRole("button", { name: "检索", exact: true }).click();
    const res = await response;
    expect(res.request().postDataJSON().retrieval).toEqual({ ...hybrid, mode });
    const { hits }: { hits: Hit[] } = await res.json();
    expect(hits).toHaveLength(5);
    const first = results.locator("article").first();
    await expect(first).toContainText(`最终分数 ${hits[0].score.toFixed(2)}`);
    for (const [key, name] of [
      ["vector", "向量"],
      ["keyword", "关键词"],
      ["rerank", "重排"],
    ] as const) {
      const score = hits[0].scores[key];
      expect(score === null).toBe(
        key === "rerank" ||
          (key === "vector" && mode === "keyword") ||
          (key === "keyword" && mode === "vector"),
      );
      if (score === null)
        await expect(first.getByLabel("分数明细")).not.toContainText(name);
      else
        await expect(first.getByLabel("分数明细")).toContainText(
          `${name} ${score.toFixed(2)}`,
        );
    }
  }
  await form.getByRole("button", { name: "混合", exact: true }).click();
  await form.getByRole("switch", { name: "重排", exact: true }).check();
  const model = form.getByLabel("重排模型", { exact: true });
  await expect(model.locator("optgroup").nth(0)).toHaveAttribute(
    "label",
    "重排模型",
  );
  await expect(model.locator("optgroup").nth(1)).toHaveAttribute(
    "label",
    "用对话模型打分",
  );
  for (const alias of ["mock-rerank", "mock"]) {
    await model.selectOption(alias);
    const response = page.waitForResponse((res) =>
      res.url().endsWith(searchPath),
    );
    await form.getByRole("button", { name: "检索", exact: true }).click();
    const res = await response;
    expect(res.request().postDataJSON().retrieval.rerank).toEqual({
      model: alias,
      candidates: 20,
    });
    const { hits }: { hits: Hit[] } = await res.json();
    expect(hits.every((hit) => hit.score === hit.scores.rerank)).toBe(true);
    const first = results.locator("article").first();
    await expect(first).toContainText("最终分数 0.91");
    await expect(first.getByLabel("分数明细")).toHaveText([
      "向量 0.93关键词 0.75重排 0.91",
    ]);
  }
  await capture(page, "retrieval-test");
});

test("没有可选模型时禁用重排并解释原因", async ({ page }) => {
  await page.route("**/api/knowledge-bases/options", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: { ...(await response.json()), rerank_models: [] },
    });
  });
  await openSearch(page);
  await expect(
    page.getByRole("switch", { name: "重排", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("没有可选的重排模型，暂时无法开启重排。"),
  ).toBeVisible();
});

test("检索测试：重排候选数前端校验，502 在结果区显示 detail 并清除旧命中", async ({
  page,
}) => {
  await openSearch(page);
  let requests = 0;
  page.on("request", (req) => {
    if (req.url().endsWith(searchPath)) requests++;
  });
  await page.getByRole("switch", { name: "重排", exact: true }).check();
  await page.getByLabel("top_k", { exact: true }).fill("10");
  const candidates = page.getByLabel("重排候选数", { exact: true });
  await candidates.fill("5");
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "重排候选数不能少于 top_k",
  );
  await expect(
    page.getByRole("button", { name: "检索", exact: true }),
  ).toBeDisabled();
  expect(requests).toBe(0);
  for (const value of ["4", "51", "10.5"]) {
    await candidates.fill(value);
    await expect(page.getByRole("main").getByRole("alert")).toContainText(
      "重排候选数须为 5–50 的整数",
    );
  }
  await candidates.fill("10");
  await page.getByRole("button", { name: "检索", exact: true }).click();
  const results = page.getByRole("region", { name: "检索结果" });
  await expect(results.locator("article")).toHaveCount(10);
  await page.getByLabel("问题", { exact: true }).fill("重排失败");
  const response = page.waitForResponse((res) =>
    res.url().endsWith(searchPath),
  );
  await page.getByRole("button", { name: "检索", exact: true }).click();
  const res = await response;
  expect(res.status()).toBe(502);
  await expect(results.getByRole("alert")).toContainText(
    (await res.json()).detail,
  );
  await expect(results.locator("article")).toHaveCount(0);
});

test("知识检索节点：旧配置按向量显示，修改后写回完整 retrieval 并保存草稿", async ({
  page,
  request,
}) => {
  const { app, config } = await createTemplate(request, "knowledge-qa");
  const kb = config.graph.nodes.find(
    (node) => node.type === "knowledge",
  ) as KnowledgeNode;
  kb.knowledge_bases = ["policies"];
  delete kb.retrieval;
  expect(
    (
      await request.put(`/api/apps/${app}/draft`, { data: { config } })
    ).status(),
  ).toBe(200);
  await page.goto(`/apps/${app}`);
  await nodeButton(page, "知识检索", "kb").click();
  const form = panel(page);
  await expect(
    form.getByRole("button", { name: "向量", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(form.getByRole("slider")).toHaveCount(0);
  await expect(
    form.getByRole("switch", { name: "重排", exact: true }),
  ).not.toBeChecked();
  await form.getByRole("button", { name: "关键词", exact: true }).click();
  await save(page, app);
  expect(
    (
      (await details(request, app)).draft!.config as FlowConfig
    ).graph.nodes.find((node) => node.id === "kb"),
  ).toMatchObject({
    retrieval: { mode: "keyword", vector_weight: 0.7, rerank: null },
  });
  await page.reload();
  await nodeButton(page, "知识检索", "kb").click();
  await expect(
    form.getByRole("button", { name: "关键词", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await form.getByRole("switch", { name: "重排", exact: true }).check();
  await form.getByLabel("重排模型", { exact: true }).selectOption("mock");
  await save(page, app);
  await capture(page, "knowledge-node");
});

for (const type of ["knowledge", "agent"] as const) {
  test(`${type} 节点：候选数小于 top_k 时在前端阻止保存，缺模型时禁用重排`, async ({
    page,
    request,
  }) => {
    const { app, config } = await createTemplate(
      request,
      type === "knowledge" ? "knowledge-qa" : "knowledge-agent",
    );
    const node = config.graph.nodes.find((item) => item.type === type) as
      | KnowledgeNode
      | AgentNode;
    const knowledge = node.type === "knowledge" ? node : node.knowledge!;
    Object.assign(knowledge, { knowledge_bases: ["policies"], top_k: 10 });
    await request.put(`/api/apps/${app}/draft`, { data: { config } });
    await page.goto(`/apps/${app}`);
    await nodeButton(page, node.title, node.id).click();
    await panel(page)
      .getByRole("switch", { name: "重排", exact: true })
      .check();
    await panel(page).getByLabel("重排候选数", { exact: true }).fill("5");
    let requests = 0;
    page.on("request", (req) => {
      if (req.method() === "PUT" && req.url().endsWith(`/apps/${app}/draft`))
        requests++;
    });
    await page.getByRole("button", { name: "保存草稿", exact: true }).click();
    await expect(panel(page).getByRole("alert")).toContainText(
      "重排候选数不能少于 top_k",
    );
    await expect(
      page.getByRole("main").getByRole("alert").first(),
    ).toContainText("草稿没有保存");
    expect(requests).toBe(0);
    await panel(page).getByLabel("重排候选数", { exact: true }).fill("10");
    await save(page, app);
    // Reload server data without models, preserving an existing configured node.
    await page.route(`**/api/apps/${app}`, async (route) => {
      const response = await route.fetch();
      const data: AppDetail = await response.json();
      await route.fulfill({
        json: { ...data, options: { ...data.options, rerank_models: [] } },
      });
    });
    await page.reload();
    await nodeButton(page, node.title, node.id).click();
    await expect(
      panel(page).getByRole("switch", { name: "重排", exact: true }),
    ).toBeDisabled();
    await expect(panel(page)).toContainText("没有可选的重排模型");
  });
}

test("knowledge-agent 模板：直接进入草稿编辑，选知识库、移除全部数据源工具也能保存", async ({
  page,
  request,
}) => {
  const app = unique("agent-ui");
  await page.goto("/apps");
  await page.getByRole("button", { name: "新建应用", exact: true }).click();
  await page.getByRole("radio", { name: /知识库智能体/ }).check();
  await page.getByRole("button", { name: "下一步", exact: true }).click();
  await page.getByLabel("标识", { exact: true }).fill(app);
  await page.getByLabel("名称", { exact: true }).fill("知识库智能体验收");
  await page.getByRole("button", { name: "创建应用", exact: true }).click();
  await expect(page).toHaveURL(`/apps/${app}`);
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toContainText("正在查看：草稿");
  const data = await details(request, app);
  expect(
    (data.current.config as FlowConfig).graph.nodes.some(
      (node) => node.type === "agent",
    ),
  ).toBe(false);
  const config = data.draft!.config as FlowConfig;
  expect(
    config.graph.nodes.find((node) => node.type === "agent"),
  ).toMatchObject({
    tools: [],
    max_steps: 6,
    knowledge: { knowledge_bases: [], retrieval: hybrid },
  });
  await nodeButton(page, "知识库智能体", "agent").click();
  const form = panel(page);
  await expect(
    form.getByRole("switch", { name: "让智能体检索知识库" }),
  ).toBeChecked();
  await form.getByRole("checkbox", { name: /售后政策/ }).check();
  const datasource = form
    .getByRole("group", { name: "允许使用的工具" })
    .getByRole("checkbox")
    .first();
  await datasource.check();
  await datasource.uncheck();
  await form.getByLabel("top_k", { exact: true }).fill("2");
  await expect(form.getByLabel("top_k", { exact: true })).toHaveAttribute(
    "max",
    "10",
  );
  await form.getByLabel("min_score", { exact: true }).fill("0.2");
  await form.getByRole("button", { name: "混合", exact: true }).click();
  await form.getByRole("switch", { name: "重排", exact: true }).check();
  await save(page, app);
  expect(
    (
      (await details(request, app)).draft!.config as FlowConfig
    ).graph.nodes.find((node) => node.type === "agent"),
  ).toMatchObject({
    tools: [],
    knowledge: {
      knowledge_bases: ["policies"],
      top_k: 2,
      min_score: 0.2,
      retrieval: {
        ...hybrid,
        rerank: { model: "mock-rerank", candidates: 20 },
      },
    },
  });
  await capture(page, "agent-node");
  // Turning knowledge off writes null; both empty choices are rejected locally and by the mock API.
  await form.getByRole("switch", { name: "让智能体检索知识库" }).uncheck();
  await expect(
    form.getByText("至少选一个数据源工具或知识库", { exact: true }),
  ).toBeVisible();
  await datasource.check();
  await save(page, app);
  const saved = (await details(request, app)).draft!.config as FlowConfig;
  const agent = saved.graph.nodes.find(
    (node) => node.type === "agent",
  ) as AgentNode;
  expect(agent.knowledge).toBeNull();
  await datasource.uncheck();
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert").first()).toContainText(
    "草稿没有保存",
  );
  agent.tools = [];
  const rejected = await request.put(`/api/apps/${app}/draft`, {
    data: { config: saved },
  });
  expect(rejected.status()).toBe(422);
  expect(await rejected.text()).toContain("至少选一个数据源工具或知识库");
});

test("新增节点默认值和 agent.hits 后续节点插入变量", async ({
  page,
  request,
}) => {
  const types = await (await request.get("/api/node-types")).json();
  expect(
    types.find((item: { type: string }) => item.type === "knowledge").defaults
      .retrieval,
  ).toEqual(hybrid);
  const agentType = types.find(
    (item: { type: string }) => item.type === "agent",
  );
  expect(agentType.defaults.knowledge).toBeNull();
  expect(agentType.outputs).toContainEqual({
    key: "hits",
    description: "hits",
  });
  const { app, config } = await createTemplate(request, "knowledge-agent");
  const agent = config.graph.nodes.find(
    (node) => node.type === "agent",
  ) as AgentNode;
  agent.knowledge!.knowledge_bases = ["policies"];
  delete agent.knowledge!.retrieval;
  config.graph.nodes.splice(2, 0, {
    id: "format",
    type: "template",
    title: "整理引用",
    template: "",
  });
  config.graph.edges = [
    { from: "start", to: "agent" },
    { from: "agent", to: "format" },
    { from: "format", to: "end" },
  ];
  expect(
    (
      await request.put(`/api/apps/${app}/draft`, { data: { config } })
    ).status(),
  ).toBe(200);
  await page.goto(`/apps/${app}`);
  await nodeButton(page, "知识库智能体", "agent").click();
  await expect(
    panel(page).getByRole("button", { name: "向量", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await panel(page)
    .getByRole("button", { name: "关键词", exact: true })
    .click();
  await nodeButton(page, "整理引用", "format").click();
  await panel(page)
    .getByRole("button", { name: "插入变量：模板", exact: true })
    .click();
  await panel(page)
    .getByRole("button", { name: "{{agent.hits}}", exact: true })
    .click();
  await expect(panel(page).getByLabel("模板", { exact: true })).toHaveValue(
    "{{agent.hits}}",
  );
  await save(page, app);
  expect(
    (
      (await details(request, app)).draft!.config as FlowConfig
    ).graph.nodes.find((node) => node.type === "agent"),
  ).toMatchObject({ knowledge: { retrieval: { ...hybrid, mode: "keyword" } } });
});

test("运行详情：智能体检索步骤、统一引用来源和检索失败步骤", async ({
  page,
  request,
}) => {
  const { app, config } = await createTemplate(request, "knowledge-agent");
  const agent = config.graph.nodes.find(
    (node) => node.type === "agent",
  ) as AgentNode;
  agent.knowledge = {
    knowledge_bases: ["policies"],
    top_k: 2,
    min_score: 0.3,
    retrieval: { ...hybrid, rerank: { model: "mock-rerank", candidates: 20 } },
  };
  config.graph.nodes.splice(1, 0, {
    id: "kb",
    type: "knowledge",
    title: "先检索",
    knowledge_bases: ["policies"],
    top_k: 1,
    min_score: 0.3,
    query: "{{start.question}}",
    retrieval: hybrid,
  });
  config.graph.edges = [
    { from: "start", to: "kb" },
    { from: "kb", to: "agent" },
    { from: "agent", to: "end" },
  ];
  expect(
    (
      await request.put(`/api/apps/${app}/draft`, { data: { config } })
    ).status(),
  ).toBe(200);
  expect(
    (
      await request.post(`/api/apps/${app}/draft/publish`, {
        data: { note: "检索验收" },
      })
    ).status(),
  ).toBe(200);
  const created = await request.post("/api/runs", {
    data: { app, input: { question: "退换货期限" } },
  });
  const { run_id } = await created.json();
  await page.goto(`/runs/${run_id}`);
  const sources = page.getByRole("region", { name: "引用来源", exact: true });
  await expect(sources.locator("details")).toHaveCount(2);
  const run = await (await request.get(`/api/runs/${run_id}`)).json();
  expect(
    run.sources.map((source: { index: number; node: string }) => [
      source.index,
      source.node,
    ]),
  ).toEqual([
    [1, "agent"],
    [2, "agent"],
  ]);
  await page
    .getByRole("list", { name: "本次运行的步骤" })
    .getByRole("button", { name: /知识库智能体/ })
    .click();
  await expect(page.getByRole("list", { name: "智能体步骤" })).toHaveText(
    "检索知识库：退换货期限（命中 2 条）",
  );
  await page
    .locator(".result-card")
    .getByRole("link", { name: "引用来源 2", exact: true })
    .click();
  await expect(sources.locator("details").nth(1)).toBeFocused();
  await expect(sources.locator("details").nth(1)).toHaveAttribute("open", "");
  await expect(
    sources.locator("details").nth(1).getByRole("link", { name: "下载原文件" }),
  ).toHaveAttribute(
    "href",
    /knowledge-bases\/policies\/documents\/return-policy\/file$/,
  );
  const flow = await (await request.get(`/api/runs/${run_id}/flow`)).json();
  expect(
    flow.steps
      .find((step: { node: string }) => step.node === "agent")
      .output.hits.map((hit: { index: number }) => hit.index),
  ).toEqual([1, 2]);
  const failed = await request.post("/api/runs", {
    data: { app, input: { question: "重排失败" } },
  });
  const failedId = (await failed.json()).run_id;
  await page.goto(`/runs/${failedId}`);
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  await page
    .getByRole("list", { name: "本次运行的步骤" })
    .getByRole("button", { name: /知识库智能体/ })
    .click();
  const steps = page.getByRole("list", { name: "智能体步骤" });
  await expect(steps).toContainText("检索知识库：重排失败（命中 0 条）");
  await expect(steps).toContainText("重排失败：模拟重排服务不可用，请稍后重试");
  expect(
    (await (await request.get(`/api/runs/${failedId}`)).json()).status,
  ).toBe("done");
});

test("ETL knowledge_mode 使用普通 enum 表单显示并保存", async ({
  page,
  request,
}) => {
  await page.goto("/apps/etl");
  await page
    .getByRole("group", { name: "流程图", exact: true })
    .getByRole("button", { name: /^出方案/ })
    .click();
  const settings = page.getByRole("region", {
    name: "节点 出方案",
    exact: true,
  });
  await expect(settings.getByLabel("检索方式", { exact: true })).toHaveValue(
    "hybrid",
  );
  await settings
    .getByLabel("检索方式", { exact: true })
    .selectOption("keyword");
  try {
    await save(page, "etl");
    const data = await details(request, "etl");
    if (!("nodes" in data.draft!.config))
      throw new Error("Expected code config");
    expect(data.draft!.config.nodes.plan.params.knowledge_mode).toBe("keyword");
    await page.reload();
    await page
      .getByRole("group", { name: "流程图", exact: true })
      .getByRole("button", { name: /^出方案/ })
      .click();
    await expect(settings.getByLabel("检索方式", { exact: true })).toHaveValue(
      "keyword",
    );
  } finally {
    await request.delete("/api/apps/etl/draft");
  }
});
