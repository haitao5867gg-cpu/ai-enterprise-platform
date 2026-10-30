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
  // The draft to check is shown above, so how the run went starts folded,
  // and where the run is still reaches a screen reader.
  await expect(
    page.locator("details.conversation-history"),
  ).not.toHaveAttribute("open");
  await expect(
    page.getByRole("status").filter({ hasText: "应用已暂停，等待人工确认" }),
  ).toHaveCount(1);
  await expect(
    page.getByText("运行过程 · 2 条消息", { exact: true }),
  ).toBeVisible();
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
  // Once there is a result, how the run went is folded until asked for.
  await expect(
    page.locator("details.conversation-history"),
  ).not.toHaveAttribute("open");
  await page.getByText(/^运行过程 · \d+ 条消息$/).click();
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
  await page.getByLabel("修改意见").fill("请先补充空值与重复数据的处理规则。");
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
  // Waiting again with the new draft shown above, 运行过程 is folded.
  const summary = page.getByText("运行过程 · 4 条消息", { exact: true });
  await expect(summary).toBeVisible();
  await expect(
    page.locator("details.conversation-history"),
  ).not.toHaveAttribute("open");
  await summary.click();
  const rejected = page
    .getByRole("list", { name: "消息记录" })
    .getByRole("listitem")
    .nth(2);
  await expect(rejected.locator(".message-meta strong")).toHaveText("李雷");
  await expect(rejected.locator(".message-step")).toHaveText("确认");
  await expect(rejected).toContainText(
    "选了「驳回」，意见：请先补充空值与重复数据的处理规则。",
  );
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  await capture(page, "rejected");
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  // Opened by the person, it stays open when the result comes.
  await expect(page.locator("details.conversation-history")).toHaveAttribute(
    "open",
    "",
  );
  await expect(
    page.getByRole("status").filter({ hasText: "本次运行已结束" }),
  ).toHaveCount(1);

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

test("a failed run says why and offers a retry only when one may help", async ({
  page,
}) => {
  let status = "error";
  let retried: unknown = null;
  const failed = {
    run_id: "retry-run",
    app: "hello",
    status: "error",
    input: { text: "整理客户数据" },
    messages: [],
    pending: null,
    result: null,
    error: "节点「回答」(draft)：模型服务返回 502",
    failed: {
      node: "draft",
      title: "回答",
      error_type: "model_error",
      retryable: true,
    },
    actions: { cancel: false, retry: true, rerun: true },
  };
  await page.route("**/api/runs/retry-run", (route) =>
    route.fulfill({
      json:
        status === "error"
          ? failed
          : {
              ...failed,
              status: "running",
              error: null,
              failed: null,
              actions: { cancel: true, retry: false, rerun: true },
            },
    }),
  );
  await page.route("**/api/runs/retry-run/retry", (route) => {
    retried = route.request().postDataJSON();
    status = "running";
    return route.fulfill({
      status: 202,
      json: { run_id: "retry-run", status: "running" },
    });
  });
  await page.goto("/runs/retry-run");
  const card = page.locator(".failure-card");
  await expect(card).toContainText(
    "在「回答」这一步，模型服务出错了。这类问题多半是暂时的，可以从这一步重试。",
  );
  // The raw error stays visible for whoever needs the detail.
  await expect(card).toContainText("模型服务返回 502");
  await expect(
    card.getByRole("link", { name: "复制并重新发起" }),
  ).toHaveAttribute("href", "/runs/new?from=retry-run");
  await page.getByRole("button", { name: "从「回答」重试" }).click();
  await expect(page.locator(".failure-card")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "取消运行" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "从「回答」重试" }),
  ).toHaveCount(0);
  // The pressed button is gone, so focus stays on the page at its title.
  await expect(page.getByRole("heading", { level: 1 })).toBeFocused();
  expect(retried).toEqual({});

  await page.route("**/api/runs/stuck-run", (route) =>
    route.fulfill({
      json: {
        ...failed,
        run_id: "stuck-run",
        error: "节点「回答」(draft)：超过执行次数上限",
        failed: {
          node: "draft",
          title: "回答",
          error_type: "max_visits",
          retryable: false,
        },
        actions: { cancel: false, retry: false, rerun: true },
      },
    }),
  );
  await page.goto("/runs/stuck-run");
  await expect(page.locator(".failure-card")).toContainText(
    "在「回答」这一步，流程在同一步来回的次数到了上限，重试多半还是一样。",
  );
  await expect(page.getByRole("button", { name: /重试/ })).toHaveCount(0);
});

