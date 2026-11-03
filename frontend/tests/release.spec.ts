import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type { AppDetail, FlowConfig } from "../lib/apps";

// docs/api/data-nodes.md, "第五批：上线到 prod": the release node in the editor and on
// the run page.
const id = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });

async function newApp(request: APIRequestContext, app: string) {
  const made = await request.post("/api/apps", {
    data: { app, title: "上线测试", from: { blank: true } },
  });
  expect(made.status()).toBe(201);
  return (await made.json()) as AppDetail;
}

async function choose(page: Page, label: string, target: string) {
  await panel(page)
    .getByRole("group", { name: "下一步" })
    .getByLabel(label, { exact: true })
    .selectOption(target);
}

async function select(page: Page, nodeId: string) {
  await page
    .getByRole("complementary", { name: "节点列表" })
    .getByRole("button", { name: new RegExp(`（${nodeId}）$`) })
    .click();
}

test("the release node is added, checked and saved", async ({
  page,
  request,
}) => {
  const app = id("release");
  await newApp(request, app);
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
  await page.goto(`/apps/${app}`);
  await page.getByRole("button", { name: "添加节点", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "添加节点" });
  await dialog
    .getByRole("region", { name: "数据工程" })
    .getByRole("button", { name: "上线到 prod", exact: true })
    .click();
  // It writes Databricks: no trying it alone.
  await expect(
    panel(page).getByRole("heading", { name: "试跑这个节点" }),
  ).toHaveCount(0);
  await expect(panel(page).getByLabel("上线环境")).toHaveValue("prod");

  await select(page, "answer");
  await choose(page, "下一步", "databricks_release");
  await select(page, "databricks_release");
  await choose(page, "已上线", "end");
  await choose(page, "失败", "end");
  await panel(page)
    .getByLabel("bundle", { exact: true })
    .fill("{{answer.text}}");
  const schedule = panel(page).getByLabel("定时（Quartz cron）", {
    exact: true,
  });
  await schedule.fill("0 2 * * *");
  // Checked before saving: nothing is saved until it is right.
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(panel(page)).toContainText(
    "定时要写成 Quartz cron，如每天 2 点：0 0 2 * * ?",
  );
  await schedule.fill("0 0 2 ? * MON");
  await panel(page).getByLabel("上线环境").fill("uat");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "草稿已保存" }),
  ).toBeVisible();
  const saved = (await (
    await request.get(`/api/apps/${app}`)
  ).json()) as AppDetail;
  const config = saved.draft!.config as FlowConfig;
  expect(
    config.graph.nodes.find((node) => node.id === "databricks_release"),
  ).toMatchObject({
    type: "databricks_release",
    bundle: "{{answer.text}}",
    schedule: "0 0 2 ? * MON",
    environment: "uat",
  });
  expect(config.graph.edges).toEqual(
    expect.arrayContaining([
      { from: "answer", to: "databricks_release" },
      { from: "databricks_release", to: "end", branch: "deployed" },
      { from: "databricks_release", to: "end", branch: "failed" },
    ]),
  );
});

