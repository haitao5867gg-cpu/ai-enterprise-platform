import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { isFlowConfig, type AppDetail } from "../lib/apps";

async function appDetail(
  request: APIRequestContext,
  app = "etl",
): Promise<AppDetail> {
  const response = await request.get(`/api/apps/${app}`);
  expect(response.ok()).toBeTruthy();
  return response.json();
}
async function planSettings(page: Page) {
  await page
    .getByRole("group", { name: "流程图", exact: true })
    .getByRole("button", { name: /^出方案/ })
    .click();
  return page.getByRole("region", { name: "节点 出方案", exact: true });
}
async function saveDraft(page: Page) {
  const response = page.waitForResponse(
    (response) =>
      response.url().endsWith("/draft") &&
      response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  expect((await response).status()).toBe(200);
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toContainText("正在查看：草稿");
}
async function latestVersion(request: APIRequestContext, app: string) {
  const versions: { version: number }[] = await (
    await request.get(`/api/apps/${app}/versions`)
  ).json();
  return Math.max(...versions.map((item) => item.version));
}

test.beforeEach(async ({ request }) => {
  for (const app of ["etl", "hello"]) {
    expect((await request.delete(`/api/apps/${app}/draft`)).status()).toBe(204);
    expect(
      (
        await request.post(`/api/apps/${app}/versions/1/activate`, { data: {} })
      ).status(),
    ).toBe(200);
  }
});

test("application draft, evaluation, publish, configuration comparison and confirmed rollback", async ({
  page,
  request,
}) => {
  const nextVersion = (await latestVersion(request, "etl")) + 1;
  await page.addInitScript(() => localStorage.setItem("aep.actor", "张三"));
  await page.goto("/apps");
  await expect(
    page.getByRole("link", { name: /自然语言 ETL 工作流/ }),
  ).toContainText("需通过评测");
  await page.getByRole("link", { name: /自然语言 ETL 工作流/ }).click();
  await expect(
    page.getByRole("region", { name: "应用设计", exact: true }),
  ).toContainText("固定流程的工作流");
  const plan = await planSettings(page);
  await plan.getByLabel("模型", { exact: true }).selectOption("deepseek");
  await plan.getByLabel("最多重写次数", { exact: true }).fill("4");
  await expect(page.getByText("有未保存的修改", { exact: true })).toBeVisible();
  const saved = page.waitForRequest(
    (request) =>
      request.url().endsWith("/apps/etl/draft") && request.method() === "PUT",
  );
  await saveDraft(page);
  expect((await saved).postDataJSON().config.nodes.plan).toMatchObject({
    model: "deepseek",
    params: { max_attempts: 4 },
  });
  await page.reload();
  await expect(
    (await planSettings(page)).getByLabel("模型", { exact: true }),
  ).toHaveValue("deepseek");
  await expect(plan.getByLabel("最多重写次数", { exact: true })).toHaveValue(
    "4",
  );
  await page.getByLabel("发布说明（必填）").fill("改用 DeepSeek，允许四次重写");
  await expect(page.getByRole("button", { name: "发布新版本" })).toBeDisabled();
  await expect(
    page.getByText("草稿须先通过评测才能发布；总分需达到阈值。"),
  ).toBeVisible();
  const evaluation = page.waitForResponse((response) =>
    response.url().endsWith("/draft/eval"),
  );
  await page.getByRole("button", { name: "运行评测", exact: true }).click();
  expect((await evaluation).status()).toBe(202);
  await expect(page.getByText("评测运行中", { exact: true })).toBeVisible();
  const duplicate = await request.post("/api/apps/etl/draft/eval", {
    data: {},
  });
  expect(duplicate.status()).toBe(409);
  expect((await duplicate.json()).detail).toContain("已有评测");
  await expect(page.getByText("评测通过", { exact: true })).toBeVisible({
    timeout: 16_000,
  });
  await expect(page.getByText("总分 0.90 · 阈值 0.80")).toBeVisible();
  await expect(
    page.getByRole("list", { name: "评测用例" }).getByRole("listitem"),
  ).toHaveCount(2);
  const caseHref = await page
    .getByRole("link", { name: "orders-clean ↗" })
    .getAttribute("href");
  const run = await request.get(`/api${caseHref}`);
  expect((await run.json()).status).toBe("done");
  await page.getByRole("link", { name: "orders-clean ↗" }).click();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  await page.goBack();
  await page.getByLabel("发布说明（必填）").fill("改用 DeepSeek，允许四次重写");
  const published = page.waitForRequest((request) =>
    request.url().endsWith("/draft/publish"),
  );
  await page.getByRole("button", { name: "发布新版本" }).click();
  expect((await published).postDataJSON()).toMatchObject({
    actor: "张三",
    note: "改用 DeepSeek，允许四次重写",
  });
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toContainText(`当前版本 v${nextVersion}`);
  const history = page.getByRole("region", { name: "版本历史", exact: true });
  await expect(history).toContainText(`v${nextVersion} · 当前版本`);
  await expect(history).toContainText("张三");
  await expect(history).toContainText("评测分 0.90");
  await history.getByLabel("v1", { exact: true }).check();
  await history.getByLabel(`v${nextVersion}`, { exact: true }).check();
  const diff = page.getByRole("region", { name: "配置差异" });
  await expect(diff).toContainText("plan.model");
  await expect(diff).toContainText("deepseek");
  await expect(diff).toContainText("plan.max_attempts");
  const first = history
    .locator(".asset-history > li")
    .filter({ hasText: /^v1 ·/ });
  page.once("dialog", (dialog) => dialog.dismiss());
  await first.getByRole("button", { name: "设为当前版本" }).click();
  expect((await appDetail(request)).current_version).toBe(nextVersion);
  page.once("dialog", (dialog) => dialog.accept());
  const activated = page.waitForRequest((request) =>
    request.url().endsWith("/versions/1/activate"),
  );
  await first.getByRole("button", { name: "设为当前版本" }).click();
  expect((await activated).postDataJSON()).toEqual({ actor: "张三" });
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toContainText("当前版本 v1");
  await expect(
    (await planSettings(page)).getByLabel("模型", { exact: true }),
  ).toHaveValue("");
});

test("failed evaluation blocks publishing; saving a correction clears previous results", async ({
  page,
  request,
}) => {
  await page.goto("/apps/etl");
  const plan = await planSettings(page);
  await plan.getByLabel("最多重写次数", { exact: true }).fill("1");
  await saveDraft(page);
  await page.getByRole("button", { name: "运行评测", exact: true }).click();
  await expect(page.getByText("评测未通过", { exact: true })).toBeVisible({
    timeout: 16_000,
  });
  await expect(page.getByText("总分 0.60 · 阈值 0.80")).toBeVisible();
  await page.getByLabel("发布说明（必填）").fill("不可发布的失败草稿");
  await expect(page.getByRole("button", { name: "发布新版本" })).toBeDisabled();
  const blocked = await request.post("/api/apps/etl/draft/publish", {
    data: { note: "绕过界面也不能发布" },
  });
  expect(blocked.status()).toBe(409);
  expect((await blocked.json()).detail).toContain("通过评测");
  await plan.getByLabel("最多重写次数", { exact: true }).fill("3");
  await saveDraft(page);
  await expect(
    page.getByText("尚未运行评测。每次保存草稿后，需要重新评测。"),
  ).toBeVisible();
  expect((await appDetail(request)).draft?.eval).toBeNull();
  await expect(page.getByText("总分 0.60 · 阈值 0.80")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "发布新版本" })).toBeDisabled();
});

