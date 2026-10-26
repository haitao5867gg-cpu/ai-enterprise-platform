import { expect, test, type Page } from "@playwright/test";
import { mkdir, readFile } from "node:fs/promises";

async function capture(page: Page, name: string) {
  if (process.env.AEP_CAPTURE_SCREENSHOTS !== "1") return;
  await mkdir("docs/screenshots", { recursive: true });
  await page.screenshot({
    path: `docs/screenshots/${name}.png`,
    fullPage: true,
    animations: "disabled",
  });
}

// The dev mock uses stored ZIP entries. Verify all UTF-8 contents and entry count.
function readStoredZip(bytes: Buffer) {
  const files: Record<string, string> = {};
  let offset = 0;
  while (bytes.readUInt32LE(offset) === 0x04034b50) {
    expect(bytes.readUInt16LE(offset + 8)).toBe(0);
    const size = bytes.readUInt32LE(offset + 18);
    const nameLength = bytes.readUInt16LE(offset + 26);
    const extraLength = bytes.readUInt16LE(offset + 28);
    const name = bytes
      .subarray(offset + 30, offset + 30 + nameLength)
      .toString("utf8");
    const start = offset + 30 + nameLength + extraLength;
    files[name] = bytes.subarray(start, start + size).toString("utf8");
    offset = start + size;
  }
  expect(bytes.readUInt32LE(offset)).toBe(0x02014b50);
  expect(bytes.readUInt32LE(bytes.length - 22)).toBe(0x06054b50);
  expect(bytes.readUInt16LE(bytes.length - 12)).toBe(Object.keys(files).length);
  return files;
}

test("ETL: choose source → request → plan → reject → approve → code → ZIP", async ({
  page,
  request,
}, testInfo) => {
  await page.goto("/runs/new");
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  await expect(page.getByLabel("你想完成什么？")).toBeDisabled();
  await expect(page.getByRole("button", { name: "开始运行" })).toBeDisabled();
  await expect(
    page
      .getByLabel("数据源", { exact: true })
      .getByRole("option", { name: "示例库 · postgres" }),
  ).toBeAttached();
  await page
    .getByLabel("数据源", { exact: true })
    .selectOption("sample-postgres");
  await expect(
    page.getByText("合成客户与订单数据，仅用于本地工作台演示。"),
  ).toBeVisible();
  await expect(page.getByLabel("生成语言")).toHaveValue("pyspark");
  await page
    .getByLabel("你想完成什么？")
    .fill("清洗客户和订单，去重并按更新时间增量加载订单。");
  const created = page.waitForRequest(
    (r) => r.url().endsWith("/api/runs") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "开始运行" }).click();
  expect((await created).postDataJSON()).toEqual({
    app: "etl",
    input: {
      datasource_id: "sample-postgres",
      request: "清洗客户和订单，去重并按更新时间增量加载订单。",
      language: "pyspark",
    },
  });
  const plan = page.getByRole("region", { name: "ETL 方案" });
  await expect(plan).toBeVisible();
  const orders = plan.getByRole("article", { name: "目标表 orders_clean" });
  await expect(orders).toContainText("public.orders");
  await expect(orders).toContainText("按订单号去重");
  await expect(
    orders.getByRole("table", { name: "orders_clean 列映射" }),
  ).toContainText("CAST(amount AS DECIMAL(18, 2))");
  await expect(orders.locator(".plan-facts")).toContainText("主键order_id");
  await expect(orders).toContainText("增量键：updated_at");
  await expect(orders).toContainText("不满足时：丢弃记录");
  await expect(orders).toContainText("不满足时：停止管道");
  const customers = plan.getByRole("article", {
    name: "目标表 customers_clean",
  });
  await expect(customers).toContainText("全量加载");
  await expect(customers).toContainText("不满足时：告警");
  const id = new URL(page.url()).pathname.split("/").at(-1)!;
  expect((await request.get(`/api/runs/${id}/files.zip`)).status()).toBe(404);
  const detail = await (await request.get(`/api/runs/${id}`)).json();
  const trace = page.getByRole("link", { name: "在 Langfuse 中查看" });
  await expect(trace).toHaveAttribute(
    "href",
    `http://localhost:3100/project/aep-local/traces/${detail.trace_id}`,
  );
  await expect(trace).toHaveAttribute("rel", "noopener noreferrer");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1360, height: 960 });
  await capture(page, "etl-plan");
  const comment = "补充金额非负校验，并保留更新时间。";
  await page.getByLabel("驳回意见").fill(comment);
  const rejected = page.waitForRequest(
    (r) => r.url().endsWith("/resume") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "驳回", exact: true }).click();
  expect((await rejected).postDataJSON()).toEqual({
    decision: "reject",
    comment,
  });
  await expect(plan).toContainText(`已根据意见调整：${comment}`);
  await expect(orders).toContainText(`调整要求：${comment}`);
  await expect(page.getByLabel("驳回意见")).toHaveValue("");
  await page.reload();
  await expect(plan).toContainText(comment);
  await page.getByRole("button", { name: "确认", exact: true }).click();
  const generated = page.getByRole("region", { name: "生成的文件" });
  await expect(generated).toBeVisible();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "ETL 方案" })).toHaveCount(0);
  await expect(generated).toContainText("代码检查通过");
  await expect(generated.locator(".code-content")).toContainText(
    "def customers_clean",
  );
  await expect(generated.locator(".syntax-keyword").first()).toHaveText("from");
  const tree = page.getByRole("navigation", { name: "生成文件目录" });
  await expect(tree.getByRole("button")).toHaveCount(5);
  await tree
    .getByRole("button", { name: "打开 src/orders_clean.py", exact: true })
    .click();
  await expect(generated.locator(".code-content")).toContainText(
    "def orders_clean",
  );
  await tree
    .getByRole("button", { name: "打开 databricks.yml", exact: true })
    .click();
  await expect(generated.locator(".code-content")).toContainText("bundle:");
  await expect(generated.locator(".syntax-key").first()).toHaveText("bundle");
  await tree
    .getByRole("button", { name: "打开 src/orders_clean.py", exact: true })
    .click();
  await capture(page, "etl-code");
  const downloaded = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载 ZIP" }).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe("etl_demo.zip");
  const saved = testInfo.outputPath("etl_demo.zip");
  await download.saveAs(saved);
  const finished = await (await request.get(`/api/runs/${id}`)).json();
  expect(readStoredZip(await readFile(saved))).toEqual(finished.result.files);
  await page.reload();
  await expect(generated).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await capture(page, "etl-mobile-code");
});

