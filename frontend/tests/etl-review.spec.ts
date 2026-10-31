import { readFile } from "node:fs/promises";
import { test, expect, type Page } from "@playwright/test";

// The two human steps of the ETL template's second version (docs/api/data-nodes.md,
// "两个人工步骤的页面"), on the mock app retail-etl-v2.
async function start(page: Page, request = "按城市和会员等级统计上个月的销售") {
  await page.goto("/runs/new?app=retail-etl-v2");
  await page.getByLabel("需求").fill(request);
  // Whoever starts it decides its steps, under the same name.
  await page.getByLabel("你的名字").fill("韩梅梅");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(
    page.getByRole("heading", { name: "确认方案", exact: true }),
  ).toBeVisible();
}
function posted(page: Page) {
  return page.waitForRequest(
    (request) =>
      request.method() === "POST" && request.url().endsWith("/resume"),
  );
}
async function confirmPlan(page: Page) {
  await page
    .getByRole("group", { name: "1. 统计哪段时间？" })
    .getByLabel("源库现有的 12 个月")
    .check();
  await page
    .getByRole("group", { name: "2. 退款的订单算不算有效订单？" })
    .getByLabel("不算，整单去掉")
    .check();
  await page
    .getByRole("button", { name: "确认方案，开始做", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "审核试算结果", exact: true }),
  ).toBeVisible();
}

test("plan review: the brief, questions on the page, notes on items turn the decision", async ({
  page,
}) => {
  await start(page);
  const review = page.locator(".etl-review");
  await expect(review.getByText("我们理解你要的是")).toBeVisible();
  await expect(review.locator(".brief-goal")).toHaveText(
    "按月、按城市、按会员等级统计有效订单的 4 个指标",
  );
  // Metrics and dimensions without a source in the glossary are marked.
  await expect(
    review
      .getByRole("table", { name: "指标" })
      .getByRole("row")
      .filter({ hasText: "下单客户数" }),
  ).toContainText("没找到口径文档，按需求理解");
  await expect(
    review
      .getByRole("table", { name: "指标" })
      .getByRole("row")
      .filter({ hasText: "不含运费" }),
  ).not.toContainText("没找到口径文档");
  // [n] in the brief point to the material the plan relies on.
  await expect(
    review.getByRole("link", { name: "引用来源 2" }).first(),
  ).toBeVisible();
  await expect(review.getByText("最后给你的答案")).toBeVisible();
  const shape = review.getByRole("table", { name: "结果表长这样" });
  await expect(shape).toContainText("上海、北京、未知");
  await expect(shape).toContainText("运行后得出");
  // The questions are on the page, with what they affect; the side only counts them.
  const first = review.getByRole("group", { name: "1. 统计哪段时间？" });
  await expect(first).toContainText("影响：销售额、订单数、下单客户数、客单价");
  await expect(page.locator("#approval")).toContainText("还有 2 题没答");
  await expect(page.locator("#approval .approval-question")).toHaveCount(0);

  let posts = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/resume"))
      posts++;
  });
  const confirm = page.getByRole("button", {
    name: "确认方案，开始做",
    exact: true,
  });
  const revise = page.getByRole("button", {
    name: "按我的意见修改方案",
    exact: true,
  });
  await confirm.click();
  await expect(page.locator("#question-q1-error")).toHaveText("请选一个答案");
  await expect(page.locator("#approval [role=alert]")).toContainText(
    "还有 2 题没答",
  );
  expect(posts).toBe(0);
  await first.getByLabel("源库现有的 12 个月").check();
  await review
    .getByRole("group", { name: "2. 退款的订单算不算有效订单？" })
    .getByLabel("不算，整单去掉")
    .check();
  await expect(page.locator("#approval")).toContainText("2 题都答了");
  await expect(confirm).toBeVisible();
  await expect(revise).toBeDisabled();

  // A note on a metric: the decision turns to 按我的意见修改方案.
  await review
    .getByRole("button", { name: "这里不对：客单价", exact: true })
    .click();
  await review.getByLabel("对「客单价」的意见").fill("保留两位小数");
  await review.getByRole("button", { name: "挂上意见" }).click();
  await expect(review.locator(".note-attached")).toContainText("保留两位小数");
  await expect(page.locator(".note-list")).toContainText("挂了 1 条意见");
  await expect(page.locator(".turn-hint")).toContainText(
    "会按「按我的意见修改方案」处理",
  );
  await expect(confirm).toHaveCount(0);
  await expect(revise).toBeEnabled();
  // Removed again, the plan goes on as it is.
  await page
    .locator(".note-list")
    .getByRole("button", { name: "删除对「客单价」的意见" })
    .click();
  await expect(confirm).toBeVisible();

  // Notes on a column of the technical plan and on a question.
  const columns = review.getByRole("table", {
    name: "ads_sales_city_member_m 的列",
  });
  await columns.getByRole("button", { name: "这里不对：城市" }).click();
  await columns.getByLabel("对「城市」的意见").fill("北京市和北京要合并");
  await columns.getByRole("button", { name: "挂上意见" }).click();
  await first.getByRole("button", { name: "这里不对：统计哪段时间？" }).click();
  await first.getByLabel("对「统计哪段时间？」的意见").fill("按自然年");
  await first.getByRole("button", { name: "挂上意见" }).click();
  await expect(page.locator(".note-list")).toContainText("挂了 2 条意见");

  const sent = posted(page);
  await revise.click();
  const body = (await sent).postDataJSON();
  expect(body).toMatchObject({
    decision: "revise",
    values: { q1: "all", q2: "exclude" },
    notes: [
      {
        item: "column:ads_sales_city_member_m.city",
        text: "北京市和北京要合并",
      },
      { item: "question:q1", text: "按自然年" },
    ],
  });
  expect(body.seen).toMatch(/^sha256:/);

  // The plan is written again: the questions answered are rules now, the notes are gone.
  await expect(review.getByText("第 2 轮")).toBeVisible();
  await expect(page.locator(".note-list")).toHaveCount(0);
  await expect(review.locator(".review-questions")).toHaveCount(0);
  await review.getByText("已按口径处理（5 条）").click();
  await expect(review.locator(".plan-rules")).toContainText(
    "已确认：统计哪段时间？——源库现有的 12 个月",
  );
  await confirm.click();
  await expect(
    page.getByRole("heading", { name: "审核试算结果", exact: true }),
  ).toBeVisible();
});