test("hello publishes without evaluation and tolerates unavailable browser storage", async ({
  page,
  request,
}) => {
  const version = (await latestVersion(request, "hello")) + 1;
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => {
      throw new Error("Storage unavailable");
    };
    Storage.prototype.setItem = () => {
      throw new Error("Storage unavailable");
    };
  });
  const config = (await appDetail(request, "hello")).current.config;
  if (!isFlowConfig(config)) throw new Error("Expected flow config");
  const draft = config.graph.nodes.find((node) => node.type === "llm")!;
  draft.model = "qwen";
  expect(
    (await request.put("/api/apps/hello/draft", { data: { config } })).status(),
  ).toBe(200);
  await page.goto("/apps/hello");
  await expect(
    page.getByRole("button", { name: "运行评测", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("combobox", { name: "模型", exact: true }),
  ).toHaveCount(0);
  const noEval = await request.post("/api/apps/hello/draft/eval", { data: {} });
  expect(noEval.status()).toBe(409);
  await expect(
    page.getByText("此应用没有评测集，保存草稿后可直接发布。"),
  ).toBeVisible();
  await page.getByLabel("你的名字").fill("李四");
  await page.getByLabel("发布说明（必填）").fill("免评测发布");
  await expect(page.getByRole("button", { name: "发布新版本" })).toBeEnabled();
  await page.getByRole("button", { name: "发布新版本" }).click();
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toContainText(`当前版本 v${version}`);
  expect((await appDetail(request, "hello")).current.created_by).toBe("李四");
});

