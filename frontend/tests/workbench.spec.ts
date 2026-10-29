import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";

async function capture(page: Page, name: string) {
  if (process.env.AEP_CAPTURE_SCREENSHOTS !== "1") return;
  await mkdir("docs/screenshots", { recursive: true });
  await page.screenshot({
    path: `docs/screenshots/${name}.png`,
    fullPage: true,
    animations: "disabled",
  });
}

test("new run → approval → done, then reject with a comment", async ({
  page,
  request,
}) => {
  const origins = new Set<string>();
  page.on("request", (request) => {
    if (request.resourceType() === "fetch")
      origins.add(new URL(request.url()).origin);
  });
  await page.goto("/runs/new");
  const create = page.getByRole("button", { name: "开始运行" });
  await expect(create).toBeDisabled();
  await page
    .getByLabel("你想完成什么？")
    .fill("请整理一份客户数据清洗方案，并在执行前让我确认。");
  await page.getByLabel("你的名字").fill(" 李雷 ");
  await capture(page, "new-run");
  const created = page.waitForResponse(
    (r) => r.url().endsWith("/api/runs") && r.request().method() === "POST",
  );
  await create.click();
  expect((await created).status()).toBe(201);
  expect((await created).request().postDataJSON().requester).toBe("李雷");
  await expect(page).toHaveURL(/\/runs\/[a-z0-9-]+$/);
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "请整理一份客户数据清洗方案，并在执行前让我确认。",
  );
  await expect(page.locator(".detail-meta")).toContainText("李雷 提出 · 刚刚");
  await expect(page.locator(".run-info")).toContainText("停在「确认」");
  await expect(
    page.getByRole("list", { name: "消息记录" }).getByRole("listitem"),
  ).toHaveCount(2);
  const id = new URL(page.url()).pathname.split("/").at(-1)!;
  const invalidDecision = await request.post(`/api/runs/${id}/resume`, {
    data: { decision: ["approve"], comment: "" },
  });
  expect(invalidDecision.status()).toBe(422);
  await capture(page, "approval");
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "确认", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("list", { name: "消息记录" }).getByRole("listitem"),
  ).toHaveCount(3);
  await capture(page, "completed");
  await page.reload();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();

  await page.getByRole("main").getByRole("link", { name: "新建运行" }).click();
  // The name is remembered for the next run.
  await expect(page.getByLabel("你的名字")).toHaveValue("李雷");
  await page
    .getByLabel("你想完成什么？")
    .fill("检查订单数据的字段映射，执行前请确认。");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  await page.getByLabel("驳回意见").fill("请先补充空值与重复数据的处理规则。");
  const resumed = page.waitForRequest(
    (r) => r.url().endsWith("/resume") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "驳回", exact: true }).click();
  // The name given when starting the run is who decides, too.
  expect((await resumed).postDataJSON()).toEqual({
    decision: "reject",
    comment: "请先补充空值与重复数据的处理规则。",
    actor: "李雷",
  });
  await expect(
    page.getByRole("list", { name: "消息记录" }).getByRole("listitem"),
  ).toHaveCount(4);
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  await expect(page.getByRole("list", { name: "消息记录" })).toContainText(
    "请先补充空值与重复数据的处理规则。",
  );
  await capture(page, "rejected");
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();

  await page.getByRole("link", { name: "返回运行记录" }).click();
  const row = page
    .getByRole("row")
    .filter({ has: page.locator(`a[href="/runs/${id}"]`) });
  await expect(row).toContainText("客户数据清洗方案");
  await expect(row).toContainText("李雷");
  await expect(row).toContainText("问答示例");
  await expect(row).toContainText("已完成");
  // How long people took depends on this test's speed; the list test checks it.
  await expect(row).toContainText(/机器 \d+ 秒/);
  await expect(page.getByRole("table")).toContainText("订单数据");
  await capture(page, "run-list");
  expect([...origins]).toEqual(["http://127.0.0.1:3100"]);
});

test("empty list, filters, loading failure and retry", async ({ page }) => {
  await page.route("**/api/runs", (route) => route.fulfill({ json: [] }));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "从一个需求开始" }),
  ).toBeVisible();
  await page.unroute("**/api/runs");
  await page.route("**/api/runs", (route) =>
    route.fulfill({ status: 503, json: { detail: "Unavailable" } }),
  );
  await page.getByRole("button", { name: "刷新运行列表" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "后端服务暂不可用",
  );
  await page.unroute("**/api/runs");
  await page.route("**/api/runs", (route) =>
    route.fulfill({
      json: [{ run_id: "filter-test", app: "hello", status: "waiting" }],
    }),
  );
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.getByRole("table")).toBeVisible();
  await page.getByRole("button", { name: "已完成", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "这个状态下还没有运行" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "等待确认", exact: true }).click();
  // A run without a title is named after its app.
  await expect(page.getByRole("table")).toContainText("hello 的运行");
});