async function releaseRun(
  request: APIRequestContext,
  app: string,
  bundleName: string,
) {
  const created = await newApp(request, app);
  const config = structuredClone(created.current.config) as FlowConfig;
  config.graph.nodes = config.graph.nodes.filter(
    (node) => node.id !== "answer",
  );
  config.graph.nodes.splice(
    1,
    0,
    {
      id: "check_code",
      type: "etl_code_check",
      title: "检查并打包",
      reply: "{{start.question}}",
      plan: "{{start.question}}",
      source_name: "示例库",
      name: bundleName,
      max_attempts: 3,
    },
    {
      id: "release",
      type: "databricks_release",
      title: "上线到 prod",
      bundle: "{{check_code.bundle}}",
      schedule: "0 0 2 1 * ?",
      environment: "prod",
    },
  );
  config.graph.edges = [
    { from: "start", to: "check_code" },
    { from: "check_code", to: "release", branch: "ok" },
    { from: "check_code", to: "end", branch: "retry" },
    { from: "release", to: "end", branch: "deployed" },
    { from: "release", to: "end", branch: "failed" },
  ];
  const end = config.graph.nodes.find((node) => node.type === "end");
  if (end?.type === "end")
    end.outputs = [{ key: "release", from: ["{{release.summary}}"] }];
  const saved = await request.put(`/api/apps/${app}/draft`, {
    data: { config },
  });
  expect(saved.status(), await saved.text()).toBe(200);
  const published = await request.post(`/api/apps/${app}/draft/publish`, {
    data: { note: "上线" },
  });
  expect(published.status(), await published.text()).toBe(200);
  const run = await request.post("/api/runs", {
    data: { app, input: { question: "上线" } },
  });
  expect(run.status()).toBe(201);
  const { run_id } = (await run.json()) as { run_id: string };
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/runs/${run_id}`)).json()).status,
    )
    .toBe("done");
  return run_id;
}

test("the run page shows the released job, or why nothing was released", async ({
  page,
  request,
}) => {
  const done = await releaseRun(request, id("released"), "retail_sales");
  const run = await (await request.get(`/api/runs/${done}`)).json();
  expect(run.result.release).toBe(
    "已上线到 prod：Job 100，每月 1 日 02:00（Asia/Shanghai）刷新 `demo_prod`.retail_mart",
  );
  await page.goto(`/runs/${done}`);
  const flow = page.getByRole("region", { name: "工作流程" });
  await flow
    .getByRole("list", { name: "本次运行的步骤" })
    .getByRole("button", { name: /上线到 prod/ })
    .click();
  const shown = flow.getByRole("group", { name: "上线结果" });
  await expect(shown).toContainText("已上线到 prod：Job 100");
  await expect(
    shown.getByRole("link", { name: "在 Databricks 上打开 Job ↗" }),
  ).toHaveAttribute("href", "https://adb-mock.azuredatabricks.net/#job/100");

  const failed = await releaseRun(request, id("unreleased"), "fail_sales");
  await page.goto(`/runs/${failed}`);
  await page
    .getByRole("region", { name: "工作流程" })
    .getByRole("list", { name: "本次运行的步骤" })
    .getByRole("button", { name: /上线到 prod/ })
    .click();
  const why = page.getByRole("group", { name: "上线结果" });
  await expect(why).toContainText(
    "上线到 prod 失败：没有配置 prod 目标（AEP_DATABRICKS_PROD_CATALOG、AEP_DATABRICKS_PROD_SCHEMA）",
  );
  await expect(why).toHaveClass(/failed/);
  await expect(why.getByRole("link")).toHaveCount(0);
});

test("the refresh time is asked only of someone releasing", async ({
  page,
}) => {
  // jadeflow retest I-19: 先不上线 was refused for want of a refresh time.
  const runId = id("release-review");
  const waiting = {
    run_id: runId,
    app: "t2-forecast",
    status: "waiting",
    step: { node: "release_review", title: "上线审批" },
    messages: [{ role: "assistant", content: "试算结果已审核通过" }],
    pending: {
      kind: "approval",
      prompt: "试算结果已审核通过。请看上线说明。",
      options: ["release", "skip"],
      option_labels: { release: "上线到 prod", skip: "先不上线，交付" },
      comment_required: [],
      changed_branch: null,
      fields: [
        {
          key: "schedule",
          label: "上线后什么时候自动刷新？",
          type: "select",
          required: true,
          choices: [
            { value: "0 0 2 * * ?", label: "每天 02:00" },
            { value: "0 0 2 1 * ?", label: "每月 1 日 02:00" },
          ],
          suggested: "0 0 2 * * ?",
          only_for: ["release"],
        },
      ],
      details: {
        type: "flow_human",
        node: "release_review",
        show: "## 上线说明\n\n- 上线到项目「T2」的 生产 环境（prod）",
        view: null,
        data: {},
        round: 1,
        seen: "sha256:1",
        dropped_fields: [],
      },
    },
    result: null,
    error: null,
  };
  await page.route(`**/api/runs/${runId}`, (route) =>
    route.fulfill({ json: waiting }),
  );
  const posted: unknown[] = [];
  await page.route(`**/api/runs/${runId}/resume`, (route) => {
    posted.push(route.request().postDataJSON());
    return route.fulfill({
      status: 202,
      json: { run_id: runId, status: "running" },
    });
  });
  await page.goto(`/runs/${runId}`);
  const question = page.getByRole("group", {
    name: "1. 上线后什么时候自动刷新？",
  });
  await expect(question).toContainText(
    "只在选「上线到 prod」时要答，选别的不用答",
  );
  await page.getByLabel("你的名字").fill("韩梅梅");

  // Releasing needs it.
  await page.getByRole("button", { name: "上线到 prod", exact: true }).click();
  await expect(page.locator("#question-schedule-error")).toHaveText(
    "请选一个答案",
  );
  expect(posted).toEqual([]);

  // Not releasing does not, and sends no answer.
  await page
    .getByRole("button", { name: "先不上线，交付", exact: true })
    .click();
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]).toEqual({
    decision: "skip",
    comment: "",
    actor: "韩梅梅",
    seen: "sha256:1",
  });
});
