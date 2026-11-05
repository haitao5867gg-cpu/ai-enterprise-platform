import { test, expect, type APIRequestContext } from "@playwright/test";
import type { FlowConfig } from "../lib/apps";
import { runTab } from "./run-page";

// docs/api/evals.md: eval sets, feedback on runs and the developer view.
const id = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;

async function newApp(request: APIRequestContext, app: string) {
  const made = await request.post("/api/apps", {
    data: { app, title: "评测测试", from: { blank: true } },
  });
  expect(made.status()).toBe(201);
  return (await made.json()) as { current: { config: FlowConfig } };
}

async function finishedRun(
  request: APIRequestContext,
  app: string,
  question: string,
) {
  const created = await request.post("/api/runs", {
    data: { app, input: { question } },
  });
  expect(created.status()).toBe(201);
  const { run_id } = (await created.json()) as { run_id: string };
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/runs/${run_id}`)).json()).status,
    )
    .toBe("done");
  return run_id;
}

test("a run rated right becomes an eval case that gates publishing", async ({
  page,
  request,
}) => {
  test.setTimeout(90_000);
  const app = id("evalset");
  const created = await newApp(request, app);
  const runId = await finishedRun(request, app, "我要退货");

  await page.goto(`/runs/${runId}`);
  const panel = page.getByRole("region", { name: "这次的结果对吗？" });
  await expect(panel).toBeVisible();
  await panel.getByRole("button", { name: "👍 对" }).click();
  await expect(panel.getByRole("button", { name: "👍 对" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(panel).toContainText("1 人说对 · 0 人说不对");
  await panel.getByLabel("说一句为什么（可不填）").fill("回答对了");
  await panel.getByRole("button", { name: "保存说明" }).click();
  await expect(panel.getByRole("button", { name: "保存说明" })).toBeDisabled();
  const feedback = (await (await request.get(`/api/runs/${runId}`)).json())
    .feedback;
  expect(feedback.mine).toMatchObject({ rating: "good", note: "回答对了" });

  await panel.getByRole("button", { name: "存成评测用例" }).click();
  await expect(panel.getByRole("status")).toContainText(
    "按这次的结果生成了 1 项检查",
  );
  await panel.getByRole("link", { name: "去评测集看看" }).click();
  await expect(page).toHaveURL(`/apps/${app}#release`);
  const set = page.getByRole("region", { name: "评测集" });
  const table = set.getByRole("table", { name: "评测用例" });
  await expect(table.getByRole("row")).toHaveCount(2);
  await expect(table).toContainText("我要退货");
  await expect(table).toContainText("answer 等于 （模拟回答）我要退货");
  await expect(table.getByRole("link", { name: "来自运行 ↗" })).toHaveAttribute(
    "href",
    `/runs/${runId}`,
  );

  // The set now gates publishing: a saved draft must pass it first.
  const draft = await request.put(`/api/apps/${app}/draft`, {
    data: { config: created.current.config },
  });
  expect(draft.status()).toBe(200);
  await page.reload();
  await expect(page.getByRole("button", { name: "查看草稿" })).toBeDisabled();
  const release = page.getByRole("region", { name: "发布前评测" });
  await expect(release).toContainText("尚未运行评测");
  await expect(page.locator("#app-publish-reason")).toContainText(
    "草稿须先通过评测才能发布",
  );
  await release.getByRole("button", { name: "运行评测" }).click();
  await expect(release).toContainText("评测通过", { timeout: 20_000 });
  const checks = release.getByRole("list", { name: "我要退货 的检查" });
  await expect(checks).toContainText("运行完成");
  await expect(checks.locator("li.passed")).toHaveCount(2);
  await expect(page.locator("#app-publish-reason")).toHaveCount(0);

  // Changing the set after the eval says to run it again.
  await set.getByLabel("发布门槛（0–1）").fill("0.9");
  await set.getByRole("button", { name: "保存评测集" }).click();
  await expect(set.getByRole("status")).toContainText("评测集已保存");
  await expect(release.locator(".eval-stale")).toContainText(
    "评测集改过了，建议重新评测",
  );
});