test("ETL can generate SQL and switch back to hello", async ({ page }) => {
  await page.goto("/runs/new");
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  await page
    .getByLabel("数据源", { exact: true })
    .selectOption("sample-postgres");
  await page.getByLabel("生成语言").selectOption("sql");
  await page.getByLabel("你想完成什么？").fill("生成 SQL 清洗管道");
  const created = page.waitForRequest(
    (r) => r.url().endsWith("/api/runs") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "开始运行" }).click();
  expect((await created).postDataJSON().input.language).toBe("sql");
  await expect(page.getByRole("region", { name: "ETL 方案" })).toContainText(
    "SQL",
  );
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.locator(".language-sql")).toContainText(
    "CREATE OR REFRESH MATERIALIZED VIEW",
  );
  await expect(page.locator(".syntax-keyword").first()).toHaveText("CREATE");
  await page.getByRole("main").getByRole("link", { name: "新建运行" }).click();
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  await page.getByLabel("应用", { exact: true }).selectOption("hello");
  await expect(page.getByLabel("数据源", { exact: true })).toHaveCount(0);
  await page.getByLabel("你想完成什么？").fill("仍然可以使用 hello");
  const hello = page.waitForRequest(
    (r) => r.url().endsWith("/api/runs") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "开始运行" }).click();
  expect((await hello).postDataJSON()).toEqual({
    app: "hello",
    input: { text: "仍然可以使用 hello" },
  });
});

test("data source loading, failure, retry and empty state block ETL creation", async ({
  page,
}) => {
  let state: "failed" | "empty" | "valid" = "failed";
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/datasources", async (route) => {
    await held;
    if (state === "failed") return route.fulfill({ status: 503, json: {} });
    return route.fulfill({
      json:
        state === "empty"
          ? []
          : [
              {
                datasource_id: "sample-postgres",
                name: "示例库",
                kind: "postgres",
                description: "测试数据",
              },
            ],
    });
  });
  await page.goto("/runs/new");
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  await expect(page.getByLabel("数据源", { exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "开始运行" })).toBeDisabled();
  release();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "后端服务暂不可用",
  );
  state = "empty";
  await page.getByRole("button", { name: "重试" }).click();
  await expect(
    page.getByText("暂无可用数据源，请先登记数据源后刷新。"),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "开始运行" })).toBeDisabled();
  state = "valid";
  await page.getByRole("button", { name: "刷新数据源" }).click();
  await page
    .getByLabel("数据源", { exact: true })
    .selectOption("sample-postgres");
  await page.getByLabel("你想完成什么？").fill("数据源恢复后可以开始");
  await expect(page.getByRole("button", { name: "开始运行" })).toBeEnabled();
});

for (const [name, details] of [
  ["future", { type: "future_app", plan: { arbitrary: true } }],
  ["malformed", { type: "etl_plan", plan: { targets: [null] } }],
  ["null", null],
] as const) {
  test(`approval accepts ${name} details and keeps prompt and buttons`, async ({
    page,
  }) => {
    let status = "waiting";
    await page.route(`**/api/runs/${name}-details`, (route) =>
      route.fulfill({
        json: {
          run_id: `${name}-details`,
          app: "etl",
          status,
          messages: [],
          pending:
            status === "waiting"
              ? {
                  kind: "approval",
                  prompt: "通用确认提示",
                  options: ["approve", "reject"],
                  details,
                }
              : null,
          result: null,
          error: null,
          trace_id: null,
        },
      }),
    );
    await page.route(`**/api/runs/${name}-details/resume`, (route) => {
      status = "done";
      return route.fulfill({
        status: 202,
        json: { run_id: `${name}-details`, status: "running" },
      });
    });
    await page.goto(`/runs/${name}-details`);
    await expect(page.getByText("通用确认提示")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "驳回", exact: true }),
    ).toBeEnabled();
    await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
    await expect(page.getByRole("region", { name: "ETL 方案" })).toHaveCount(0);
    await expect(
      page.getByRole("link", { name: "在 Langfuse 中查看" }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "确认", exact: true }).click();
    await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  });
}