test("invalid integer values and backend 422 / 409 details are visible", async ({
  page,
  request,
}) => {
  await page.goto("/apps/etl");
  const plan = await planSettings(page);
  await plan.getByLabel("最多重写次数", { exact: true }).fill("9");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "最多重写次数须为 1~5 的整数",
  );
  expect((await appDetail(request)).draft).toBeNull();
  const config = (await appDetail(request)).current.config;
  if (isFlowConfig(config)) throw new Error("Expected code config");
  config.nodes.plan.params.max_attempts = 9;
  const invalid = await request.put("/api/apps/etl/draft", {
    data: { config },
  });
  expect(invalid.status()).toBe(422);
  expect((await invalid.json()).detail[0].loc).toEqual([
    "body",
    "config",
    "nodes",
    "plan",
    "params",
    "max_attempts",
  ]);
  await plan.getByLabel("最多重写次数", { exact: true }).fill("4");
  await page.route("**/api/apps/etl/draft", (route) =>
    route.fulfill({
      status: 422,
      json: {
        detail: [
          { loc: ["body", "config", "nodes", "plan"], msg: "组合配置不合法" },
        ],
      },
    }),
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "body.config.nodes.plan: 组合配置不合法",
  );
  await page.unroute("**/api/apps/etl/draft");
  page.once("dialog", (dialog) => dialog.accept());
  const flow = (await appDetail(request, "hello")).current.config;
  if (!isFlowConfig(flow)) throw new Error("Expected flow config");
  flow.graph.nodes.find((node) => node.type === "llm")!.model = "deepseek";
  expect(
    (
      await request.put("/api/apps/hello/draft", { data: { config: flow } })
    ).status(),
  ).toBe(200);
  await page.goto("/apps/hello");
  await page.route("**/api/apps/hello/draft/publish", (route) =>
    route.fulfill({
      status: 409,
      json: { detail: "当前版本已变化，请刷新后重试" },
    }),
  );
  await page.getByLabel("发布说明（必填）").fill("冲突测试");
  await page.getByRole("button", { name: "发布新版本" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "当前版本已变化，请刷新后重试",
  );
});

