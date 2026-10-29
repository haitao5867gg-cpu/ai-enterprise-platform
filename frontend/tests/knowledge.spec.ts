import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { mkdir } from "node:fs/promises";
import type { AppDetail, FlowConfig } from "../lib/apps";
import type { Document, KnowledgeBase } from "../lib/knowledge";

test.describe.configure({ timeout: 90_000 });
test.use({ actionTimeout: 15_000 });
// Exercise the existing list editor explicitly; desktop now defaults to canvas.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("aep.flow-editor.view", "list"));
});

const unique = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
const policy =
  "退换货期限为签收后七天。请保留原包装和购买凭证，申请通过后原路退还货款。";
async function capture(page: Page, name: string) {
  if (process.env.AEP_CAPTURE_KNOWLEDGE_SCREENSHOTS !== "1") return;
  await mkdir("docs/screenshots/issue-67", { recursive: true });
  await page.evaluate(() => {
    // A focused source below the fold can make Chromium scroll again when a
    // full-page capture resizes the viewport. Preserve the highlight, blur focus.
    if (document.activeElement instanceof HTMLElement)
      document.activeElement.blur();
  });
  await expect
    .poll(async () => {
      await page.evaluate(() => {
        window.scrollTo({ top: 0, behavior: "instant" });
        return new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        );
      });
      return page.evaluate(() => window.scrollY);
    })
    .toBe(0);
  await page.screenshot({
    path: `docs/screenshots/issue-67/${name}.png`,
    fullPage: true,
    animations: "disabled",
  });
}
async function createBase(
  request: APIRequestContext,
  name = unique("资料"),
): Promise<KnowledgeBase> {
  const response = await request.post("/api/knowledge-bases", {
    data: { name },
  });
  expect(response.status()).toBe(201);
  return response.json();
}
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });
const node = (page: Page, title: string, id: string) =>
  page
    .getByRole("complementary", { name: "节点列表" })
    .getByRole("button", { name: `${title}（${id}）`, exact: true });
async function details(
  request: APIRequestContext,
  app: string,
): Promise<AppDetail> {
  return (await request.get(`/api/apps/${app}`)).json();
}