test("the run list says what each run is about, who asked, where it is and how long it took", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-10-29T10:00:00Z") });
  const running = {
    run_id: "list-running",
    app: "retail-etl",
    app_title: "零售数据 ETL",
    app_version: 3,
    draft: false,
    status: "running",
    title: "按城市和月份统计有效订单销售额",
    created_by: "李雷",
    created_at: "2026-10-29T07:50:00Z",
    updated_at: "2026-10-29T09:55:00Z",
    finished_at: null,
    step: { node: "plan", title: "出方案" },
    waiting_since: null,
    wait_seconds: 7200,
  };
  const old = {
    run_id: "list-old",
    app: "hello",
    app_title: "问答示例",
    app_version: 1,
    draft: false,
    status: "done",
    title: null,
    created_by: null,
    created_at: "2026-10-27T10:00:00Z",
    updated_at: "2026-10-27T10:03:00Z",
    finished_at: "2026-10-27T10:03:00Z",
    step: { node: "end", title: "结束" },
    waiting_since: null,
    // From before waits were counted.
    wait_seconds: null,
  };
  // Development mode loads the list twice on mounting; what counts is what comes later.
  let later = false;
  let refreshes = 0;
  await page.route("**/api/runs", (route) => {
    if (later) refreshes += 1;
    return route.fulfill({
      json: !later
        ? [running, old]
        : [
            {
              ...running,
              status: "waiting",
              step: { node: "review", title: "确认需求" },
              waiting_since: "2026-10-29T10:00:00Z",
            },
            old,
          ],
    });
  });
  await page.goto("/");
  const rows = page.getByRole("table").getByRole("row");
  const first = rows.nth(1);
  await expect(first).toContainText("按城市和月份统计有效订单销售额");
  await expect(first).toContainText("李雷");
  await expect(first).toContainText("零售数据 ETL");
  await expect(first).toContainText("v3");
  await expect(first).toContainText("进行到「出方案」");
  await expect(first).toContainText("2 小时前");
  await expect(first.locator("time")).toHaveAttribute("title", /2026/);
  await expect(first.locator(".run-duration span")).toHaveText([
    "机器 10 分钟",
    "等人 2 小时",
  ]);
  await expect(first).not.toContainText("list-running");
  const second = rows.nth(2);
  await expect(second).toContainText("问答示例 的运行");
  await expect(second).toContainText("2 天前");
  await expect(second).toContainText("用时 3 分钟");

  // While a run is running the list refreshes itself, and stops once none is.
  later = true;
  await page.clock.fastForward(10_000);
  await expect(first).toContainText("停在「确认需求」");
  expect(refreshes).toBe(1);
  const extra = page.waitForRequest("**/api/runs", { timeout: 1_000 }).then(
    () => true,
    () => false,
  );
  await page.clock.fastForward(30_000);
  expect(await extra).toBe(false);
  expect(refreshes).toBe(1);
});

test("failed creation preserves the input and can be retried", async ({
  page,
}) => {
  await page.route("**/api/runs", (route) =>
    route.fulfill({ status: 422, json: { detail: "Invalid input" } }),
  );
  await page.goto("/runs/new");
  await page.getByLabel("你想完成什么？").fill("保留这条需求");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "提交内容未通过校验",
  );
  await expect(page.getByLabel("你想完成什么？")).toHaveValue("保留这条需求");
  await expect(page.getByRole("button", { name: "开始运行" })).toBeEnabled();
});

test("not found and failed runs have useful states; terminal polling stops", async ({
  page,
}) => {
  await page.goto("/runs/does-not-exist");
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "没有找到这条运行记录",
  );
  let reads = 0;
  await page.route("**/api/runs/failed-run", (route) => {
    reads += 1;
    return route.fulfill({
      json: {
        run_id: "failed-run",
        app: "hello",
        status: "error",
        messages: [],
        pending: null,
        result: null,
        error: "模拟执行失败，请检查输入。",
      },
    });
  });
  await page.goto("/runs/failed-run");
  await expect(page.getByRole("heading", { name: "运行失败", exact: true })).toBeVisible();
  await expect(page.getByText("模拟执行失败，请检查输入。")).toBeVisible();
  const completedReads = reads;
  await page.waitForTimeout(2_300);
  expect(reads).toBe(completedReads);
});