test("settings are generated from the contract, including published Skill versions and all parameter types", async ({
  page,
  request,
}) => {
  const name = `settings-${Date.now()}`;
  await request.post("/api/skills", {
    data: { name, description: "设置测试", body: "第一版" },
  });
  await request.post(`/api/skills/${name}/publish`, { data: { note: "v1" } });
  await request.put(`/api/skills/${name}/draft`, {
    data: { description: "设置测试", body: "第二版" },
  });
  await request.post(`/api/skills/${name}/publish`, { data: { note: "v2" } });
  const data = await appDetail(request);
  data.nodes
    .find((node) => node.id === "plan")!
    .settings.push(
      {
        key: "language",
        label: "默认代码语言",
        type: "enum",
        choices: [
          { value: "pyspark", label: "PySpark" },
          { value: "sql", label: "SQL" },
        ],
      },
      { key: "strict", label: "严格模式", type: "boolean" },
      {
        key: "prefix",
        label: "表名前缀",
        type: "string",
        max_length: 12,
        help: "目标表的命名前缀",
      },
    );
  if (isFlowConfig(data.current.config))
    throw new Error("Expected code config");
  Object.assign(data.current.config.nodes.plan.params, {
    language: "pyspark",
    strict: false,
    prefix: "",
  });
  await page.route("**/api/apps/etl", (route) => route.fulfill({ json: data }));
  await page.route("**/api/apps/etl/draft", (route) => {
    const { config } = route.request().postDataJSON();
    return route.fulfill({
      json: {
        ...data,
        has_draft: true,
        draft: {
          ...data.current,
          status: "draft",
          version: null,
          config,
          eval: null,
        },
      },
    });
  });
  await page.goto("/apps/etl");
  const plan = await planSettings(page);
  await expect(plan.getByLabel("模型", { exact: true })).toHaveValue("");
  await expect(
    plan
      .getByLabel("模型", { exact: true })
      .getByRole("option", { name: "平台默认（mock）", exact: true }),
  ).toHaveCount(1);
  await plan.getByLabel(name, { exact: true }).check();
  await expect(plan.getByLabel(`${name} 版本`, { exact: true })).toHaveValue(
    "2",
  );
  await plan.getByLabel(`${name} 版本`, { exact: true }).selectOption("1");
  await plan.getByLabel("etl-codegen", { exact: true }).check();
  await plan.getByLabel("默认代码语言", { exact: true }).selectOption("sql");
  await plan.getByRole("switch", { name: "严格模式" }).check();
  await plan.getByLabel("表名前缀", { exact: true }).fill("clean_");
  const saved = page.waitForRequest(
    (request) =>
      request.url().endsWith("/apps/etl/draft") && request.method() === "PUT",
  );
  await saveDraft(page);
  expect((await saved).postDataJSON().config.nodes.plan).toMatchObject({
    model: null,
    skills: expect.arrayContaining([
      { name, version: 1 },
      { name: "etl-codegen", version: 1 },
    ]),
    params: { language: "sql", strict: true, prefix: "clean_" },
  });
});