test("新建、批量上传、两次轮询后可用、停止轮询、前端限制与设置", async ({
  page,
  request,
}) => {
  await page.goto("/knowledge");
  await page.getByRole("button", { name: "新建知识库" }).click();
  const dialog = page.getByRole("dialog", { name: "新建知识库" });
  await dialog.getByLabel("名称", { exact: true }).fill("售后政策");
  await dialog.getByRole("button", { name: "创建知识库" }).click();
  await expect(dialog.locator(".field-error")).toContainText(
    "知识库名称已存在",
  );
  const name = unique("交付资料");
  await dialog.getByLabel("名称", { exact: true }).fill(name);
  await dialog
    .getByLabel("说明", { exact: true })
    .fill("面向交付团队的业务政策与数据口径。");
  await expect(dialog.getByLabel("向量模型")).toHaveValue("mock-embed");
  await dialog.getByText("高级 · 文档切分").click();
  await dialog.getByLabel("切分重叠", { exact: true }).fill("401");
  await expect(
    dialog.getByRole("button", { name: "创建知识库" }),
  ).toBeDisabled();
  await dialog.getByLabel("切分重叠", { exact: true }).fill("100");
  await capture(page, "create");
  const created = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/knowledge-bases") &&
      response.request().method() === "POST" &&
      response.status() === 201,
  );
  await dialog.getByRole("button", { name: "创建知识库" }).click();
  const kb: KnowledgeBase = await (await created).json();
  await expect(dialog).not.toBeVisible();
  await capture(page, "list");
  await page.getByRole("link", { name: new RegExp(name) }).click();
  await expect(
    page.getByRole("heading", { name, exact: true, level: 1 }),
  ).toBeVisible();
  let posts = 0,
    lists = 0;
  page.on("request", (request) => {
    if (request.url().endsWith(`/knowledge-bases/${kb.kb_id}/documents`)) {
      if (request.method() === "POST") posts++;
      else if (request.method() === "GET") lists++;
    }
  });
  await page.getByLabel("上传文档", { exact: true }).setInputFiles({
    name: "virus.exe",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("no"),
  });
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "virus.exe：仅支持",
  );
  expect(posts).toBe(0);
  await page.getByLabel("上传文档", { exact: true }).setInputFiles({
    name: "too-large.txt",
    mimeType: "text/plain",
    buffer: Buffer.alloc(20 * 1024 * 1024 + 1),
  });
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "每个文件最多 20 MB",
  );
  expect(posts).toBe(0);
  await page.getByLabel("上传文档", { exact: true }).setInputFiles(
    Array.from({ length: 21 }, (_, i) => ({
      name: `${i}.txt`,
      mimeType: "text/plain",
      buffer: Buffer.from(policy),
    })),
  );
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "一次最多上传 20 个文件",
  );
  expect(posts).toBe(0);
  lists = 0;
  const uploaded = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/knowledge-bases/${kb.kb_id}/documents`) &&
      response.request().method() === "POST",
  );
  await page.getByLabel("上传文档", { exact: true }).setInputFiles([
    {
      name: "交付手册.md",
      mimeType: "text/markdown",
      buffer: Buffer.from(`# 交付规范\n\n${policy}`),
    },
    {
      name: "业务口径.TXT",
      mimeType: "text/plain",
      buffer: Buffer.from(policy),
    },
  ]);
  const uploadResponse = await uploaded;
  expect(uploadResponse.status()).toBe(202);
  expect(uploadResponse.request().headers()["content-type"]).toContain(
    "multipart/form-data; boundary=",
  );
  expect(
    (await uploadResponse.json()).every(
      (doc: Document) => doc.status === "queued",
    ),
  ).toBe(true);
  const table = page.getByRole("table", { name: "文档列表" });
  await expect(table.getByText("处理中", { exact: true })).toHaveCount(2);
  await expect(table.getByText("可用", { exact: true })).toHaveCount(2);
  expect(lists).toBe(3); // immediate refresh, then the two 3-second polls
  await page.waitForTimeout(3300); // verify the timer actually stops
  expect(lists).toBe(3);
  await capture(page, "documents");
  await table
    .getByRole("row")
    .filter({ hasText: "交付手册.md" })
    .getByRole("button", { name: "查看片段" })
    .click();
  await expect(page.getByRole("dialog")).toContainText("交付规范");
  await expect(page.getByRole("dialog")).toContainText(policy);
  await page.getByRole("dialog").getByRole("button", { name: "关闭" }).click();
  await page.getByRole("tab", { name: "检索测试", exact: true }).click();
  await page.getByLabel("问题", { exact: true }).fill("退换货期限");
  await page.getByRole("button", { name: "检索", exact: true }).click();
  await expect(page.getByText("2 条命中 · 耗时 12 ms")).toBeVisible();
  await page.getByRole("tab", { name: "设置", exact: true }).click();
  await expect(page.getByLabel("向量模型")).toBeDisabled();
  await expect(page.getByText("已有文档，不能更改向量模型。")).toBeVisible();
  await page.getByText("高级 · 文档切分").click();
  await expect(page.getByText("只影响之后上传或重建的文档")).toBeVisible();
  await page.getByLabel("切分大小", { exact: true }).fill("1000");
  await page.getByRole("button", { name: "保存设置" }).click();
  await expect(page.getByText("设置已保存")).toBeVisible();
  await capture(page, "settings");
  expect(
    (await (await request.get(`/api/knowledge-bases/${kb.kb_id}`)).json())
      .chunking.size,
  ).toBe(1000);
});

test("片段按 50 分页，检索命中定位到第二页片段", async ({ page }) => {
  await page.goto("/knowledge/policies");
  await page.getByRole("button", { name: "查看片段", exact: true }).click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.locator(".knowledge-chunk")).toHaveCount(50);
  await drawer.getByRole("button", { name: "下一页" }).click();
  await expect(drawer.locator(".knowledge-chunk")).toHaveCount(11);
  await expect(drawer).toContainText("片段 51");
  await drawer.getByRole("button", { name: "关闭" }).click();
  await page.getByRole("tab", { name: "检索测试" }).click();
  await page.getByLabel("问题", { exact: true }).fill("退换货期限是多久？");
  await page.getByRole("button", { name: "检索", exact: true }).click();
  await expect(page.getByText("5 条命中 · 耗时 12 ms")).toBeVisible();
  await expect(page.locator(".knowledge-score").first()).toHaveText("0.93");
  await capture(page, "search");
  await page
    .getByRole("button", { name: "退换货政策.pdf", exact: true })
    .first()
    .click();
  await expect(drawer).toContainText("第 2 / 2 页");
  const hit = drawer.locator('[data-position="55"]');
  await expect(hit).toBeFocused();
  await expect(hit).toHaveClass(/highlighted/);
  await expect(hit).toContainText("第 56 页");
  await capture(page, "chunks");
});