for (const hasFiles of [false, true]) {
  test(`failed checks show all problems, files=${hasFiles}, generated text stays inert`, async ({
    page,
  }) => {
    const content =
      '# <img src=x onerror="alert(1)">\nprint("<script>alert(2)</script>")\n';
    await page.route("**/api/runs/failed-checks", (route) =>
      route.fulfill({
        json: {
          run_id: "failed-checks",
          app: "etl",
          status: "done",
          messages: [],
          pending: null,
          error: null,
          result: {
            bundle: "failed_bundle",
            language: "pyspark",
            files: hasFiles
              ? {
                  "src/example.py": content,
                  "notes.txt": "<b>保持原文</b>",
                }
              : {},
            checks: {
              passed: false,
              problems: ["orders_clean 缺少增量键", "目标列不存在"],
            },
          },
        },
      }),
    );
    await page.goto("/runs/failed-checks");
    const problems = page.getByRole("main").getByRole("alert");
    await expect(problems).toContainText("代码检查未通过");
    await expect(problems).toContainText("orders_clean 缺少增量键");
    await expect(problems).toContainText("目标列不存在");
    await expect(page.getByText("代码检查通过", { exact: true })).toHaveCount(
      0,
    );
    if (!hasFiles) {
      await expect(page.getByText("本次运行未生成文件。")).toBeVisible();
      await expect(page.getByRole("link", { name: "下载 ZIP" })).toHaveCount(0);
    } else {
      await expect(page.locator(".code-content")).toHaveText(content);
      await expect(
        page.locator(".code-content img, .code-content script"),
      ).toHaveCount(0);
      await page
        .getByRole("button", { name: "打开 notes.txt", exact: true })
        .click();
      await expect(page.locator(".language-text")).toHaveText(
        "<b>保持原文</b>",
      );
      await expect(page.locator(".code-content b")).toHaveCount(0);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

for (const status of ["passed", "failed"] as const) {
  test(`a ${status} Databricks dev run is shown with the result`, async ({
    page,
  }) => {
    await page.route("**/api/runs/dev-run", (route) =>
      route.fulfill({
        json: {
          run_id: "dev-run",
          app: "etl",
          status: "done",
          messages: [],
          pending: null,
          error: null,
          result: {
            bundle: "etl_dev",
            language: "pyspark",
            files: {
              "src/aep_harness.py": "MARKER = 1\n",
              "src/pipeline.py": "x = 1\n",
            },
            checks: { passed: true, problems: [] },
            verification: {
              status,
              stage: "run",
              mode: "job",
              url: "https://adb-1.test.databricks.azure.cn/#job/1/run/2",
              errors:
                status === "failed"
                  ? [
                      "order_detail: AnalysisException: [NESTED_AGGREGATE_FUNCTION]",
                    ]
                  : [],
              warnings: [],
              tables: [
                {
                  name: "order_detail",
                  status: status === "passed" ? "COMPLETED" : "FAILED",
                  rows: status === "passed" ? 4894 : null,
                  dropped: status === "passed" ? 6 : 0,
                },
              ],
            },
          },
        },
      }),
    );
    await page.goto("/runs/dev-run");
    // The pipeline file opens first, not the harness that sorts before it.
    await expect(page.locator(".code-toolbar")).toContainText(
      "src/pipeline.py",
    );
    const result = page.locator(".result-card");
    const link = page.getByRole("link", { name: "在 Databricks 中查看" });
    await expect(link).toHaveAttribute(
      "href",
      "https://adb-1.test.databricks.azure.cn/#job/1/run/2",
    );
    if (status === "passed") {
      await expect(
        page.getByText("Databricks dev 试跑通过 · Job（已有集群）"),
      ).toBeVisible();
      await expect(
        page.getByText("order_detail：4894 行，质量规则丢弃 6 行"),
      ).toBeVisible();
      await expect(result).toContainText("并在 Databricks dev 目标上试跑通过");
      await expect(result.locator(".result-icon.failed")).toHaveCount(0);
    } else {
      const alert = page.getByRole("main").getByRole("alert");
      await expect(alert).toContainText("Databricks dev 试跑未通过");
      await expect(alert).toContainText("NESTED_AGGREGATE_FUNCTION");
      await expect(result).toContainText("Databricks dev 试跑未通过");
      await expect(result.locator(".result-icon.failed")).toHaveCount(1);
    }
  });
}
