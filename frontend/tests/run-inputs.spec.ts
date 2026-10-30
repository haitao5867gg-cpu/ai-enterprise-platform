import { test, expect } from "@playwright/test";

// The new-run page shows a start node's inputs as the app says (flow-apps.md, advanced
// and examples), and a step is decided under a name.

test("the new-run page folds optional inputs under 高级选项 and offers the app's examples", async ({
  page,
}) => {
  await page.goto("/runs/new?app=retail-etl-v2");
  const form = page.getByRole("form", { name: "新建运行" });
  // What the app does, in its own words.
  await expect(form).toContainText("探查数据源、按口径知识库出方案");
  const advanced = form.locator("details.advanced-inputs");
  await expect(advanced).not.toHaveAttribute("open", "");
  await expect(advanced.locator("summary")).toContainText("3 项，不改就按默认");
  await expect(form.locator("#input-tables")).toBeHidden();

  const examples = form.getByRole("group", { name: "可以直接用的例子" });
  await expect(examples.getByRole("button")).toHaveText([
    "按城市和会员等级统计上个月的销售",
    "统计每个品类每月的销售额和毛利率，只看线上渠道。",
    "统计每月新客户数和复购率，按城市拆开。",
  ]);
  await examples.getByRole("button").nth(1).click();
  await expect(form.locator("#input-request")).toHaveValue(
    "统计每个品类每月的销售额和毛利率，只看线上渠道。",
  );

  // A problem with a folded input opens 高级选项 to show it.
  await page.route("**/api/runs", (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          status: 422,
          json: {
            detail: [
              {
                loc: ["body", "input", "tables"],
                msg: "表范围写法不对",
                type: "value_error",
              },
            ],
          },
        })
      : route.fallback(),
  );
  const refused = page.waitForRequest(
    (r) => r.url().endsWith("/api/runs") && r.method() === "POST",
  );
  await form.getByRole("button", { name: "开始运行" }).click();
  // The folded inputs go with their defaults.
  expect((await refused).postDataJSON()).toMatchObject({
    app: "retail-etl-v2",
    input: {
      request: "统计每个品类每月的销售额和毛利率，只看线上渠道。",
      tables: "retail.*",
      language: "pyspark",
    },
  });
  await expect(advanced).toHaveAttribute("open", "");
  await expect(form.locator("#input-tables-error")).toHaveText(
    "表范围写法不对",
  );
});

test("a step is decided under a name: the page asks for it before sending", async ({
  page,
  request,
}) => {
  const created = await request.post("/api/runs", {
    data: { app: "hello", input: { text: "请确认这份清洗方案" } },
  });
  expect(created.status()).toBe(201);
  const { run_id } = (await created.json()) as { run_id: string };
  let sent = 0;
  page.on("request", (r) => {
    if (r.url().endsWith("/resume") && r.method() === "POST") sent += 1;
  });
  await page.goto(`/runs/${run_id}`);
  const approval = page.locator("#approval");
  const name = approval.getByLabel("你的名字");
  await expect(name).toHaveValue("");
  await approval.getByRole("button", { name: "确认", exact: true }).click();
  await expect(approval.getByRole("alert")).toHaveText(
    "先填你的名字再提交，记录里会写是谁处理的。",
  );
  await expect(name).toBeFocused();
  await expect(name).toHaveAttribute("aria-invalid", "true");
  expect(sent).toBe(0);

  await name.fill("韩梅梅");
  await expect(approval.getByRole("alert")).toHaveCount(0);
  const resumed = page.waitForRequest(
    (r) => r.url().endsWith("/resume") && r.method() === "POST",
  );
  await approval.getByRole("button", { name: "确认", exact: true }).click();
  expect((await resumed).postDataJSON()).toMatchObject({
    decision: "approve",
    actor: "韩梅梅",
  });
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
});