test("完整 mock 契约、上传原子校验、错误原因、重建和删除确认", async ({
  request,
  page,
}) => {
  const kb = await createBase(request);
  const path = `/api/knowledge-bases/${kb.kb_id}`;
  await page.goto(`/knowledge/${kb.kb_id}`);
  const rejected = await page.evaluate(
    async ({ path, policy }) => {
      const body = new FormData();
      body.append("files", new File([policy], "valid.txt"));
      body.append("files", new File(["invalid"], "bad.exe"));
      const response = await fetch(`${path}/documents`, {
        method: "POST",
        body,
      });
      return {
        status: response.status,
        detail: (await response.json()).detail,
      };
    },
    { path, policy },
  );
  expect(rejected.status).toBe(422);
  expect(rejected.detail).toContain("bad.exe");
  expect(await (await request.get(`${path}/documents`)).json()).toEqual([]);
  expect((await request.get("/api/knowledge-bases/options")).ok()).toBe(true);
  expect(
    (
      await request.patch(path, {
        data: { chunking: { size: 199, overlap: 0 } },
      })
    ).status(),
  ).toBe(422);
  const response = await request.post(`${path}/documents`, {
    multipart: {
      files: {
        name: "empty.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("empty"),
      },
    },
  });
  const [doc]: Document[] = await response.json();
  expect(
    (await request.get(`${path}/documents/${doc.document_id}`)).status(),
  ).toBe(200);
  expect(
    (
      await request.patch(path, { data: { embedding_model: "company-embed" } })
    ).status(),
  ).toBe(409);
  const download = await request.get(
    `${path}/documents/${doc.document_id}/file`,
  );
  expect(download.headers()["content-disposition"]).toContain("empty.txt");
  expect(await download.text()).toBe("empty");
  expect(
    (
      await request.get(`${path}/documents/${doc.document_id}/chunks?limit=201`)
    ).status(),
  ).toBe(422);
  expect(
    (
      await request.post(`${path}/search`, { data: { query: "x", top_k: 21 } })
    ).status(),
  ).toBe(422);
  await page.goto(`/knowledge/${kb.kb_id}`);
  await expect(page.getByRole("table", { name: "文档列表" })).toContainText(
    "没有可提取的文字（可能是扫描件）",
  );
  const reindexed = page.waitForResponse((response) =>
    response.url().endsWith(`/${doc.document_id}/reindex`),
  );
  await page.getByRole("button", { name: "重建", exact: true }).click();
  expect((await reindexed).status()).toBe(202);
  page.once("dialog", (dialog) => dialog.accept());
  const all = page.waitForResponse((response) =>
    response.url().endsWith(`${kb.kb_id}/reindex`),
  );
  await page.getByRole("button", { name: "全部重建" }).click();
  expect(await (await all).json()).toEqual({ queued: 1 });
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page.getByRole("table")).toContainText("empty.txt");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await expect(
    page.getByText("还没有文档，上传资料后即可检索。"),
  ).toBeVisible();
  await page.route(`**${path}/documents`, (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          status: 422,
          json: { detail: "坏文件.txt：文件编码无法识别" },
        })
      : route.continue(),
  );
  await page.getByLabel("上传文档", { exact: true }).setInputFiles({
    name: "坏文件.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(policy),
  });
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "坏文件.txt：文件编码无法识别",
  );
  await page.unroute(`**${path}/documents`);
  await page.getByRole("tab", { name: "设置", exact: true }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除知识库", exact: true }).click();
  await expect(page).toHaveURL("/knowledge");
  expect((await request.get(path)).status()).toBe(404);
});