test("a waiting run is cancelled after a confirmation, then copied and sent again", async ({
  page,
}) => {
  await page.goto("/runs/new");
  await page.getByLabel("你想完成什么？").fill("统计上个月的退货率");
  await page.getByLabel("你的名字").fill("韩梅梅");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  const id = new URL(page.url()).pathname.split("/").at(-1)!;
  const cancel = page.getByRole("button", { name: "取消运行" });
  const dialog = page.getByRole("alertdialog", { name: "取消这次运行？" });
  await cancel.click();
  await expect(dialog).toContainText("已经写进开发环境的表会保留");
  // It opens on the safe choice, and closing it goes back to where it was opened.
  await expect(dialog.getByRole("button", { name: "不取消" })).toBeFocused();
  await dialog.getByRole("button", { name: "不取消" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(cancel).toBeFocused();
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  await cancel.press("Enter");
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(cancel).toBeFocused();
  await cancel.click();
  const cancelled = page.waitForRequest(
    (r) => r.url().endsWith(`/runs/${id}/cancel`) && r.method() === "POST",
  );
  await dialog.getByRole("button", { name: "确定取消" }).click();
  expect((await cancelled).postDataJSON()).toEqual({ actor: "韩梅梅" });
  await expect(page.locator(".failure-card")).toContainText(
    "韩梅梅 取消了这次运行。已经写进开发环境的表会保留。",
  );
  await expect(page.locator(".run-info")).toContainText("取消于「确认」");
  await expect(page.getByRole("heading", { level: 1 })).toBeFocused();
  await expect(page.getByRole("heading", { name: "需要你的确认" })).toHaveCount(
    0,
  );
  await expect(cancel).toHaveCount(0);

  await page
    .locator(".failure-card")
    .getByRole("link", { name: "复制并重新发起" })
    .click();
  await expect(page).toHaveURL(`/runs/new?from=${id}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "复制并重新发起",
  );
  await expect(page.locator(".copy-notice")).toContainText(
    "复制自「统计上个月的退货率」，沿用它的应用版本（v1）。",
  );
  await expect(page.getByLabel("你想完成什么？")).toHaveValue(
    "统计上个月的退货率",
  );
  await expect(page.locator("#app")).toBeDisabled();
  await expect(page.locator("#app")).toHaveValue("hello");
  await page.getByLabel("你想完成什么？").fill("统计上个月各品类的退货率");
  const rerun = page.waitForRequest(
    (r) => r.url().endsWith(`/runs/${id}/rerun`) && r.method() === "POST",
  );
  await page.getByRole("button", { name: "重新发起", exact: true }).click();
  expect((await rerun).postDataJSON()).toEqual({
    input: { text: "统计上个月各品类的退货率" },
    requester: "韩梅梅",
  });
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  expect(new URL(page.url()).pathname).not.toBe(`/runs/${id}`);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "统计上个月各品类的退货率",
  );
  await page.getByRole("link", { name: "查看原运行" }).click();
  await expect(page).toHaveURL(`/runs/${id}`);
  await expect(page.locator(".failure-card")).toContainText("已取消");
});

test("a copy asks for what the copied run asked for and keeps what the form does not show", async ({
  page,
}) => {
  const source = {
    run_id: "old-run",
    app: "hello",
    app_version: 1,
    draft: false,
    status: "done",
    title: "整理客户数据",
    messages: [],
    pending: null,
    result: null,
    error: null,
    input: { text: "整理客户数据", audience: "老板", note: "表单里没有" },
    // The version it ran with asked for more than the app asks for now.
    inputs: [
      {
        key: "text",
        label: "你想完成什么？",
        type: "textarea",
        required: true,
      },
      { key: "audience", label: "给谁看", type: "text", required: true },
    ],
    failed: null,
    actions: { cancel: false, retry: false, rerun: true },
  };
  await page.route("**/api/runs/old-run", (route) =>
    route.fulfill({ json: source }),
  );
  await page.route("**/api/runs/old-etl", (route) =>
    route.fulfill({
      json: {
        ...source,
        run_id: "old-etl",
        app: "etl",
        input: {
          datasource_id: "sample-postgres",
          request: "做一张订单明细宽表",
          language: "sql",
          name: "eval_order_facts",
          release: false,
          tables: "sales.orders",
        },
        inputs: null,
      },
    }),
  );
  const sent: unknown[] = [];
  await page.route("**/api/runs/*/rerun", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({
      status: 201,
      json: { run_id: "copied-run", status: "running" },
    });
  });

  await page.goto("/runs/new?from=old-run");
  await expect(page.getByLabel("给谁看")).toHaveValue("老板");
  await page.getByLabel("给谁看").fill("团队");
  await page.getByRole("button", { name: "重新发起", exact: true }).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0]).toEqual({
    input: { text: "整理客户数据", audience: "团队", note: "表单里没有" },
  });

  await page.goto("/runs/new?from=old-etl");
  await expect(page.getByLabel("你想完成什么？")).toHaveValue(
    "做一张订单明细宽表",
  );
  await expect(page.locator("#datasource")).toHaveValue("sample-postgres");
  await expect(page.locator("#language")).toHaveValue("sql");
  await page.getByRole("button", { name: "重新发起", exact: true }).click();
  await expect.poll(() => sent.length).toBe(2);
  // The bundle name and the dev-only flag stay; nothing the form shows is lost.
  expect(sent[1]).toEqual({
    input: {
      datasource_id: "sample-postgres",
      request: "做一张订单明细宽表",
      language: "sql",
      name: "eval_order_facts",
      release: false,
      tables: "sales.orders",
    },
  });
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
  await page.getByLabel("你的名字").fill("李雷");
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
  await page.getByLabel("修改意见").focus();
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

test("运行过程标明步骤和说话人，有结果后默认收起", async ({ page }) => {
  const hit = (index: number, cited: boolean) => ({
    chunk_id: `chunk-${index}`,
    position: index,
    text: `片段 ${index} 的原文`,
    heading: "有效订单",
    page: null,
    kb_id: "retail-metrics",
    kb_name: "零售数据口径",
    document_id: `doc-${index}`,
    document_name: `0${index}-口径.md`,
    score: 0.8,
    scores: { vector: 0.8, keyword: null, rerank: null },
    index,
    node: "glossary",
    node_title: "查业务口径",
    cited,
  });
  await page.route("**/api/runs/steps-run", (route) =>
    route.fulfill({
      json: {
        run_id: "steps-run",
        app: "retail-etl",
        app_title: "零售数据 ETL",
        status: "done",
        messages: [
          {
            role: "user",
            content: "按城市和月份统计有效订单销售额",
            node: "start",
            node_title: "开始",
            actor: "李雷",
            at: "2026-10-29T08:00:01.123456+00:00",
          },
          {
            role: "assistant",
            content: "方案整理好了：1 张目标表、2 条假设待确认",
            node: "check_plan",
            node_title: "校验方案",
            actor: null,
            at: "2026-10-29T08:02:00+00:00",
          },
          {
            role: "user",
            content: "选了「驳回」，意见：先做全量",
            node: "review",
            node_title: "确认方案",
            actor: "韩梅梅",
            at: "2026-10-29T08:05:00+00:00",
          },
          // From before messages named their step.
          { role: "user", content: "旧消息" },
          { role: "assistant", content: "旧回答" },
        ],
        pending: null,
        result: { answer: "好的 [1]" },
        error: null,
        sources: [hit(1, true), hit(2, false)],
      },
    }),
  );
  await page.goto("/runs/steps-run");
  const result = page.getByRole("region", { name: "运行结果", exact: true });
  await expect(result.locator(".citation-marker")).toHaveText("[1]");
  const history = page.locator("details.conversation-history");
  await expect(history).not.toHaveAttribute("open");
  await page.getByText("运行过程 · 5 条消息", { exact: true }).click();
  await expect(history).toHaveAttribute("open", "");
  const chat = page.getByRole("list", { name: "消息记录" });
  await expect(chat.locator(".message-meta strong")).toHaveText([
    "李雷",
    "校验方案",
    "韩梅梅",
    "未署名",
    "零售数据 ETL",
  ]);
  await expect(chat.locator(".message-avatar").first()).toHaveText("李");
  await expect(chat.locator(".message-step")).toHaveText(["开始", "确认方案"]);
  await expect(chat.locator("time")).toHaveCount(3);
  await expect(chat.locator("time").first()).toHaveAttribute(
    "datetime",
    "2026-10-29T08:00:01.123456+00:00",
  );
  await expect(chat).toContainText("选了「驳回」，意见：先做全量");
  const developer = result.locator(".developer-view");
  await expect(developer).not.toHaveAttribute("open");
  await expect(developer.locator(".result-json")).toContainText('"answer"');
  await expect(
    result.getByText("另有 1 条检索到但未引用", { exact: true }),
  ).toBeVisible();
  await page.getByText("运行过程 · 5 条消息", { exact: true }).click();
  await expect(history).not.toHaveAttribute("open");
});

test("long names stay on the page on a phone, and a new day shows its date", async ({
  page,
}) => {
  await page.route("**/api/runs/long-run", (route) =>
    route.fulfill({
      json: {
        run_id: "long-run",
        app: "hello",
        status: "running",
        messages: [
          {
            role: "user",
            content: "统计有效订单",
            node: "start",
            node_title: "开始",
            actor: "一个名字特别特别特别长的审批人员名字测试用",
            at: "2026-10-29T08:00:00+00:00",
          },
          {
            role: "assistant",
            content: "好的",
            node: "draft",
            node_title: "调用模型生成一份非常长的方案以及说明的节点标题",
            actor: null,
            at: "2026-10-30T09:10:00+00:00",
          },
        ],
        pending: null,
        result: null,
        error: null,
      },
    }),
  );
  await page.setViewportSize({ width: 390, height: 840 });
  await page.goto("/runs/long-run");
  const chat = page.getByRole("list", { name: "消息记录" });
  await expect(chat.locator("time").last()).toContainText("10月30日");
  await expect(chat.locator("time").first()).not.toContainText("月");
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 840 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  }
});

test("a result with no text to read says so and keeps its data in the developer view", async ({
  page,
}) => {
  await page.route("**/api/runs/count-run", (route) =>
    route.fulfill({
      json: {
        run_id: "count-run",
        app: "hello",
        status: "done",
        messages: [],
        pending: null,
        result: { count: 3 },
        error: null,
      },
    }),
  );
  await page.goto("/runs/count-run");
  const result = page.getByRole("region", { name: "运行结果", exact: true });
  await expect(result).toContainText(
    "应用已完成本次运行，结果在下面的开发者视图里。",
  );
  await expect(result.locator(".developer-view .result-json")).toContainText(
    '"count": 3',
  );
  await page.route("**/api/runs/count-run", (route) =>
    route.fulfill({
      json: {
        run_id: "count-run",
        app: "hello",
        status: "done",
        messages: [],
        pending: null,
        result: { reply: null, note: " " },
        error: null,
      },
    }),
  );
  await page.reload();
  await expect(result).toContainText("应用已完成本次运行，没有产出结果。");
});

test("what a person is asked to check reads as Markdown, and its tags open the sources", async ({
  page,
}) => {
  const hit = (index: number, cited: boolean) => ({
    chunk_id: `chunk-${index}`,
    position: index,
    text: `片段 ${index} 的原文`,
    heading: "有效订单",
    page: null,
    kb_id: "retail-metrics",
    kb_name: "零售数据口径",
    document_id: `doc-${index}`,
    document_name: `0${index}-口径.md`,
    score: 0.8,
    scores: { vector: 0.8, keyword: null, rerank: null },
    index,
    node: "glossary",
    node_title: "查业务口径",
    cited,
  });
  await page.route("**/api/runs/show-run", (route) =>
    route.fulfill({
      json: {
        run_id: "show-run",
        app: "hello",
        status: "waiting",
        step: { node: "review", title: "确认方案" },
        messages: [{ role: "user", content: "统计有效订单" }],
        pending: {
          kind: "approval",
          prompt: "请确认",
          options: ["approve", "reject"],
          details: {
            type: "flow_human",
            node: "review",
            show: "**请看** [1]，另见 [2]",
          },
        },
        result: null,
        error: null,
        sources: [hit(1, true), hit(2, false)],
      },
    }),
  );
  await page.goto("/runs/show-run");
  const shown = page.getByRole("region", { name: "确认方案", exact: true });
  expect(
    await shown.evaluate((element) => Boolean(element.closest(".run-main"))),
  ).toBe(true);
  await expect(shown.locator(".human-show strong")).toHaveText("请看");
  await expect(shown.locator(".citation-marker")).toHaveText(["[1]", "[2]"]);
  // The content is shown above, so how the run went starts folded.
  const history = page.locator("details.conversation-history");
  await expect(history).not.toHaveAttribute("open");
  await shown.getByRole("link", { name: "引用来源 2", exact: true }).click();
  await expect(history).toHaveAttribute("open", "");
  await expect(page.locator("details.uncited-sources")).toHaveAttribute(
    "open",
    "",
  );
  await expect(page.locator(".citation-source.highlighted")).toContainText(
    "02-口径.md",
  );
  // Opened by the tag, it still closes with one click.
  await page.getByText(/^运行过程 · \d+ 条消息$/).click();
  await expect(history).not.toHaveAttribute("open");
  // On a phone the decision comes after the page; a link leads there.
  await page.setViewportSize({ width: 390, height: 840 });
  await shown.getByRole("link", { name: "去确认" }).click();
  await expect(page).toHaveURL(/#approval$/);
});