test("cases are written by hand, checked, saved and kept read-only for the ETL app", async ({
  page,
  request,
}) => {
  const app = id("evalhand");
  await newApp(request, app);
  await page.goto(`/apps/${app}#release`);
  const set = page.getByRole("region", { name: "评测集" });
  await expect(set).toContainText("还没有用例");
  await set.getByRole("button", { name: "新增用例" }).click();
  const dialog = page.getByRole("dialog", { name: "新增用例" });
  await expect(dialog.getByLabel("用例标识")).toHaveValue("case-1");
  await dialog.getByLabel("名称").fill("退货问题");
  await dialog.getByLabel("客户问题").fill("我要退货");
  await dialog.getByRole("button", { name: "加一项检查" }).click();
  await dialog.getByRole("button", { name: "放进评测集" }).click();
  await expect(dialog.getByRole("alert")).toHaveText("检查 1 还没写完。");
  const check = dialog.getByRole("group", { name: "检查 1" });
  await check.getByLabel("结果字段").fill("answer");
  await check.getByLabel("每个词都要出现").fill("退货\n七天|7 天");
  await dialog.getByRole("button", { name: "加一项检查" }).click();
  const judged = dialog.getByRole("group", { name: "检查 2" });
  await judged.getByLabel("检查方式").selectOption("judge");
  await expect(judged.getByLabel("判断标准")).toHaveValue("意思和参考答案一致");
  await judged.getByLabel("结果字段").fill("answer");
  await judged.getByLabel("判断标准").fill("语气礼貌");
  await dialog.getByRole("button", { name: "放进评测集" }).click();
  await expect(dialog).toHaveCount(0);
  const table = set.getByRole("table", { name: "评测用例" });
  await expect(table).toContainText("answer 包含 退货、七天|7 天");
  await expect(table).toContainText("模型判断 answer：语气礼貌");
  await expect(set).toContainText("有未保存的修改");
  await set.getByRole("button", { name: "保存评测集" }).click();
  await expect(set.getByRole("status")).toContainText("评测集已保存");

  const saved = await (await request.get(`/api/apps/${app}/eval-set`)).json();
  expect(saved).toMatchObject({
    source: "platform",
    threshold: 0.8,
    cases: [
      {
        id: "case-1",
        title: "退货问题",
        input: { question: "我要退货" },
        expect: [
          { kind: "contains", path: "answer", values: ["退货", "七天|7 天"] },
          {
            kind: "judge",
            path: "answer",
            criteria: "语气礼貌",
            reference: null,
          },
        ],
      },
    ],
  });
  expect(
    (await (await request.get(`/api/apps/${app}`)).json()).has_eval_set,
  ).toBe(true);

  // A second case may not reuse the id.
  await set.getByRole("button", { name: "新增用例" }).click();
  const second = page.getByRole("dialog", { name: "新增用例" });
  await second.getByLabel("用例标识").fill("case-1");
  await second.getByRole("button", { name: "放进评测集" }).click();
  await expect(second.getByRole("alert")).toHaveText("已经有用例 case-1 了。");
  await second.getByRole("button", { name: "取消" }).click();

  // Removing the last case lifts the gate.
  await table.getByRole("button", { name: "删除" }).click();
  await set.getByRole("button", { name: "保存评测集" }).click();
  await expect(set.getByRole("status")).toContainText("评测集已清空");
  expect(
    (await (await request.get(`/api/apps/${app}`)).json()).has_eval_set,
  ).toBe(false);

  // The API refuses what the page would not send.
  const bad = await request.put(`/api/apps/${app}/eval-set`, {
    data: { cases: [{ id: "x", expect: [{ kind: "contains", values: [] }] }] },
  });
  expect(bad.status()).toBe(422);

  await page.goto("/apps/etl#release");
  const etl = page.getByRole("region", { name: "评测集" });
  await expect(etl).toContainText("在代码仓库里维护");
  await expect(etl.getByRole("button", { name: "新增用例" })).toHaveCount(0);
  expect(
    (
      await request.put("/api/apps/etl/eval-set", { data: { cases: [] } })
    ).status(),
  ).toBe(409);
});