test("create Skill, edit references, publish two versions, view history and compare bodies", async ({
  page,
  request,
}) => {
  const name = `order-rules-${Date.now()}`;
  await page.addInitScript(() => localStorage.setItem("aep.actor", "王五"));
  await page.goto("/skills");
  await expect(
    page.getByRole("article").filter({ hasText: "etl-plan" }),
  ).toContainText("etl / plan · 应用 v1");
  await page.getByRole("button", { name: "新建 Skill", exact: true }).click();
  await page.getByLabel("名称", { exact: true }).fill("Invalid_Name");
  await page.getByLabel("说明", { exact: true }).fill("订单清洗规则");
  await page.getByRole("button", { name: "创建草稿", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "名称须以小写字母开头",
  );
  await page.getByLabel("名称", { exact: true }).fill(name);
  await page
    .getByLabel("正文（Markdown）", { exact: true })
    .fill("# 订单规则\n金额必须非负");
  await page.getByRole("button", { name: "创建草稿", exact: true }).click();
  await expect(page).toHaveURL(`/skills/${name}`);
  await expect(
    page.getByRole("heading", { name: "尚未发布", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "编辑草稿", exact: true }).click();
  await page.getByRole("button", { name: "添加参考资料", exact: true }).click();
  await page.getByLabel("文件名", { exact: true }).fill("glossary.md");
  await page
    .getByLabel("参考资料正文", { exact: true })
    .fill("# 术语\n订单金额：原币金额");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Skill 草稿已保存" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "发布新版本" })).toBeDisabled();
  await page.getByLabel("发布说明（必填）").fill("初始规则");
  await page.getByRole("button", { name: "发布新版本" }).click();
  await expect(
    page.getByRole("heading", { name: "正在查看：当前版本 v1", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Skill 正文", exact: true }),
  ).toContainText("glossary.md");
  await page.getByRole("button", { name: "编辑草稿", exact: true }).click();
  await page
    .getByLabel("正文（Markdown）", { exact: true })
    .fill("# 订单规则\n金额必须非负\n过滤测试订单");
  await page.getByLabel("说明", { exact: true }).fill("订单清洗与过滤规则");
  await page.getByLabel("文件名", { exact: true }).fill("rules.md");
  await page
    .getByLabel("参考资料正文", { exact: true })
    .fill("过滤标记为 test 的订单");
  await page.getByRole("button", { name: "添加参考资料", exact: true }).click();
  await page
    .getByRole("group", { name: "参考资料 2", exact: true })
    .getByLabel("文件名", { exact: true })
    .fill("remove.md");
  await page
    .getByRole("button", { name: "删除参考资料 2", exact: true })
    .click();
  await expect(page.getByText("有未保存的修改", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Skill 草稿已保存" }),
  ).toBeVisible();
  await page.getByLabel("发布说明（必填）").fill("加入测试订单过滤");
  const publishing = page.waitForRequest((request) =>
    request.url().endsWith(`/skills/${name}/publish`),
  );
  await page.getByRole("button", { name: "发布新版本" }).click();
  expect((await publishing).postDataJSON().actor).toBe("王五");
  await expect(
    page.getByRole("heading", { name: "正在查看：当前版本 v2", exact: true }),
  ).toBeVisible();
  const history = page.getByRole("region", { name: "版本历史", exact: true });
  await expect(history).toContainText("加入测试订单过滤");
  await expect(history).toContainText("王五");
  await history.getByLabel("v1", { exact: true }).check();
  await history.getByLabel("v2", { exact: true }).check();
  await expect(page.getByRole("region", { name: "正文差异" })).toContainText(
    "+ 过滤测试订单",
  );
  await history.getByRole("button", { name: "查看 v1", exact: true }).click();
  const content = page.getByRole("region", { name: "Skill 正文", exact: true });
  await expect(content).toContainText("glossary.md");
  await expect(content).not.toContainText("过滤测试订单");
  await page.getByRole("button", { name: "返回当前版本", exact: true }).click();
  await expect(content).toContainText("rules.md");
  await expect(content).not.toContainText("glossary.md");
  const detail = await (await request.get(`/api/skills/${name}`)).json();
  expect(detail.current.references).toEqual({
    "rules.md": "过滤标记为 test 的订单",
  });
  expect(detail.has_draft).toBe(false);
  await page.goto("/skills");
  await expect(
    page.getByRole("article").filter({ hasText: name }),
  ).toContainText("最新 v2");
});

test("draft discard, version switch protection and mobile pages do not overflow", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 320, height: 840 });
  for (const path of ["/apps", "/skills", "/apps/etl", "/skills/etl-plan"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByText("正在加载…", { exact: true })).toHaveCount(0);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  }
  await page.goto("/apps/etl");
  const plan = await planSettings(page);
  await plan.getByLabel("最多重写次数", { exact: true }).fill("2");
  await saveDraft(page);
  await plan.getByLabel("最多重写次数", { exact: true }).fill("4");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "查看当前版本", exact: true }).click();
  await expect(plan.getByLabel("最多重写次数", { exact: true })).toHaveValue(
    "4",
  );
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "放弃草稿", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toContainText("当前版本 v1");
  expect((await appDetail(request)).has_draft).toBe(false);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.goto("/skills/etl-plan");
  await page.getByRole("button", { name: "编辑草稿", exact: true }).click();
  await page
    .getByLabel("正文（Markdown）", { exact: true })
    .fill("订单".repeat(100));
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Skill 草稿已保存" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "放弃草稿", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "已放弃 Skill 草稿" }),
  ).toBeVisible();
  const skill = await (await request.get("/api/skills/etl-plan")).json();
  expect(skill.has_draft).toBe(false);
  expect(skill.published_version).toBe(1);
});

