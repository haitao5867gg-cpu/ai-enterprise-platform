import { test, expect, type APIRequestContext } from "@playwright/test";

// The result page of the ETL template's second version (docs/api/data-nodes.md, "结果页"),
// on runs of the mock app retail-etl-v2 driven through the API.
type MockRun = {
  run_id: string;
  status: string;
  pending: { details: { seen: string } } | null;
};
async function moved(request: APIRequestContext, id: string): Promise<MockRun> {
  for (let i = 0; i < 50; i++) {
    const run = (await (
      await request.get(`/api/runs/${id}`)
    ).json()) as MockRun;
    if (run.status !== "running") return run;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error("the run did not move on");
}
async function decide(
  request: APIRequestContext,
  run: MockRun,
  body: Record<string, unknown>,
) {
  expect(run.status).toBe("waiting");
  const response = await request.post(`/api/runs/${run.run_id}/resume`, {
    data: { ...body, seen: run.pending?.details.seen },
  });
  expect(response.status()).toBe(202);
  return moved(request, run.run_id);
}
async function finished(request: APIRequestContext, stop = false) {
  const created = await request.post("/api/runs", {
    data: {
      app: "retail-etl-v2",
      input: {
        datasource: "sample-postgres",
        request: "按城市和会员等级统计上个月的销售",
        tables: "retail.*",
        language: "pyspark",
      },
    },
  });
  expect(created.status()).toBe(201);
  const { run_id } = (await created.json()) as { run_id: string };
  let run = await moved(request, run_id);
  run = await decide(request, run, {
    decision: "approve",
    values: { q1: "all", q2: "exclude" },
    actor: "韩梅梅",
  });
  run = await decide(
    request,
    run,
    stop
      ? { decision: "stop", comment: "需求取消了", actor: "李雷" }
      : { decision: "approve", actor: "李雷" },
  );
  expect(run.status).toBe("done");
  return run_id;
}

test("a delivered run: the answer first, then the plan with its records, the code, the report and the developer view", async ({
  page,
  request,
}) => {
  const id = await finished(request);
  await page.goto(`/runs/${id}`);
  await expect(page.locator(".detail-heading .status-badge")).toHaveText(
    "已交付",
  );
  const result = page.locator(".etl-result");
  const tabs = result.getByRole("tablist", { name: "运行结果" });
  await expect(tabs.getByRole("tab")).toHaveText([
    "答案",
    "方案",
    "代码",
    "运行报告",
    "开发者视图",
  ]);
  await expect(tabs.getByRole("tab", { name: "答案" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const card = result.getByRole("region", { name: "答案", exact: true });
  await expect(card.locator(".answer-heading")).toHaveText(
    "开发环境试算 · 已由 李雷 审核 · 尚未上线",
  );
  await expect(card.locator(".answer-conclusion")).toContainText(
    "上海 · gold 的销售额最高",
  );
  await expect(card.locator("tfoot td").nth(4)).toHaveText("—");
  await expect(card.locator(".answer-note")).toHaveText(
    "合计里的「—」：下单客户数不能把各组直接相加。",
  );
  // The side says what the run came to, without the result's JSON.
  await expect(page.locator(".run-sidebar .result-card")).toContainText(
    "已交付",
  );
  await expect(page.locator(".run-sidebar .result-json")).toHaveCount(0);

  await tabs.getByRole("tab", { name: "方案" }).click();
  await expect(result.locator(".brief-goal")).toHaveText(
    "按月、按城市、按会员等级统计有效订单的 4 个指标",
  );
  await expect(
    result.getByRole("region", { name: "确认方案的记录" }),
  ).toContainText("韩梅梅");
  await expect(
    result.getByRole("region", { name: "审核试算结果的记录" }),
  ).toContainText("李雷 ·");
  // The technical plan is folded.
  const technical = result.locator(".result-tech-plan");
  await expect(technical).not.toHaveAttribute("open", "");
  await technical.getByText("技术方案").first().click();
  await expect(
    technical.getByRole("table", { name: "ads_sales_city_member_m 的列" }),
  ).toBeVisible();

  await tabs.getByRole("tab", { name: "代码" }).click();
  await expect(result.locator(".review-code-heading")).toContainText(
    "retail_sales · 5 个文件",
  );
  const zip = await request.get(`/api/runs/${id}/files.zip`);
  expect(zip.status()).toBe(200);
  expect(zip.headers()["content-type"]).toContain("application/zip");

  await tabs.getByRole("tab", { name: "运行报告" }).click();
  await expect(result.locator(".result-text")).toContainText(
    "Databricks dev 运行成功",
  );

  await tabs.getByRole("tab", { name: "开发者视图" }).click();
  await expect(result.locator(".result-developer")).toContainText(id);
  await expect(result.locator(".result-json")).toContainText('"dev_url"');
  await expect(result.getByRole("heading", { name: "工作流程" })).toBeVisible();
});

test("a stopped run says why, with no answer and no code", async ({
  page,
  request,
}) => {
  const id = await finished(request, true);
  await page.goto(`/runs/${id}`);
  await expect(page.locator(".detail-heading .status-badge")).toHaveText(
    "已终止",
  );
  const result = page.locator(".etl-result");
  await expect(result.getByRole("heading", { name: "已终止" })).toBeVisible();
  await expect(result.locator(".review-returned")).toContainText(
    "原因：需求取消了",
  );
  await expect(result.getByRole("tab")).toHaveText([
    "方案",
    "运行报告",
    "开发者视图",
  ]);
  await expect(result.locator(".answer-card")).toHaveCount(0);
  // The code that was not delivered can still be downloaded.
  const zip = await request.get(`/api/runs/${id}/files.zip`);
  expect(zip.status()).toBe(200);
});

test("a plan without an answer design, an earlier run and an app of one's own keep their pages", async ({
  page,
  request,
}) => {
  const id = await finished(request);
  // The plan had no answer design: the answer says so, the other tabs stay.
  await page.route(`**/api/runs/${id}`, async (route) => {
    const response = await route.fetch();
    const run = await response.json();
    await route.fulfill({
      response,
      json: { ...run, result: { ...run.result, answer: null } },
    });
  });
  await page.goto(`/runs/${id}`);
  const result = page.locator(".etl-result");
  await expect(result).toContainText("这次方案没有设计答案。");
  await expect(result.getByRole("tab")).toHaveCount(5);
  await page.unroute(`**/api/runs/${id}`);

  // A run of the first version shows its plan and report as before.
  await page.goto("/runs/retail-etl-demo");
  await expect(page.getByRole("heading", { name: "ETL 方案" })).toBeVisible();
  await expect(page.locator(".run-report")).toBeVisible();
  await expect(page.locator(".etl-result")).toHaveCount(0);

  // An app's own end outputs named code and brief are texts, shown as texts.
  await page.route(`**/api/runs/${id}`, async (route) => {
    const response = await route.fetch();
    const run = await response.json();
    await route.fulfill({
      response,
      json: {
        ...run,
        outcome: null,
        result: { code: "SELECT 1", brief: "一段说明" },
      },
    });
  });
  await page.goto(`/runs/${id}`);
  await expect(page.locator(".result-card")).toContainText("SELECT 1");
  await expect(page.locator(".result-card")).toContainText("一段说明");
  await expect(page.locator(".etl-result")).toHaveCount(0);
});

test("each bar names its own group, by every dimension when the label columns repeat", async ({
  page,
  request,
}) => {
  const id = await finished(request);
  const labels = page.locator(".answer-chart .chart-label");
  // The design labels the bars by city alone, and 上海 has three member levels.
  await page.route(`**/api/runs/${id}`, async (route) => {
    const response = await route.fetch();
    const run = await response.json();
    const answer = {
      ...run.result.answer,
      chart: { ...run.result.answer.chart, label: ["city"] },
    };
    await route.fulfill({
      response,
      json: { ...run, result: { ...run.result, answer } },
    });
  });
  await page.goto(`/runs/${id}`);
  await expect(labels.first()).toHaveText("上海 · gold");
  await expect(labels).toContainText([
    "上海 · gold",
    "北京 · gold",
    "深圳 · gold",
    "上海 · silver",
  ]);
  await page.unroute(`**/api/runs/${id}`);

  // With one group per city the city alone names it.
  await page.route(`**/api/runs/${id}`, async (route) => {
    const response = await route.fetch();
    const run = await response.json();
    const before = run.result.answer;
    const seen = new Set<string>();
    const rows = before.rows.filter((row: { city: string }) =>
      seen.has(row.city) ? false : (seen.add(row.city), true),
    );
    const answer = {
      ...before,
      rows,
      chart: { ...before.chart, label: ["city"] },
    };
    await route.fulfill({
      response,
      json: { ...run, result: { ...run.result, answer } },
    });
  });
  await page.goto(`/runs/${id}`);
  await expect(labels).toHaveText([
    "上海",
    "北京",
    "深圳",
    "杭州",
    "广州",
    "成都",
    "南京",
  ]);
});