test("cases are imported from a table and an eval counts each field", async ({
  page,
  request,
}) => {
  test.setTimeout(60_000);
  const app = id("evalimport");
  const created = await newApp(request, app);
  await page.goto(`/apps/${app}#release`);
  const set = page.getByRole("region", { name: "评测集" });
  await set.getByRole("button", { name: "从表格导入" }).click();
  const form = set.getByRole("form", { name: "从表格导入" });
  await expect(form).toContainText("input.question");
  const file = form.getByLabel("表格文件（.xlsx 或 .csv，最多 5 MB）");

  // A column the platform does not know: nothing is saved, the reason is shown.
  await file.setInputFiles({
    name: "bad.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("id,备注\nx,y\n"),
  });
  await form.getByRole("button", { name: "导入" }).click();
  await expect(form.getByRole("alert")).toContainText(
    "第 2 列「备注」认不出来",
  );

  await file.setInputFiles({
    name: "cases.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      "id,title,input.question,expect.answer,contains.answer\n" +
        "refund,退货,我要退货,（模拟回答）我要退货,退货；模拟\n" +
        ",,我要换货,（模拟回答）我要换货,\n",
    ),
  });
  await form.getByRole("button", { name: "导入" }).click();
  await expect(set.getByRole("status")).toContainText(
    "导入了 2 条用例，现在共 2 条",
  );
  await expect(form).toHaveCount(0);
  const table = set.getByRole("table", { name: "评测用例" });
  await expect(table.getByRole("row")).toHaveCount(3);
  await expect(table).toContainText("answer 包含 退货、模拟");
  const saved = await (await request.get(`/api/apps/${app}/eval-set`)).json();
  expect(saved.cases.map((item: { id: string }) => item.id)).toEqual([
    "refund",
    "row-2",
  ]);

  // Replace asks first, then keeps only the new rows.
  await set.getByRole("button", { name: "从表格导入" }).click();
  await form.getByLabel("替换全部用例").check();
  await form.getByLabel("表格文件（.xlsx 或 .csv，最多 5 MB）").setInputFiles({
    name: "one.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      "id,input.question,expect.answer\nonly,我要退货,（模拟回答）我要退货\n",
    ),
  });
  page.once("dialog", (dialog) => {
    expect(dialog.message()).toContain("替换会删掉现在的 2 条用例");
    void dialog.accept();
  });
  await form.getByRole("button", { name: "导入" }).click();
  await expect(set.getByRole("status")).toContainText("导入了 1 条用例");
  await expect(table.getByRole("row")).toHaveCount(2);

  // The eval shows how each checked field did.
  const draft = await request.put(`/api/apps/${app}/draft`, {
    data: { config: created.current.config },
  });
  expect(draft.status()).toBe(200);
  await page.reload();
  const release = page.getByRole("region", { name: "发布前评测" });
  await release.getByRole("button", { name: "运行评测" }).click();
  await expect(release).toContainText("评测通过", { timeout: 20_000 });
  const fields = release.getByRole("table", { name: "按字段" });
  await expect(fields.getByRole("row")).toHaveCount(2);
  await expect(fields.getByRole("row").nth(1)).toContainText("answer");
  await expect(fields.getByRole("row").nth(1)).toContainText("1 / 1");
  await expect(fields.getByRole("row").nth(1)).toContainText("100%");
});