test("data review: a failed dev run has no 通过, a note on a file sends the code back, the answer card and the record", async ({
  page,
}) => {
  await start(page, "按城市统计销售额，演示试算失败");
  await confirmPlan(page);
  const review = page.locator(".etl-review");
  await expect(review.getByRole("tab", { name: "试算结果" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(review.getByRole("alert")).toContainText("试算没通过");
  await expect(review.locator(".review-errors")).toContainText(
    "UNRESOLVED_COLUMN",
  );
  await expect(
    page
      .locator("#approval")
      .getByRole("button", { name: "通过", exact: true }),
  ).toHaveCount(0);
  const fixCode = page
    .locator("#approval")
    .getByRole("button", { name: "改代码", exact: true });
  await expect(fixCode).toBeDisabled();

  // The code is read from the run's steps; a note hangs on the open file.
  await review.getByRole("tab", { name: "代码" }).click();
  await review
    .getByRole("button", { name: "打开 src/ads_sales_city_member_m.py" })
    .click();
  await expect(review.locator(".code-content")).toContainText("groupBy");
  await expect(review.getByRole("link", { name: "下载 ZIP" })).toHaveAttribute(
    "href",
    /\/files\.zip$/,
  );
  await review
    .getByRole("button", { name: "这里不对：src/ads_sales_city_member_m.py" })
    .click();
  await review
    .getByLabel("对「src/ads_sales_city_member_m.py」的意见")
    .fill("折扣列叫 discount");
  await review.getByRole("button", { name: "挂上意见" }).click();
  await expect(fixCode).toBeEnabled();
  const sent = posted(page);
  await fixCode.click();
  expect((await sent).postDataJSON()).toMatchObject({
    decision: "fix_code",
    notes: [
      {
        item: "file:src/ads_sales_city_member_m.py",
        text: "折扣列叫 discount",
      },
    ],
  });

  // Round 2 passes: the answer card, with the totals and a measure that does not add up.
  await expect(review.getByText("第 2 轮")).toBeVisible();
  await expect(review.getByText("开发环境试算通过")).toBeVisible();
  const card = review.getByRole("region", { name: "答案", exact: true });
  await expect(card.locator(".answer-conclusion")).toHaveText(
    "2026-10，上海 · gold 的销售额最高：1,234,567.00 元，占合计 18.4%。",
  );
  const table = card.getByRole("table", { name: "答案表格" });
  await expect(table.getByRole("row").nth(1)).toContainText("1,234,567.00");
  await expect(table.getByRole("row").nth(1)).toContainText("5,035");
  const totals = table.locator("tfoot tr");
  await expect(totals).toContainText("2026-10 合计");
  await expect(totals).toContainText("6,709,854.12");
  await expect(totals.locator("td").nth(4)).toHaveText("—");
  await expect(card.locator(".answer-note")).toHaveText(
    "合计里的「—」：下单客户数不能把各组直接相加。",
  );
  await expect(card).toContainText("共 27 组，显示前 10");
  await expect(
    card.getByRole("img", { name: /^柱状图：销售额（元）/ }),
  ).toBeVisible();
  await expect(card).toContainText("质量：3 条质量规则全部通过，丢弃 12 行");
  // The CSV carries the values as they came.
  const download = page.waitForEvent("download");
  await card.getByRole("button", { name: "下载 CSV" }).click();
  const csv = await readFile((await (await download).path())!, "utf8");
  expect(csv).toContain(
    "城市,会员等级,销售额（元）,订单数,下单客户数,客单价（元）",
  );
  expect(csv).toContain("上海,gold,1234567,5035,812,");
  await card.getByText("技术细节").click();
  await expect(card.locator(".answer-sql")).toContainText("LIMIT 10");
  await expect(card).toContainText("只展示，平台不会执行它");
  // The preview is sorted by the target's key.
  await expect(review.locator(".table-preview")).toContainText(
    "按 stat_month、city、member_level 从小到大排",
  );
  await expect(review.locator(".review-tables")).toContainText(
    "12 行不通过，已丢弃",
  );
  // A quality rule takes a note too.
  await expect(
    review.getByRole("button", { name: "这里不对：valid_amount" }),
  ).toBeVisible();

  // How the plan was confirmed.
  await review.getByRole("tab", { name: "确认记录" }).click();
  const record = review.getByRole("region", { name: "确认方案的记录" });
  await expect(record).toContainText("选了「确认方案，开始做」");
  await expect(record.locator(".suggested-tag")).toHaveCount(2);
  await expect(record).toContainText("sha256:");

  await page
    .locator("#approval")
    .getByRole("button", { name: "通过", exact: true })
    .click();
  await expect(page.locator(".detail-heading .status-badge")).toHaveText(
    "已交付",
  );
});

test("data review: 终止 needs a remark and ends the run stopped", async ({
  page,
}) => {
  await start(page);
  await confirmPlan(page);
  const stop = page
    .locator("#approval")
    .getByRole("button", { name: "终止", exact: true });
  await expect(stop).toBeDisabled();
  await expect(page.locator("#approval")).toContainText(
    "（选「改代码」「改方案」「终止」时必填，挂了意见也算）",
  );
  await page.getByLabel(/^意见/).fill("需求取消了");
  const sent = posted(page);
  await stop.click();
  expect((await sent).postDataJSON()).toMatchObject({
    decision: "stop",
    comment: "需求取消了",
  });
  await expect(page.locator(".detail-heading .status-badge")).toHaveText(
    "已终止",
  );
});

test("data review sends the plan back: the plan review says so at its top", async ({
  page,
}) => {
  await start(page);
  await confirmPlan(page);
  await page
    .locator(".etl-review")
    .getByRole("tab", { name: "口径与方案" })
    .click();
  // Here notes go on columns and quality rules, not on metrics.
  await expect(
    page
      .locator(".etl-review")
      .getByRole("button", { name: "这里不对：客单价", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("table", { name: "ads_sales_city_member_m 的列" })
    .getByRole("button", { name: "这里不对：客单价（元）" })
    .click();
  await page.getByLabel("对「客单价（元）」的意见").fill("按支付金额算");
  await page.getByRole("button", { name: "挂上意见" }).click();
  await page
    .locator("#approval")
    .getByRole("button", { name: "改方案", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "确认方案", exact: true }),
  ).toBeVisible();
  const returned = page.locator(".review-returned");
  await expect(returned).toContainText("审核要求改方案，请再确认一次");
  // A column is named by the platform as table.column.
  await expect(returned).toContainText(
    "ads_sales_city_member_m.avg_order_value：按支付金额算",
  );
});