test("in-app links ask before leaving unsaved edits", async ({ page }) => {
  await page.goto("/apps/etl");
  const plan = await planSettings(page);
  await plan.getByLabel("最多重写次数", { exact: true }).fill("2");
  const nav = page.getByRole("navigation", { name: "主导航" });

  let asked = "";
  page.once("dialog", (dialog) => {
    asked = dialog.message();
    void dialog.dismiss();
  });
  await nav.getByRole("link", { name: "Skill", exact: true }).click();
  expect(asked).toContain("未保存");
  await expect(page).toHaveURL(/\/apps\/etl$/);
  await expect(plan.getByLabel("最多重写次数", { exact: true })).toHaveValue(
    "2",
  );

  page.once("dialog", (dialog) => void dialog.accept());
  await nav.getByRole("link", { name: "Skill", exact: true }).click();
  await expect(page).toHaveURL(/\/skills$/);
});

test("mock contract validates Skill limits, missing drafts, names and published references", async ({
  request,
}) => {
  expect(
    (await request.post("/api/apps/etl/draft/eval", { data: {} })).status(),
  ).toBe(409);
  expect(
    (
      await request.post("/api/apps/hello/draft/publish", {
        data: { note: "无草稿" },
      })
    ).status(),
  ).toBe(409);
  expect(
    (
      await request.post("/api/skills/etl-plan/publish", {
        data: { note: "无草稿" },
      })
    ).status(),
  ).toBe(409);
  expect((await request.delete("/api/skills/etl-plan/draft")).status()).toBe(
    404,
  );
  expect(
    (
      await request.post("/api/skills", {
        data: { name: "etl-plan", description: "重名", body: "" },
      })
    ).status(),
  ).toBe(409);
  for (const extra of [
    { name: "Invalid_Name" },
    { description: "" },
    { description: "字".repeat(501) },
    { body: "a".repeat(20_001) },
    { references: { "../bad.md": "bad" } },
    { references: { "long.md": "a".repeat(20_001) } },
    {
      references: Object.fromEntries(
        Array.from({ length: 11 }, (_, i) => [`ref${i}.md`, ""]),
      ),
    },
  ]) {
    const result = await request.post("/api/skills", {
      data: {
        name: `limit-${Date.now()}`,
        description: "校验",
        body: "",
        ...extra,
      },
    });
    expect(result.status()).toBe(422);
    expect(Array.isArray((await result.json()).detail)).toBe(true);
  }
  const config = (await appDetail(request)).current.config;
  if (isFlowConfig(config)) throw new Error("Expected code config");
  config.nodes.plan.skills = [{ name: "etl-plan", version: 99 }];
  expect(
    (await request.put("/api/apps/etl/draft", { data: { config } })).status(),
  ).toBe(422);
  expect((await request.get("/api/apps/missing")).status()).toBe(404);
  expect((await request.get("/api/skills/etl-plan/versions/99")).status()).toBe(
    404,
  );
  const skills: { name: string }[] = await (
    await request.get("/api/skills")
  ).json();
  expect(skills.map((skill) => skill.name)).toEqual(
    skills.map((skill) => skill.name).sort(),
  );
});