test("developer view: step time and tokens, rerun from a step on the draft, compare two runs", async ({
  page,
  request,
}) => {
  const app = id("devview");
  const created = await newApp(request, app);
  const runId = await finishedRun(request, app, "退货要几天");

  await page.goto(`/runs/${runId}`);
  await page.evaluate(() => localStorage.removeItem("aep.developerView"));
  await page.reload();
  await runTab(page, "过程");
  const flow = page.getByRole("region", { name: "工作流程" });
  await expect(flow.locator(".developer-bar")).toHaveCount(0);
  await flow.getByLabel("开发者视图").check();
  await expect(flow.locator(".developer-bar")).toContainText(
    "1 次调用 · 1,860 tokens",
  );
  const steps = flow.getByRole("list", { name: "本次运行的步骤" });
  await expect(steps).toContainText("回答 · 1.8 秒 · 1,860 tokens");
  await steps.getByRole("button", { name: /回答/ }).click();
  const node = flow.getByRole("region", { name: "节点 回答" });
  await expect(node.locator(".step-usage")).toContainText(
    "耗时 1.8 秒 · 1 次调用 · 1,860 tokens（输入 1,520 / 输出 340） · $0.0041",
  );

  // No draft yet: the draft rerun says so.
  const rerun = node.getByRole("group", { name: "从这一步重跑" });
  await rerun.getByRole("button", { name: "从这一步重跑" }).click();
  await expect(rerun.getByRole("alert")).toContainText("这个应用没有草稿");

  const config = structuredClone(created.current.config);
  const answer = config.graph.nodes.find((n) => n.id === "answer")!;
  if (answer.type === "llm") answer.prompt = "草稿专用：{{start.question}}";
  await request.put(`/api/apps/${app}/draft`, { data: { config } });
  await rerun.getByRole("button", { name: "从这一步重跑" }).click();
  await expect(page).not.toHaveURL(`/runs/${runId}`);
  const newId = new URL(page.url()).pathname.split("/").at(-1)!;
  await runTab(page, "详情");
  await expect(page.getByText("原运行的「回答」这一步")).toBeVisible();
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/runs/${newId}`)).json()).status,
    )
    .toBe("done");
  const again = await (await request.get(`/api/runs/${newId}`)).json();
  expect(again).toMatchObject({
    draft: true,
    rerun_of: runId,
    start_at: { node: "answer", title: "回答", run_id: runId },
    result: { answer: "（模拟回答）草稿专用：退货要几天" },
  });
  const newFlow = await (await request.get(`/api/runs/${newId}/flow`)).json();
  expect(newFlow.steps.map((s: { node: string }) => s.node)).toEqual([
    "answer",
    "end",
  ]);
  const wrong = await request.post(`/api/runs/${runId}/rerun-from`, {
    data: { node: "start", config: "same" },
  });
  expect(wrong.status()).toBe(422);

  // The view stays on for this viewer; compare the two runs.
  await page.reload();
  await runTab(page, "过程");
  const bar = page
    .getByRole("region", { name: "工作流程" })
    .locator(".developer-bar");
  await bar.getByLabel("要对比的运行编号").fill(runId);
  await bar.getByRole("link", { name: "对比两次运行" }).click();
  await expect(page).toHaveURL(`/runs/compare?a=${newId}&b=${runId}`);
  const compared = page.getByRole("table", { name: "两次运行对比" });
  const answerRow = compared.locator("tr", {
    has: page.getByRole("rowheader", { name: /^回答/ }),
  });
  await expect(answerRow.locator(".compare-flag")).toHaveText("产出不同");
  await expect(answerRow).toContainText("草稿专用：退货要几天");
  const row = (name: RegExp) =>
    compared.locator("tr", { has: page.getByRole("rowheader", { name }) });
  // Same input; the rerun began after the start, so its result differs.
  await expect(row(/^输入/)).not.toHaveClass(/differs/);
  await expect(row(/^结果/)).toHaveClass(/differs/);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
