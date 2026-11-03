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
  // Nothing was dropped after a rejection: no word on it.
  await expect(
    review.getByRole("note", { name: "校验退回后改动的内容" }),
  ).toHaveCount(0);
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
  // Each metric says which result column holds it.
  await expect(
    review
      .getByRole("table", { name: "指标" })
      .getByRole("row")
      .filter({ hasText: "不含运费" }),
  ).toContainText("结果列 sales_amount");
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

test("template v3: write modes and proposed checks on the plan, a failed check has no 通过, a note on it sends the code back", async ({
  page,
}) => {
  await start(page, "按城市和会员等级统计上个月的销售，演示数据检查不通过");
  const review = page.locator(".etl-review");
  // How each table is written, and the checks the plan proposes (notes can hang on them).
  await expect(review.locator(".load-badge")).toHaveText([
    "整表覆盖",
    "按条件替换 · stat_month = ${param.update_month}",
  ]);
  // The result table exists: the plan says which column it adds and where.
  const resultTable = review.getByRole("article", {
    name: "目标表 ads_sales_city_member_m",
  });
  await expect(resultTable).toContainText(
    "加列表已经存在，确认方案后在表里加 1 列：avg_order_value DECIMAL(18,2)，可为空（放在 customer_count 后面）",
  );
  await expect(resultTable.locator(".column-added")).toHaveCount(1);
  await expect(resultTable.locator(".column-type")).toHaveText([
    "DECIMAL(18,2)",
  ]);
  await expect(
    resultTable.getByRole("row", { name: /avg_order_value/ }),
  ).toContainText("新加");
  await expect(
    review
      .getByRole("article", { name: /^目标表 / })
      .first()
      .getByText("加列"),
  ).toHaveCount(0);
  // A check the model dropped after the platform sent the plan back is said at the top.
  await expect(
    review.getByRole("note", { name: "校验退回后改动的内容" }),
  ).toContainText("检查「和上月环比不超过 30%」");
  // So is what the model reviewer still found after the model's rewrites (llm_review).
  const planReview = review.getByRole("note", {
    name: "模型审查方案没改过来的问题",
  });
  await expect(planReview).toContainText("（1 处）");
  await expect(planReview).toContainText(
    "资料写明还没定的事要写成问题（规则 r3）：资料说退货中心的冲红单怎么算还没定，方案直接按负数记了 建议：写成一个问题",
  );
  await expect(planReview).toContainText("依据：冲红单的口径待财务确认");
  const proposed = review.getByRole("region", { name: "建议的数据检查" });
  await expect(proposed).toContainText("每月每个城市每个等级一行");
  await expect(proposed).toContainText(
    "ads_sales_city_member_m 按 stat_month、city、member_level 没有重复的行",
  );
  await expect(proposed).toContainText(
    "ads_sales_city_member_m 的 sum(sales_amount)和 dwd_sales_order_line 的 sum(line_amount)一致（容差 0.1%）",
  );
  await expect(proposed).toContainText("不该有的行 · 警告");
  // A known difference is part of what the check says.
  await expect(proposed).toContainText(
    "ads_sales_city_member_m 里没有满足 sales_amount < 0 的行；已知差异 1 处（退货中心的冲红单按负数记）不算不通过",
  );
  // An error check says it blocks delivery, and the page counts them (I-10).
  await expect(proposed).toContainText("键不重复 · 错误：会挡住交付");
  await expect(proposed.getByRole("note")).toContainText(
    "其中 2 条是错误级，会挡住交付",
  );
  await expect(
    proposed.getByRole("button", { name: "这里不对：没有负的销售额" }),
  ).toBeVisible();
  await confirmPlan(page);

  // The dev run passed, the repeated key failed a check: no 通过.
  await expect(review.getByText("开发环境试算通过")).toBeVisible();
  await expect(
    review.getByRole("note", { name: "模型审查代码没改过来的问题" }),
  ).toContainText("读预测表的地方都先按版本筛选");
  await expect(review.getByRole("alert")).toContainText("数据检查没通过");
  const checks = review.getByRole("region", { name: "数据检查", exact: true });
  await expect(checks).toContainText("3 项检查：2 项通过，1 项不通过");
  const keys = checks
    .getByRole("listitem")
    .filter({ hasText: "每月每个城市每个等级一行" });
  await expect(keys).toContainText("不通过");
  await expect(keys).toContainText(
    "键不重复：按 stat_month、city、member_level 有 1 组重复",
  );
  await expect(
    keys.getByRole("table", { name: "每月每个城市每个等级一行 的例子" }),
  ).toContainText("gold");
  await expect(
    page
      .locator("#approval")
      .getByRole("button", { name: "通过", exact: true }),
  ).toHaveCount(0);

  await keys
    .getByRole("button", { name: "这里不对：每月每个城市每个等级一行" })
    .click();
  await review
    .getByLabel("对「每月每个城市每个等级一行」的意见")
    .fill("同月同城同等级只能有一行");
  await review.getByRole("button", { name: "挂上意见" }).click();
  const sent = posted(page);
  await page
    .locator("#approval")
    .getByRole("button", { name: "改代码", exact: true })
    .click();
  expect((await sent).postDataJSON()).toMatchObject({
    decision: "fix_code",
    notes: [{ item: "check:keys", text: "同月同城同等级只能有一行" }],
  });

  // Round 2: the checks pass, 通过 is back, and the result has the check report.
  await expect(review.getByText("第 2 轮")).toBeVisible();
  await expect(
    review.getByRole("note", { name: "模型审查代码没改过来的问题" }),
  ).toHaveCount(0);
  await expect(checks).toContainText("3 项检查：3 项通过");
  await expect(review.getByRole("alert")).toHaveCount(0);
  await page
    .locator("#approval")
    .getByRole("button", { name: "通过", exact: true })
    .click();
  await expect(page.locator(".detail-heading .status-badge")).toHaveText(
    "已交付",
  );
  const result = page.getByRole("region", { name: "运行结果", exact: true });
  await result.getByRole("tab", { name: "运行报告" }).click();
  await expect(result.locator(".result-text")).toContainText(
    "3 项检查：3 项通过",
  );
});