test("knowledge-qa 创建后直接编辑，模板配置保留在草稿", async ({
  page,
  request,
}) => {
  const app = unique("knowledge-qa");
  await page.goto("/apps");
  await page.getByRole("button", { name: "新建应用" }).click();
  await page.getByRole("radio", { name: /知识问答/ }).check();
  await page.getByRole("button", { name: "下一步", exact: true }).click();
  await page.getByLabel("标识", { exact: true }).fill(app);
  await page.getByLabel("名称", { exact: true }).fill("政策知识问答");
  await page.getByRole("button", { name: "创建应用", exact: true }).click();
  await expect(page).toHaveURL(`/apps/${app}`);
  await expect(
    page.getByRole("region", { name: "编排", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toContainText("正在查看：草稿");
  const data = await details(request, app);
  expect(
    (data.current.config as FlowConfig).graph.nodes.some(
      (node) => node.type === "knowledge",
    ),
  ).toBe(false);
  const config = data.draft!.config as FlowConfig;
  expect(config.graph.nodes.find((node) => node.id === "kb")).toMatchObject({
    knowledge_bases: [],
    query: "{{start.question}}",
    top_k: 5,
    min_score: 0.3,
  });
  expect(config.graph.nodes.find((node) => node.id === "answer")).toMatchObject(
    { prompt: expect.stringContaining("{{kb.context}}") },
  );
  await node(page, "知识检索", "kb").click();
  await panel(page)
    .getByRole("checkbox", { name: /售后政策/ })
    .check();
  await expect(panel(page)).toContainText("1 个文档");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByText("草稿已保存，上次评测结果已清空。", { exact: true }),
  ).toBeVisible();
});

test("编辑器添加知识检索节点，连接后在下一个模型里插入 kb.context", async ({
  page,
  request,
}) => {
  const app = unique("knowledge-editor");
  await request.post("/api/apps", {
    data: { app, title: "知识检索编排", from: { blank: true } },
  });
  await page.goto(`/apps/${app}`);
  await page.getByRole("button", { name: "添加节点", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "知识检索", exact: true })
    .click();
  await panel(page).getByLabel("标识", { exact: true }).fill("kb");
  await panel(page).getByLabel("标识", { exact: true }).press("Enter");
  await panel(page)
    .getByRole("checkbox", { name: /售后政策/ })
    .check();
  await panel(page)
    .getByLabel("检索问题", { exact: true })
    .fill("{{start.question}}");
  await panel(page)
    .getByRole("group", { name: "下一步", exact: true })
    .getByLabel("下一步", { exact: true })
    .selectOption("answer");
  await node(page, "开始", "start").click();
  await panel(page)
    .getByRole("group", { name: "下一步", exact: true })
    .getByLabel("下一步", { exact: true })
    .selectOption("kb");
  await node(page, "回答", "answer").click();
  await panel(page).getByLabel("提示词", { exact: true }).fill("");
  await panel(page)
    .getByRole("button", { name: "插入变量：提示词", exact: true })
    .click();
  await expect(
    panel(page).getByRole("button", { name: "{{kb.hits}}", exact: true }),
  ).toBeVisible();
  await panel(page)
    .getByRole("button", { name: "{{kb.context}}", exact: true })
    .click();
  await expect(panel(page).getByLabel("提示词", { exact: true })).toHaveValue(
    "{{kb.context}}",
  );
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/apps/${app}/draft`) &&
      response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  expect((await saved).status()).toBe(200);
  await capture(page, "editor");
  expect(
    (
      (await details(request, app)).draft!.config as FlowConfig
    ).graph.nodes.find((node) => node.id === "answer"),
  ).toMatchObject({ prompt: "{{kb.context}}" });
});

test("运行引用全局编号、来源排序、Markdown 角标定位和当前版本删除冲突", async ({
  page,
  request,
}) => {
  const app = unique("citation-run");
  await request.post("/api/apps", {
    data: { app, title: "引用测试应用", from: { template: "knowledge-qa" } },
  });
  const data = await details(request, app),
    config = data.draft!.config as FlowConfig;
  const kbNode = config.graph.nodes.find((node) => node.type === "knowledge")!;
  Object.assign(kbNode, { knowledge_bases: ["policies"], top_k: 2 });
  config.graph.nodes.splice(2, 0, { ...kbNode, id: "kb2", title: "再次检索" });
  config.graph.edges = [
    { from: "start", to: "kb" },
    { from: "kb", to: "kb2" },
    { from: "kb2", to: "answer" },
    { from: "answer", to: "end" },
  ];
  const answer = config.graph.nodes.find((node) => node.type === "llm")!;
  if (answer.type === "llm")
    answer.prompt =
      "\n\n## 退货政策\n\n**七天内可申请退货** [1]。详情参见 [2]。\n\n`[1]` 是代码。";
  if (answer.type === "llm")
    answer.system = "根据资料回答：{{kb.context}}\n{{kb2.context}}";
  expect(
    (
      await request.put(`/api/apps/${app}/draft`, { data: { config } })
    ).status(),
  ).toBe(200);
  expect(
    (
      await request.post(`/api/apps/${app}/draft/publish`, {
        data: { note: "测试知识库引用" },
      })
    ).status(),
  ).toBe(200);
  const created = await request.post("/api/runs", {
    data: { app, input: { question: "退货期限" } },
  });
  const { run_id } = await created.json();
  await page.goto(`/runs/${run_id}`);
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  const run = await (await request.get(`/api/runs/${run_id}`)).json();
  expect(run.sources.map((source: { index: number }) => source.index)).toEqual([
    1, 2,
  ]);
  expect(
    run.sources.every(
      (source: { node_title: string }) => source.node_title === "再次检索",
    ),
  ).toBe(true);
  await page.route(`**/api/runs/${run_id}`, (route) =>
    route.fulfill({ json: { ...run, sources: [...run.sources].reverse() } }),
  );
  await page.reload();
  const sources = page.getByRole("region", { name: "引用来源", exact: true });
  await expect(sources.locator("summary").first()).toContainText("[1]");
  await expect(sources).toContainText("再次检索");
  await expect(page.locator(".result-card .markdown-content h2")).toHaveText(
    "退货政策",
  );
  await expect(page.locator(".result-card code").first()).toHaveText("[1]");
  await page
    .locator(".result-card .citation-marker")
    .getByRole("link", { name: "引用来源 2", exact: true })
    .first()
    .click();
  const source = sources.locator("details").nth(1);
  await expect(source).toHaveAttribute("open", "");
  await expect(source).toHaveClass(/highlighted/);
  await expect(source).toBeFocused();
  await expect(
    source.getByRole("link", { name: "下载原文件" }),
  ).toHaveAttribute(
    "href",
    /knowledge-bases\/policies\/documents\/return-policy\/file$/,
  );
  await capture(page, "run-sources");
  await page.goto("/knowledge/policies");
  await page.getByRole("tab", { name: "设置", exact: true }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除知识库", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    `引用测试应用（${app}）`,
  );
  await page.route(`**/api/runs/${run_id}`, (route) =>
    route.fulfill({
      json: { ...run, sources: [], result: { answer: "无来源时保留 [1]" } },
    }),
  );
  await page.goto(`/runs/${run_id}`);
  await expect(page.locator(".result-card")).toContainText("无来源时保留 [1]");
  await expect(page.locator(".citation-marker")).toHaveCount(0);
});

test("应用 knowledge_bases 设置、ETL 方案依据与各文字字段角标", async ({
  page,
  request,
}) => {
  const data = await details(request, "etl"),
    config = structuredClone(data.current.config);
  if (!("nodes" in config)) throw new Error("Expected code app");
  config.nodes.plan.params.knowledge_bases = [];
  config.nodes.plan.params.knowledge_top_k = 5;
  expect(
    (await request.put("/api/apps/etl/draft", { data: { config } })).status(),
  ).toBe(200);
  try {
    await page.goto("/apps/etl");
    await page
      .getByRole("group", { name: "流程图", exact: true })
      .getByRole("button", { name: /^出方案/ })
      .click();
    const settings = page.getByRole("region", {
      name: "节点 出方案",
      exact: true,
    });
    await expect(
      settings.getByRole("checkbox", { name: /售后政策/ }),
    ).not.toBeChecked();
    await expect(
      settings.getByLabel("检索片段数", { exact: true }),
    ).toHaveValue("5");
    await settings.getByRole("checkbox", { name: /售后政策/ }).check();
    await settings.getByLabel("检索片段数", { exact: true }).fill("1");
    const saved = page.waitForResponse(
      (response) =>
        response.url().endsWith("/apps/etl/draft") &&
        response.request().method() === "PUT",
    );
    await page.getByRole("button", { name: "保存草稿", exact: true }).click();
    expect((await saved).status()).toBe(200);
    const response = await request.post("/api/runs", {
      data: {
        app: "etl",
        draft: true,
        input: {
          datasource_id: "sample-postgres",
          request: "清洗客户和订单",
          language: "pyspark",
        },
      },
    });
    const { run_id } = await response.json();
    await page.goto(`/runs/${run_id}`);
    const plan = page.getByRole("region", { name: "ETL 方案", exact: true });
    await expect(
      plan.getByRole("region", { name: "依据的资料", exact: true }),
    ).toBeVisible();
    await expect(plan.locator(".plan-summary .citation-marker")).toHaveCount(1);
    await expect(
      plan.locator(".plan-assumptions .citation-marker"),
    ).toHaveCount(1);
    await expect(plan.locator(".mapping-table .citation-marker")).toHaveCount(
      1,
    );
    await plan
      .locator(".plan-summary")
      .getByRole("link", { name: "引用来源 1" })
      .click();
    await expect(plan.locator(".citation-source")).toHaveClass(/highlighted/);
    await expect(plan.locator(".citation-source")).toContainText("退换货期限");
    await capture(page, "etl-references");
  } finally {
    await request.delete("/api/apps/etl/draft");
  }
});