test("polls every two seconds, recovers, honors options and prevents duplicate resumes", async ({
  page,
}) => {
  const readTimes: number[] = [];
  let observePolls = false;
  let failNext = false;
  const waiting = {
    run_id: "poll-test",
    app: "hello",
    status: "waiting",
    messages: [{ role: "assistant", content: "先确认这条消息" }],
    pending: { kind: "approval", prompt: "继续吗？", options: ["approve"] },
    result: null,
    error: null,
  };
  await page.route("**/api/runs/poll-test", (route) => {
    if (observePolls) readTimes.push(Date.now());
    if (failNext) {
      failNext = false;
      return route.fulfill({ status: 502, json: {} });
    }
    return route.fulfill({ json: waiting });
  });
  await page.goto("/runs/poll-test");
  await expect(
    page.getByRole("button", { name: "驳回", exact: true }),
  ).toBeDisabled();
  // Strict Mode may abort an initial read. Inject failure after the UI is ready.
  observePolls = true;
  failNext = true;
  await expect(page.getByRole("main").getByRole("alert")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "确认", exact: true }),
  ).toBeDisabled();
  await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
  expect(readTimes[1] - readTimes[0]).toBeGreaterThanOrEqual(1_900);
  let posts = 0;
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/runs/poll-test/resume", async (route) => {
    posts += 1;
    await held;
    await route.fulfill({ status: 409, json: { detail: "Already resumed" } });
  });
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("button", { name: "提交中…" })).toBeDisabled();
  await expect.poll(() => posts).toBe(1);
  release();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "运行状态已发生变化",
  );
  await expect(
    page.getByRole("button", { name: "确认", exact: true }),
  ).toBeEnabled();
});

test("mobile layout fits and approval can be reached using the keyboard", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/runs/new");
  await page.getByLabel("你想完成什么？").fill("移动端也能确认运行。");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  await page.getByLabel("驳回意见").focus();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "确认", exact: true }),
  ).toBeFocused();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await capture(page, "mobile-approval");
});

test("conversation renders replies as Markdown, keeps typed text as is and folds JSON", async ({
  page,
}) => {
  await page.route("**/api/runs/chat-run", (route) =>
    route.fulfill({
      json: {
        run_id: "chat-run",
        app: "hello",
        status: "done",
        messages: [
          { role: "user", content: "按月统计 **销售额**" },
          {
            role: "assistant",
            content:
              "## 方案\n\n- 先建明细表\n- 再建汇总表\n注意：集群正在冷启动\n\n![图](https://example.com/p.png)",
          },
          {
            role: "assistant",
            content: '{"summary": "先建明细表", "targets": []}',
          },
          {
            role: "assistant",
            content: '```json\n{"targets": {"orders": "df = source()"}}\n```',
          },
        ],
        pending: null,
        result: null,
        error: null,
      },
    }),
  );
  await page.goto("/runs/chat-run");
  const chat = page.getByRole("list", { name: "消息记录" });
  await expect(chat.locator(":scope > li")).toHaveCount(4);
  await expect(chat.getByText("按月统计 **销售额**")).toBeVisible();
  const reply = chat.locator(".message-markdown");
  await expect(reply.getByRole("heading", { name: "方案" })).toBeVisible();
  await expect(reply.getByRole("listitem")).toHaveText([
    "先建明细表",
    /^再建汇总表\s*注意：集群正在冷启动$/,
  ]);
  await expect(reply.locator("li br")).toHaveCount(1);
  await expect(chat).not.toContainText("## 方案");
  // An image in a reply is a link, never loaded with the page.
  await expect(reply.locator("img")).toHaveCount(0);
  await expect(reply.getByRole("link", { name: "图片：图" })).toHaveAttribute(
    "href",
    "https://example.com/p.png",
  );
  const data = chat.locator("details.message-data");
  await expect(data).toHaveCount(2);
  await expect(data.locator("summary").first()).toHaveText(
    "模型返回的结构化数据",
  );
  await expect(data.locator("pre").first()).toBeHidden();
  await data.locator("summary").first().click();
  await expect(data.locator("pre").first()).toContainText(
    '"summary": "先建明细表"',
  );
  await expect(chat).not.toContainText("```");
  // Development leftovers and English slogans are gone from the shell.
  await expect(page.getByText("hello / etl")).toHaveCount(0);
  await expect(page.getByText("默认工作空间")).toHaveCount(0);
  await expect(page.getByText("EVERY STEP, CONNECTED")).toHaveCount(0);
});
