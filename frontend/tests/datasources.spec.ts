import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type { AppDetail, FlowConfig } from "../lib/apps";
import type { Datasource, Health, TestResult } from "../lib/datasources";

// The mock keeps its state for the whole dev server and other specs add apps that use
// 示例库, so app counts are matched loosely and new sources get unique names.
test.describe.configure({ timeout: 60_000 });

const unique = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
const DETAIL_URL = /\/datasources\/[0-9a-f-]{36}$/;
const REFUSED =
  "连接被拒绝：这个地址和端口上没有服务。请检查端口，或确认连接器已经启动。";
const refusedHealth = (): Health => ({
  ok: false,
  latency_ms: null,
  tool_count: null,
  error: "refused",
  message: REFUSED,
  checked_at: new Date().toISOString(),
});
async function noSideScroll(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
}
const row = (page: Page, table: string, name: string) =>
  page
    .getByRole("table", { name: table, exact: true })
    .getByRole("row")
    .filter({ has: page.getByRole("link", { name, exact: true }) });
async function openRegister(page: Page, button = "登记数据源") {
  await page.goto("/datasources");
  await page.getByRole("button", { name: button, exact: true }).click();
  return page.getByRole("dialog");
}
async function register(
  request: APIRequestContext,
  data: { kind: string; connector_url: string; name?: string },
): Promise<Datasource> {
  const response = await request.post("/api/datasources", {
    data: { name: unique("登记"), description: "", ...data },
  });
  expect(response.status()).toBe(201);
  return response.json();
}

test("列表分两组，逐行检查连通状态并统计工具和应用", async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route(
    "**/api/datasources/sample-postgres/health",
    async (route) => {
      await held;
      await route.continue();
    },
  );
  await page.goto("/datasources");
  await expect(
    page.getByRole("heading", { name: "数据源与工具", level: 1 }),
  ).toBeVisible();
  await expect(page.getByText("连接器是数据团队部署的小服务")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "数据源", exact: true, level: 2 }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "工具服务", exact: true, level: 2 }),
  ).toBeVisible();
  const sample = row(page, "数据源", "示例库");
  const status = sample.locator(".datasource-health-cell");
  await expect(status).toHaveText("检查中…");
  await expect(status).toHaveAttribute("aria-busy", "true");
  release();
  await expect(status).toHaveText("正常");
  await expect(status).toHaveAttribute("aria-busy", "false");
  await expect(sample).toContainText("PostgreSQL");
  await expect(sample).toContainText(
    "合成客户与订单数据，仅用于本地工作台演示。",
  );
  await expect(sample).toContainText("6 个工具");
  await expect(sample).toContainText(/\d+ 个应用/);
  await expect(sample.getByRole("button")).toHaveCount(0);
  const legacy = row(page, "数据源", "旧报表库");
  await expect(legacy).toContainText("连接被拒绝");
  await expect(legacy).not.toContainText("connector.test");
  await expect(
    legacy.getByRole("button", { name: "重新检查「旧报表库」" }),
  ).toBeVisible();
  const tickets = row(page, "工具服务", "工单系统");
  await expect(tickets).toContainText("工具服务");
  await expect(tickets).toContainText("3 个工具");
  await expect(tickets).toContainText("0 个应用");
  await expect(
    page.getByRole("table", { name: "工具服务", exact: true }),
  ).not.toContainText("示例库");
  await expect(
    page.getByText(
      "连通状态在打开页面时检查，结果保留 60 秒；应用数按各应用当前版本的配置统计。",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "数据源与工具", exact: true }),
  ).toHaveClass(/active/);
});

test("重新检查：按钮留在原处、带 fresh，并读出结果", async ({ page }) => {
  await page.route("**/api/datasources/sample-mysql/health", (route) =>
    route.fulfill({ status: 503, json: {} }),
  );
  await page.goto("/datasources");
  const mysql = page
    .getByRole("table", { name: "数据源", exact: true })
    .getByRole("row")
    .filter({ has: page.locator('a[href="/datasources/sample-mysql"]') });
  await expect(mysql).toContainText("没查到状态");
  await page.unroute("**/api/datasources/sample-mysql/health");
  const recheck = mysql.getByRole("button", { name: /^重新检查「/ });
  await recheck.click();
  await expect(mysql).toContainText("正常");
  await expect(recheck).toBeVisible();
  await expect(recheck).toBeFocused();

  await expect(row(page, "数据源", "旧报表库")).toContainText("连接被拒绝");
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route(
    "**/api/datasources/legacy-report/health*",
    async (route) => {
      await held;
      await route.continue();
    },
  );
  const button = row(page, "数据源", "旧报表库").getByRole("button");
  await expect(button).toHaveAccessibleName("重新检查「旧报表库」");
  const fresh = page.waitForRequest((request) =>
    request.url().endsWith("/api/datasources/legacy-report/health?fresh=true"),
  );
  await button.click();
  await fresh;
  await expect(button).toHaveText("检查中…");
  // The words on the button are part of its name (WCAG 2.5.3).
  await expect(button).toHaveAccessibleName("检查中…「旧报表库」");
  await expect(button).toHaveAttribute("aria-disabled", "true");
  await expect(button).toBeFocused();
  release();
  await expect(button).toHaveText("重新检查");
  await expect(button).toHaveAccessibleName("重新检查「旧报表库」");
  await expect(button).toBeFocused();
  await expect(page.locator(".sr-only[role=status]")).toHaveText(
    "旧报表库：连接被拒绝",
  );
});

test("登记：先测试连接才能保存，改了地址要重新测试", async ({ page }) => {
  const dialog = await openRegister(page);
  await expect(dialog).toHaveAccessibleName("登记数据源");
  const kind = dialog.getByLabel("类型", { exact: true });
  await expect(kind).toBeFocused();
  await expect(kind).toHaveValue("");
  await expect(dialog).toContainText("类型登记后不能修改。");
  await expect(
    dialog.getByText("数据库账号密码配在连接器上，平台只保存地址。"),
  ).toBeVisible();
  await expect(
    dialog.getByText(
      "填连接器的地址，不是数据库的地址。地址一般以 /mcp 结尾。",
    ),
  ).toBeVisible();
  const save = dialog.getByRole("button", { name: "保存" });
  await expect(save).toBeDisabled();
  await expect(dialog).toContainText("请先选择类型。");
  await kind.selectOption("postgres");
  await expect(dialog).toContainText("请先测试连接。");
  await expect(
    dialog.getByText("测试连接时会检查。类型登记后不能修改。"),
  ).toBeVisible();
  const name = unique("订单库");
  await dialog.getByLabel("名称", { exact: true }).fill(name);
  const url = dialog.getByLabel("连接器地址", { exact: true });
  await url.fill("http://orders.connector.test:8001/mcp");
  const test = dialog.getByRole("button", { name: "测试连接" });
  await test.click();
  const result = dialog.getByRole("status");
  await expect(result).toContainText("连接成功 · 35 毫秒 · 6 个工具");
  await expect(test).toBeFocused();
  await result.getByText("查看工具").click();
  await expect(result.getByText("只读（连接器自报）")).toHaveCount(6);
  await expect(save).toBeEnabled();
  await url.fill("http://orders.connector.test:8002/mcp");
  await expect(save).toBeDisabled();
  await expect(dialog).toContainText("地址或类型改过了，请重新测试连接。");
  await expect(result).toHaveText("");
  await test.click();
  await expect(result).toContainText("连接成功");
  await dialog.getByLabel(/^说明/).fill("线上订单的只读副本，每天凌晨同步");
  await save.click();
  await expect(page).toHaveURL(DETAIL_URL);
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  await expect(page.getByText("正常 · 35 毫秒 · 6 个工具")).toBeVisible();
  await expect(page.locator(".datasource-url")).toHaveText(
    "http://orders.connector.test:8002/mcp",
  );

  await page.goto("/runs/new");
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  await expect(
    page
      .getByLabel("数据源", { exact: true })
      .getByRole("option", { name: `${name} · PostgreSQL` }),
  ).toBeAttached();
});

test("拒绝不安全或写错的地址，不去连接", async ({ page }) => {
  const dialog = await openRegister(page);
  await dialog.getByLabel("类型", { exact: true }).selectOption("mysql");
  await dialog.getByLabel("名称", { exact: true }).fill(unique("不安全"));
  const save = dialog.getByRole("button", { name: "保存" });
  const result = dialog.getByRole("status");
  for (const [url, text] of [
    ["http://u:p@connector-postgres:8001/mcp", "地址里不能带账号密码"],
    ["http://connector-postgres:8001/mcp?api_key=x", "地址里不能带账号密码"],
    ["http://sandbox-relay:8004/mcp", "平台内部服务"],
    ["http://169.254.169.254/", "元数据地址"],
    ["http://2852039166/latest", "元数据地址"],
    [
      "http://elsewhere.example/mcp",
      "主机 elsewhere.example:80 不在允许的范围内，请联系管理员把它加进 AEP_CONNECTOR_HOSTS。",
    ],
    ["mysql://app:secret@db:3306/sales", "这是数据库的地址"],
    ["connector-mysql:8002/mcp", "地址格式不对"],
  ]) {
    await dialog.getByLabel("连接器地址", { exact: true }).fill(url);
    await dialog.getByRole("button", { name: "测试连接" }).click();
    await expect(result).toContainText("测试没有通过");
    await expect(result).toContainText(text);
    await expect(save).toBeDisabled();
    await expect(dialog).toContainText("测试连接通过后才能保存。");
  }
});

test("连不上只显示问题分类，不显示原始报错", async ({ page }) => {
  const dialog = await openRegister(page);
  await dialog.getByLabel("类型", { exact: true }).selectOption("postgres");
  const result = dialog.getByRole("status");
  for (const [host, text] of [
    ["timeout", "超时：连接器在 5 秒内没有响应。"],
    ["refused", "连接被拒绝：这个地址和端口上没有服务。"],
    ["not-mcp", "不是 MCP 服务：能连上这个地址，但对方不是 MCP 连接器。"],
    ["nowhere", "网络不通：找不到这个主机"],
  ]) {
    await dialog
      .getByLabel("连接器地址", { exact: true })
      .fill(`http://${host}.connector.test:8001/mcp`);
    await dialog.getByRole("button", { name: "测试连接" }).click();
    await expect(result).toContainText(text);
    await expect(result).not.toContainText("connector.test");
    await expect(result).not.toContainText("Errno");
  }
});

test("数据库类型缺工具不能保存，工具服务可以，并提示没声明只读的工具", async ({
  page,
}) => {
  const dialog = await openRegister(page);
  await dialog.getByLabel("类型", { exact: true }).selectOption("postgres");
  await dialog.getByLabel("名称", { exact: true }).fill(unique("部分工具"));
  await dialog
    .getByLabel("连接器地址", { exact: true })
    .fill("http://partial.connector.test:8001/mcp");
  const test = dialog.getByRole("button", { name: "测试连接" });
  await test.click();
  const result = dialog.getByRole("status");
  await expect(result).toContainText(
    "缺少 ETL 需要的工具：table_stats、read_rows。",
  );
  await expect(result).toContainText("请把类型选成「工具服务」");
  const save = dialog.getByRole("button", { name: "保存" });
  await expect(save).toBeDisabled();
  await dialog.getByLabel("类型", { exact: true }).selectOption("mcp");
  await expect(dialog).toHaveAccessibleName("登记工具服务");
  await expect(result).toHaveText("");
  await expect(dialog).toContainText("地址或类型改过了，请重新测试连接。");
  await expect(dialog).toContainText(
    "数据库账号密码配在连接器上，平台只保存地址。其他账号和密钥也一样配在连接器上。",
  );
  await test.click();
  await expect(result).toContainText("连接成功 · 35 毫秒 · 4 个工具");
  await expect(save).toBeEnabled();

  await dialog
    .getByLabel("连接器地址", { exact: true })
    .fill("http://tools.connector.test:8010/mcp");
  await test.click();
  await expect(result).toContainText(
    "有 1 个工具没有声明只读。第一阶段平台只支持只读工具，请确认它们不会修改数据。",
  );
  await result.getByText("查看工具").click();
  await expect(
    result.getByRole("listitem").filter({ hasText: "get_customer" }),
  ).toContainText("未声明只读");
  await dialog
    .getByLabel("连接器地址", { exact: true })
    .fill("http://empty.connector.test:8010/mcp");
  await test.click();
  await expect(result).toContainText("连接器没有提供任何工具");
  await expect(save).toBeDisabled();
});

test("详情：先看连通状态，再列工具参数和只读标记，使用情况单独加载", async ({
  page,
}) => {
  await page.goto("/datasources/sample-postgres");
  await expect(page).toHaveTitle("数据源与工具 · AEP");
  await expect(
    page.getByRole("heading", { name: "示例库", level: 1 }),
  ).toBeVisible();
  await expect(page.locator(".asset-tags")).toHaveText("PostgreSQL");
  await expect(page.getByText("正常 · 35 毫秒 · 6 个工具")).toBeVisible();
  await expect(page.getByText(/^检查时间 \d{1,2}:\d{2}:\d{2}$/)).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "工具（6）", level: 2 }),
  ).toBeVisible();
  const tool = (name: string) =>
    page
      .locator("article")
      .filter({ has: page.getByRole("heading", { name, exact: true }) });
  await expect(tool("describe_table")).toContainText(
    "查看表结构：列、类型、能否为空、主键和外键",
  );
  await expect(tool("describe_table")).toContainText("只读（连接器自报）");
  const params = page.getByRole("table", { name: "describe_table 的参数" });
  await expect(params.getByRole("row")).toHaveCount(3);
  await expect(params.getByRole("row").nth(1)).toContainText("schema文字是");
  await expect(params.getByRole("row").nth(2)).toContainText("table文字是");
  const sample = page.getByRole("table", { name: "sample_rows 的参数" });
  await expect(sample.getByRole("row").nth(3)).toHaveText(
    "limit整数否默认 10，1～100",
  );
  await expect(tool("list_schemas")).toContainText("不需要参数。");
  await expect(
    page.getByText("「只读」是连接器自己报的，只用来展示，平台没有核实。"),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /试一下/ })).toHaveCount(0);
  const usage = page
    .getByRole("listitem")
    .filter({ has: page.locator('a[href="/apps/retail-etl"]') });
  await expect(usage).toHaveText(/^零售数据 ETL · v\d+ · 开始节点的默认值$/);
  await expect(usage.getByRole("link")).toHaveAttribute(
    "href",
    "/apps/retail-etl",
  );
  await expect(page.getByText("比如「自然语言 ETL 工作流」")).toBeVisible();

  await page.goto("/datasources/ticket-tools");
  await expect(page.locator(".asset-tags")).toHaveText("工具服务");
  await expect(page.getByText("有 1 个工具没有声明只读。")).toBeVisible();
  await expect(tool("get_customer")).toContainText("未声明只读");
  await expect(tool("get_customer")).toContainText(
    "Look up a customer's profile and their open tickets.",
  );
  await expect(tool("search_tickets")).toContainText("只读（连接器自报）");
  await expect(
    page.getByRole("table", { name: "search_tickets 的参数" }),
  ).toContainText("可选：open、pending、closed");
  await expect(page.getByText("还没有应用用到它。")).toBeVisible();
  // ETL never uses a tool service, so it is no example here.
  await expect(
    page.getByText("开始节点也没有默认值的不算在内。"),
  ).toBeVisible();
  await expect(page.getByText("自然语言 ETL 工作流")).toHaveCount(0);

  await page.route("**/api/datasources/legacy-report/usage", (route) =>
    route.fulfill({ status: 503, json: {} }),
  );
  await page.goto("/datasources/legacy-report");
  await expect(
    page.getByText(
      "连接被拒绝：这个地址和端口上没有服务。请检查端口，或确认连接器已经启动。",
    ),
  ).toBeVisible();
  await expect(
    page.getByText("连接器现在连不上，暂时看不到工具。"),
  ).toBeVisible();
  const usageSection = page.getByRole("region", { name: "被哪些应用使用" });
  await expect(usageSection.getByRole("alert")).toContainText(
    "后端服务暂不可用",
  );
  await page.unroute("**/api/datasources/legacy-report/usage");
  await usageSection.getByRole("button", { name: "重试" }).click();
  await expect(usageSection).toContainText("还没有应用用到它。");
  const fresh = page.waitForRequest((request) =>
    request.url().endsWith("/legacy-report/health?fresh=true"),
  );
  await page.getByRole("button", { name: "重新检查连接并列出工具" }).click();
  await fresh;
  await expect(
    page.getByText("连接器现在连不上，暂时看不到工具。"),
  ).toBeVisible();
  const announced = page.locator(".sr-only[role=status]");
  await expect(announced).toHaveText("旧报表库：连接被拒绝");
  // The same result again is read out again: the region empties while checking.
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route(
    "**/api/datasources/legacy-report/health*",
    async (route) => {
      await held;
      await route.continue();
    },
  );
  const statusButton = page
    .getByRole("region", { name: "连通状态" })
    .getByRole("button");
  await statusButton.click();
  await expect(announced).toHaveText("");
  await expect(statusButton).toHaveAccessibleName("检查中…");
  await expect(
    page.getByRole("region", { name: "工具" }).getByRole("button"),
  ).toHaveAccessibleName("检查中…，完成后列出工具");
  release();
  await expect(announced).toHaveText("旧报表库：连接被拒绝");
  await expect(statusButton).toBeFocused();

  await page.goto("/datasources/no-such-source");
  // Next.js adds its own empty route announcer (also role=alert) outside <main>.
  await expect(page.locator("main").getByRole("alert")).toContainText(
    "没有找到这个数据源或工具服务",
  );
  await expect(page.getByRole("button", { name: "重试" })).toHaveCount(0);
});

test("修改名称和说明不用重新测试，改地址要先测试，重名提示在名称上", async ({
  page,
  request,
}) => {
  const source = await register(request, {
    kind: "mysql",
    connector_url: "http://sales.connector.test:8002/mcp",
  });
  await page.goto(`/datasources/${source.datasource_id}`);
  const edit = page.getByRole("button", { name: "修改", exact: true });
  await edit.click();
  const dialog = page.getByRole("dialog", { name: "修改数据源" });
  const name = dialog.getByLabel("名称", { exact: true });
  await expect(name).toBeFocused();
  await expect(dialog).toContainText("MySQL");
  await expect(dialog).toContainText("类型登记后不能修改。");
  const save = dialog.getByRole("button", { name: "保存" });
  await expect(save).toBeDisabled();
  await expect(dialog).toContainText("还没有修改。");
  const renamed = unique("销售库");
  await name.fill(renamed);
  await dialog.getByLabel(/^说明/).fill("销售和零售两套示例数据，含脏数据");
  await expect(save).toBeEnabled();
  const patch = page.waitForRequest((r) => r.method() === "PATCH");
  await save.click();
  expect((await patch).postDataJSON()).toEqual({
    name: renamed,
    description: "销售和零售两套示例数据，含脏数据",
  });
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(renamed);
  await expect(page.getByText("已保存", { exact: true })).toBeVisible();
  await expect(edit).toBeFocused();
  await expect(
    page.getByText("销售和零售两套示例数据，含脏数据"),
  ).toBeVisible();

  await edit.click();
  await name.fill("示例库");
  await save.click();
  await expect(dialog.getByRole("alert")).toHaveText(
    "已经有叫「示例库」的数据源或工具服务了",
  );
  await expect(name).toHaveAttribute("aria-invalid", "true");
  await dialog.getByRole("button", { name: "关闭" }).click();
  await expect(dialog).toBeHidden();
  await expect(edit).toBeFocused();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(renamed);

  await edit.click();
  await dialog
    .getByLabel("连接器地址", { exact: true })
    .fill("http://refused.connector.test:8002/mcp");
  await expect(save).toBeDisabled();
  await expect(dialog).toContainText("请先测试连接。");
  await dialog.getByRole("button", { name: "测试连接" }).click();
  await expect(dialog.getByRole("status")).toContainText("连接被拒绝");
  await expect(save).toBeDisabled();
  await dialog
    .getByLabel("连接器地址", { exact: true })
    .fill("http://sales2.connector.test:8002/mcp");
  await expect(dialog).toContainText("地址改过了，请重新测试连接。");
  await dialog.getByRole("button", { name: "测试连接" }).click();
  await save.click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".datasource-url")).toHaveText(
    "http://sales2.connector.test:8002/mcp",
  );
  await expect(page.getByText("正常 · 35 毫秒 · 6 个工具")).toBeVisible();
});

test("上传文件登记，详情列出文件连接器的工具", async ({ page }) => {
  const dialog = await openRegister(page, "上传文件");
  await expect(dialog).toHaveAccessibleName("上传文件");
  await expect(dialog.getByLabel("类型", { exact: true })).toHaveValue("file");
  await expect(dialog.getByLabel("连接器地址", { exact: true })).toHaveCount(0);
  const panel = dialog.getByRole("region", { name: "上传文件面板" });
  const name = unique("门店销量");
  await panel.getByLabel("数据集名称").fill(name);
  await panel.getByLabel("选择多个文件，或拖拽到这里").setInputFiles({
    name: "stores.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("store,amount\n上海,12\n北京,20\n"),
  });
  await panel.getByRole("button", { name: "上传并创建数据源" }).click();
  await expect(page).toHaveURL(DETAIL_URL);
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  await expect(page.locator(".asset-tags")).toHaveText("上传文件");
  await expect(page.getByText("上传文件：stores")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "工具（6）", level: 2 }),
  ).toBeVisible();
  // connectors/files counts rows exactly and has one schema per upload.
  const tool = (name: string) =>
    page
      .locator("article")
      .filter({ has: page.getByRole("heading", { name, exact: true }) });
  await expect(tool("table_stats")).toContainText(
    "统计行数，并抽样统计各列的空值比例、不同值个数和常见取值",
  );
  await expect(tool("table_stats")).not.toContainText("估算");
  await expect(tool("list_schemas")).toContainText("列出上传数据集的 schema");
  await page.getByRole("button", { name: "修改", exact: true }).click();
  const edit = page.getByRole("dialog", { name: "修改数据源" });
  await expect(edit.getByLabel("连接器地址", { exact: true })).toHaveAttribute(
    "readonly",
    "",
  );
  await expect(edit).toContainText(
    "上传文件的数据源由平台生成地址，不能修改。",
  );
  await expect(edit.getByRole("button", { name: "测试连接" })).toHaveCount(0);
});

test("ETL 和数据工程节点不出现工具服务，调用工具节点和开始节点可以选", async ({
  page,
  request,
}) => {
  await page.goto("/runs/new");
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  const sources = page.getByLabel("数据源", { exact: true });
  await expect(
    sources.getByRole("option", { name: "示例库 · PostgreSQL" }),
  ).toBeAttached();
  await expect(sources.getByRole("option", { name: /工单系统/ })).toHaveCount(
    0,
  );

  await page.goto("/runs/new?app=retail-etl");
  const start = page.getByLabel("数据源", { exact: false }).first();
  await expect(
    start.locator('optgroup[label="数据源"] option'),
  ).not.toHaveCount(0);
  await expect(
    start.locator('optgroup[label="工具服务"] option', { hasText: "工单系统" }),
  ).toHaveCount(1);

  const app = unique("tools").toLowerCase();
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title: "工具服务测试", from: { blank: true } },
      })
    ).status(),
  ).toBe(201);
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
  await page.goto(`/apps/${app}`);
  const panel = page.getByRole("region", { name: /^节点设置：/ });
  const add = async (label: string) => {
    await page.getByRole("button", { name: "添加节点", exact: true }).click();
    await page
      .getByRole("dialog", { name: /添加节点|新建节点/ })
      .getByRole("button", { name: label, exact: true })
      .click();
  };
  await add("探查数据");
  const profileSource = panel.getByLabel("数据源", { exact: true });
  await expect(
    profileSource.getByRole("option", { name: "示例库 · PostgreSQL" }),
  ).toBeAttached();
  await expect(
    profileSource.getByRole("option", { name: /工单系统/ }),
  ).toHaveCount(0);
  await add("调用工具");
  const toolSource = panel.getByLabel("数据源", { exact: true });
  await expect(
    toolSource.getByRole("option", { name: "工单系统 · 工具服务" }),
  ).toBeAttached();
  await toolSource.selectOption("ticket-tools");
  const tools = panel.getByLabel("工具", { exact: true });
  await expect(
    tools.getByRole("option", { name: "get_customer（未声明只读）" }),
  ).toBeAttached();
  await expect(
    tools.getByRole("option", { name: "search_tickets", exact: true }),
  ).toBeAttached();
});

test("改了地址后，编排应用里列出的是新连接器的工具", async ({
  page,
  request,
}) => {
  const source = await register(request, {
    kind: "mcp",
    connector_url: "http://tools.connector.test:8010/mcp",
  });
  const app = unique("toolcache").toLowerCase();
  const title = unique("工具列表");
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title, from: { blank: true } },
      })
    ).status(),
  ).toBe(201);
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
  page.on("dialog", (dialog) => void dialog.accept());
  const toolsOfNewNode = async () => {
    await page.getByRole("button", { name: "添加节点", exact: true }).click();
    await page
      .getByRole("dialog", { name: /添加节点|新建节点/ })
      .getByRole("button", { name: "调用工具", exact: true })
      .click();
    const panel = page.getByRole("region", { name: /^节点设置：/ });
    await panel
      .getByLabel("数据源", { exact: true })
      .selectOption(source.datasource_id);
    return panel.getByLabel("工具", { exact: true });
  };
  await page.goto(`/apps/${app}`);
  let tools = await toolsOfNewNode();
  await expect(
    tools.getByRole("option", { name: "search_tickets", exact: true }),
  ).toBeAttached();

  // Move the source to another connector without reloading the page.
  await page.getByRole("link", { name: "数据源与工具", exact: true }).click();
  await page.getByRole("link", { name: source.name, exact: true }).click();
  await page.getByRole("button", { name: "修改", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "修改工具服务" });
  await dialog.getByLabel("连接器地址").fill("http://crm.connector.test/mcp");
  await dialog.getByRole("button", { name: "测试连接" }).click();
  await expect(dialog.getByRole("status")).toContainText("连接成功");
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("已保存", { exact: true })).toBeVisible();

  await page.getByRole("link", { name: "应用", exact: true }).click();
  await page.getByRole("link", { name: title }).first().click();
  tools = await toolsOfNewNode();
  await expect(
    tools.getByRole("option", { name: "describe_table", exact: true }),
  ).toBeAttached();
  await expect(
    tools.getByRole("option", { name: "search_tickets", exact: true }),
  ).toHaveCount(0);
});

test("只改名称不会播报一次没做过的检查", async ({ page, request }) => {
  const source = await register(request, {
    kind: "mcp",
    connector_url: "http://tools.connector.test:8010/mcp",
  });
  await page.goto(`/datasources/${source.datasource_id}`);
  const announced = page.locator(".sr-only[role=status]");
  await page
    .getByRole("region", { name: "连通状态" })
    .getByRole("button")
    .click();
  await expect(announced).toHaveText(`${source.name}：正常`);
  await page.getByRole("button", { name: "修改", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "修改工具服务" });
  const renamed = unique("改名");
  await dialog.getByLabel("名称", { exact: true }).fill(renamed);
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByRole("heading", { name: renamed })).toBeVisible();
  await expect(announced).toHaveText(`${source.name}：正常`);
});

test("详情：列工具时连不上，连通状态跟着变，不会在「正常」下面报错", async ({
  page,
  request,
}) => {
  const source = await register(request, {
    kind: "mysql",
    connector_url: "http://stale.connector.test:8002/mcp",
  });
  const base = `**/api/datasources/${source.datasource_id}`;
  // The status cached when the page opened says 正常; then the connector stops.
  const reread: string[] = [];
  let down = false;
  await page.route(`${base}/health*`, async (route) => {
    if (!down) return route.continue();
    reread.push(route.request().url());
    await route.fulfill({ json: refusedHealth() });
  });
  let listed = 0;
  await page.route(`${base}/tools`, async (route) => {
    listed += 1;
    down = true;
    await route.fulfill({ status: 502, json: { detail: REFUSED } });
  });
  await page.goto(`/datasources/${source.datasource_id}`);
  const status = page.getByRole("region", { name: "连通状态" });
  await expect(status).toContainText(REFUSED);
  await expect(status).not.toContainText("正常");
  await expect(
    page.getByText("连接器现在连不上，暂时看不到工具。"),
  ).toBeVisible();
  // Read again from the cache /tools just updated, not checked afresh.
  expect(reread).toHaveLength(1);
  expect(reread[0]).not.toContain("fresh=true");
  expect(listed).toBe(1);

  // A connector that answers but fails to list its tools: shown once, no loop.
  await page.unroute(`${base}/health*`);
  await page.unroute(`${base}/tools`);
  const failed = "连接器列工具时报错，请让管理员查看连接器的日志。";
  listed = 0;
  await page.route(`${base}/tools`, async (route) => {
    listed += 1;
    await route.fulfill({ status: 502, json: { detail: failed } });
  });
  await page.reload();
  const tools = page.getByRole("region", { name: "工具", exact: true });
  await expect(tools.getByRole("alert")).toContainText(failed);
  await expect(status).toContainText("正常");
  await expect(status.getByRole("button")).toHaveText("重新检查");
  // Nothing asks for the tools again by itself.
  await page.waitForTimeout(500);
  expect(listed).toBe(1);
  const fresh = page.waitForRequest((r) =>
    r.url().endsWith("health?fresh=true"),
  );
  await tools.getByRole("button", { name: "重试" }).click();
  await fresh;
  await expect.poll(() => listed).toBe(2);
  await expect(tools.getByRole("alert")).toContainText(failed);
});

test("修改保存后焦点回到「修改」，之前点过工具那里的重新检查也一样", async ({
  page,
  request,
}) => {
  const source = await register(request, {
    kind: "mysql",
    connector_url: "http://focus.connector.test:8002/mcp",
  });
  const health = `**/api/datasources/${source.datasource_id}/health*`;
  await page.route(health, (route) => route.fulfill({ json: refusedHealth() }));
  await page.goto(`/datasources/${source.datasource_id}`);
  const recheck = page.getByRole("button", { name: "重新检查连接并列出工具" });
  const checked = page.waitForResponse((r) => r.url().endsWith("fresh=true"));
  await recheck.click();
  await checked;
  await expect(recheck).toBeVisible();
  await page.unroute(health);
  const edit = page.getByRole("button", { name: "修改", exact: true });
  await edit.click();
  const dialog = page.getByRole("dialog", { name: "修改数据源" });
  await dialog
    .getByLabel("连接器地址", { exact: true })
    .fill("http://focus2.connector.test:8002/mcp");
  await dialog.getByRole("button", { name: "测试连接" }).click();
  await expect(dialog.getByRole("status")).toContainText("连接成功");
  await dialog.getByRole("button", { name: "保存" }).click();
  await expect(dialog).toBeHidden();
  await expect(
    page.getByRole("heading", { name: "工具（6）", level: 2 }),
  ).toBeVisible();
  await expect(edit).toBeFocused();
});

test("详情：连接器给的参数说明格式不对，页面照常显示", async ({ page }) => {
  await page.route("**/api/datasources/ticket-tools/tools", (route) =>
    route.fulfill({
      json: [
        {
          name: "odd_tool",
          description: "",
          read_only: true,
          input_schema: {
            type: "object",
            required: "q",
            properties: {
              q: { description: { en: "query" } },
              n: { description: 5, type: { $ref: "#/defs/n" }, enum: "a" },
              m: { anyOf: { type: "string" }, minimum: "1" },
              k: { type: ["integer", "null"], maximum: 9 },
              c: { type: "constructor" },
              x: "not a schema",
            },
          },
        },
      ],
    }),
  );
  await page.goto("/datasources/ticket-tools");
  const params = page.getByRole("table", { name: "odd_tool 的参数" });
  const rows = params.getByRole("row");
  await expect(rows).toHaveCount(7);
  await expect(rows.nth(1)).toHaveText("q—否—");
  await expect(rows.nth(2)).toHaveText("n—否—");
  await expect(rows.nth(3)).toHaveText("m—否—");
  await expect(rows.nth(4)).toHaveText("k整数否最大 9");
  await expect(rows.nth(5)).toHaveText("cconstructor否—");
  await expect(rows.nth(6)).toHaveText("x—否—");
});

test("工具服务进不了 ETL：保存、编排运行和 ETL 运行都用中文报错", async ({
  request,
}) => {
  const app = unique("etl-tools").toLowerCase();
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title: "工具服务进 ETL", from: { app: "retail-etl" } },
      })
    ).status(),
  ).toBe(201);
  const detail: AppDetail = await (
    await request.get(`/api/apps/${app}`)
  ).json();
  // The app's options name each data source by id, name and type only.
  for (const option of detail.options.datasources)
    expect(Object.keys(option).sort()).toEqual([
      "datasource_id",
      "kind",
      "name",
    ]);
  const config = structuredClone(detail.current.config) as FlowConfig;
  const profile = config.graph.nodes.find((node) => node.id === "profile");
  if (profile?.type !== "etl_profile") throw new Error("no profile node");
  profile.datasource = "ticket-tools";
  const saved = await request.put(`/api/apps/${app}/draft`, {
    data: { config },
  });
  expect(saved.status()).toBe(422);
  expect(JSON.stringify(await saved.json())).toContain(
    "「工单系统」是工具服务，这里要选数据库或上传文件的数据源",
  );

  const failure = async (data: unknown) => {
    const created = await request.post("/api/runs", { data });
    expect(created.status()).toBe(201);
    const { run_id } = await created.json();
    let run: { status: string; error: string | null; failed: unknown } = {
      status: "",
      error: null,
      failed: null,
    };
    await expect
      .poll(async () => {
        run = await (await request.get(`/api/runs/${run_id}`)).json();
        return run.status;
      })
      .toBe("error");
    return run;
  };
  const flowRun = await failure({
    app,
    input: { datasource: "ticket-tools", request: "统计销售额" },
  });
  expect(flowRun.error).toContain(
    "「工单系统」是工具服务，不能用来探查或抽取数据，请选数据库或上传文件的数据源",
  );
  expect(flowRun.failed).toMatchObject({
    node: "profile",
    error_type: "input_error",
  });
  const etlRun = await failure({
    app: "etl",
    input: { request: "统计销售额", datasource_id: "ticket-tools" },
  });
  expect(etlRun.error).toBe(
    "「工单系统」是工具服务，ETL 要选数据库或上传文件的数据源",
  );
  expect(etlRun.failed).toMatchObject({
    node: "profile",
    error_type: "input_error",
  });

  // An exported app names its data sources the same way, without their addresses.
  const blank = unique("export").toLowerCase();
  await request.post("/api/apps", {
    data: { app: blank, title: "导出工单", from: { blank: true } },
  });
  const plain: AppDetail = await (
    await request.get(`/api/apps/${blank}`)
  ).json();
  const flow = structuredClone(plain.current.config) as FlowConfig;
  flow.graph.nodes.splice(1, 0, {
    id: "lookup",
    type: "tool",
    title: "查工单",
    datasource: "ticket-tools",
    tool: "search_tickets",
    args: {},
  });
  flow.graph.edges = [
    { from: "start", to: "lookup" },
    { from: "lookup", to: "answer" },
    { from: "answer", to: "end" },
  ];
  expect(
    (
      await request.put(`/api/apps/${blank}/draft`, { data: { config: flow } })
    ).status(),
  ).toBe(200);
  await request.post(`/api/apps/${blank}/draft/publish`, {
    data: { note: "加上工单" },
  });
  const exported = await request.get(`/api/apps/${blank}/versions/2/export`);
  expect(JSON.parse(await exported.text()).requires.datasources).toEqual([
    { datasource_id: "ticket-tools", name: "工单系统", kind: "mcp" },
  ]);
});

test("手机布局不横向滚动，对话框可以只用键盘操作并把焦点还回去", async ({
  page,
}) => {
  await page.goto("/datasources");
  const register = page.getByRole("button", { name: "登记数据源" });
  await register.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "登记数据源" });
  const kind = dialog.getByLabel("类型", { exact: true });
  await expect(kind).toBeFocused();
  await kind.selectOption("postgres");
  await page.keyboard.press("Tab");
  await expect(dialog.getByLabel("名称", { exact: true })).toBeFocused();
  await page.keyboard.type("键盘登记");
  await page.keyboard.press("Tab");
  await page.keyboard.type("http://keyboard.connector.test:8001/mcp");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "测试连接" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(dialog.getByRole("status")).toContainText("连接成功");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(register).toBeFocused();
  const services = page.getByRole("button", { name: "登记工具服务" });
  await services.click();
  await expect(
    page.getByRole("dialog", { name: "登记工具服务" }).getByLabel("类型"),
  ).toHaveValue("mcp");
  await page
    .getByRole("dialog", { name: "登记工具服务" })
    .getByRole("button", { name: "关闭" })
    .click();
  await expect(services).toBeFocused();

  await page.setViewportSize({ width: 700, height: 844 });
  await page.goto("/datasources");
  // Between phone and desktop the table keeps its width and scrolls in its box.
  expect(
    await page
      .getByRole("table", { name: "数据源", exact: true })
      .evaluate((table) => table.getBoundingClientRect().width),
  ).toBeGreaterThanOrEqual(760);
  await noSideScroll(page);

  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/datasources");
    const sample = row(page, "数据源", "示例库");
    await expect(sample).toContainText("正常");
    await expect(sample).toContainText("6 个工具");
    await noSideScroll(page);
    for (const box of await page.locator(".table-scroll").all())
      expect(await box.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(
        true,
      );
    const tag = row(page, "数据源", "MySQL 示例库").getByText("MySQL", {
      exact: true,
    });
    await expect(tag).toBeInViewport({ ratio: 1 });
    await page.getByRole("button", { name: "登记数据源" }).click();
    await page
      .getByRole("dialog")
      .getByLabel("类型", { exact: true })
      .selectOption("mysql");
    await noSideScroll(page);
    await page.keyboard.press("Escape");
    await page.goto("/datasources/ticket-tools");
    await expect(
      page.getByRole("heading", { name: "工具（3）", level: 2 }),
    ).toBeVisible();
    await noSideScroll(page);
    // At 390 every parameter column shows; at 320 a long name may scroll in its box.
    if (width === 390)
      for (const box of await page.locator(".table-scroll").all())
        expect(
          await box.evaluate((el) => el.scrollWidth <= el.clientWidth),
        ).toBe(true);
  }
});

test("mock 接口契约：测试、登记、修改、连通状态缓存、使用情况和错误", async ({
  request,
}) => {
  const ok = await request.post("/api/datasources/test", {
    data: {
      kind: "postgres",
      connector_url: "http://connector-postgres:8001/mcp",
    },
  });
  const result: TestResult = await ok.json();
  expect(result).toMatchObject({
    ok: true,
    latency_ms: 35,
    missing_tools: [],
    problems: [],
  });
  expect(result.tools.map((tool) => tool.read_only)).toEqual(
    Array(6).fill(true),
  );
  expect(Object.keys(result).sort()).toEqual(
    ["latency_ms", "missing_tools", "ok", "problems", "tools"].sort(),
  );
  for (const [data, status, detail] of [
    [
      { kind: "file", connector_url: "http://x.connector.test/mcp" },
      422,
      "上传文件的数据源请用「上传文件」登记，不用填地址",
    ],
    [
      { kind: "oracle", connector_url: "http://x" },
      422,
      "「类型」的格式、长度或范围不正确",
    ],
    [{ kind: "mcp", connector_url: null }, 422, "请求字段不能为 null"],
  ] as const) {
    const response = await request.post("/api/datasources/test", { data });
    expect(response.status()).toBe(status);
    expect((await response.json()).detail).toBe(detail);
  }
  // Checked as the backend does: the host as written, before anything is connected.
  for (const [url, code, text] of [
    ["postgres:5432/mcp", "bad_url", "地址格式不对"],
    ["postgresql+psycopg://u@h/db", "bad_url", "这是数据库的地址"],
    ["mysql+pymysql://db/x", "bad_url", "这是数据库的地址"],
    ["mongodb+srv://u:p@cluster/db", "bad_url", "这是数据库的地址"],
    ["http://127.1:8001/mcp", "bad_url", "地址格式不对"],
    ["http://0x7f.1:8001/mcp", "bad_url", "地址格式不对"],
    ["http://167772161/mcp", "bad_url", "地址格式不对"],
    ["http://connector-postgres./mcp", "bad_url", "地址格式不对"],
    ["http://a!b.connector.test/mcp", "bad_url", "地址格式不对"],
    ["http://224.0.0.1/mcp", "bad_url", "地址格式不对"],
    ["http://u:p@2852039166/", "metadata", "元数据地址"],
    ["http://u:p@connector-postgres:8001/mcp", "credentials", "账号密码"],
  ]) {
    const response = await request.post("/api/datasources/test", {
      data: { kind: "postgres", connector_url: url },
    });
    const { problems }: TestResult = await response.json();
    expect(
      problems.map((item) => item.code),
      url,
    ).toEqual([code]);
    expect(problems[0].message, url).toContain(text);
  }
  const name = unique("契约");
  const failed = await request.post("/api/datasources", {
    data: {
      name,
      kind: "postgres",
      connector_url: "http://refused.connector.test:8001/mcp",
    },
  });
  expect(failed.status()).toBe(422);
  expect((await failed.json()).detail).toBe(
    "测试连接没有通过。连接被拒绝：这个地址和端口上没有服务。请检查端口，或确认连接器已经启动。",
  );
  const created = await register(request, {
    name,
    kind: "postgres",
    connector_url: "HTTP://Contract.Connector.Test:80/mcp?schema=sales",
  });
  expect(created.connector_url).toBe(
    "http://contract.connector.test/mcp?schema=sales",
  );
  const duplicate = await request.post("/api/datasources", {
    data: { name, kind: "mcp", connector_url: "http://x.connector.test/mcp" },
  });
  expect(duplicate.status()).toBe(409);
  expect((await duplicate.json()).detail).toBe(
    `已经有叫「${name}」的数据源或工具服务了`,
  );
  const path = `/api/datasources/${created.datasource_id}`;
  expect(
    (await (await request.patch(path, { data: { kind: "mysql" } })).json())
      .detail,
  ).toBe("类型登记后不能修改");
  const moved = await request.patch(path, {
    data: { connector_url: "http://timeout.connector.test/mcp" },
  });
  expect(moved.status()).toBe(422);
  expect((await moved.json()).detail).toMatch(/^测试连接没有通过。超时：/);
  const renamed = await request.patch(path, {
    data: { name: `${name}-改`, connector_url: created.connector_url },
  });
  expect(await renamed.json()).toMatchObject({
    name: `${name}-改`,
    connector_url: created.connector_url,
  });

  const first: Health = await (await request.get(`${path}/health`)).json();
  const cached: Health = await (await request.get(`${path}/health`)).json();
  expect(cached.checked_at).toBe(first.checked_at);
  await new Promise((resolve) => setTimeout(resolve, 5));
  const fresh: Health = await (
    await request.get(`${path}/health?fresh=true`)
  ).json();
  expect(fresh.checked_at).not.toBe(first.checked_at);
  expect(fresh).toMatchObject({ ok: true, tool_count: 6, error: null });
  const legacy = await request.get("/api/datasources/legacy-report/tools");
  expect(legacy.status()).toBe(502);
  expect((await legacy.json()).detail).toMatch(/^连接被拒绝：/);
  const refused: Health = await (
    await request.get("/api/datasources/legacy-report/health")
  ).json();
  expect(refused).toMatchObject({
    ok: false,
    tool_count: null,
    error: "refused",
  });

  // Usage: a constant that holds the id (any case) counts as written in the node.
  const service = await register(request, {
    kind: "mcp",
    connector_url: "http://tools.connector.test:8010/mcp",
  });
  const app = unique("usage").toLowerCase();
  await request.post("/api/apps", {
    data: { app, title: "用到工单", from: { blank: true } },
  });
  const detail: AppDetail = await (
    await request.get(`/api/apps/${app}`)
  ).json();
  const config = structuredClone(detail.current.config) as FlowConfig;
  config.env.vars = [
    { key: "crm", type: "string", value: service.datasource_id.toUpperCase() },
  ];
  config.graph.nodes.splice(1, 0, {
    id: "lookup",
    type: "tool",
    title: "查工单",
    datasource: "{{env.crm}}",
    tool: "search_tickets",
    args: {},
  });
  config.graph.edges = [
    { from: "start", to: "lookup" },
    { from: "lookup", to: "answer" },
    { from: "answer", to: "end" },
  ];
  expect(
    (
      await request.put(`/api/apps/${app}/draft`, { data: { config } })
    ).status(),
  ).toBe(200);
  expect(
    (
      await request.post(`/api/apps/${app}/draft/publish`, {
        data: { note: "加上工单" },
      })
    ).status(),
  ).toBe(200);
  expect(
    await (
      await request.get(`/api/datasources/${service.datasource_id}/usage`)
    ).json(),
  ).toEqual({
    apps: [{ app, title: "用到工单", version: 2, via: "literal" }],
  });
  const missing = await request.get("/api/datasources/no-such-source");
  expect(missing.status()).toBe(404);
  expect((await missing.json()).detail).toBe("没有找到这个数据源或工具服务");

  // A taken name is reported before an upload's address change, as in the backend.
  const upload = await request.post("/api/datasources/files", {
    multipart: {
      name: unique("契约文件"),
      files: {
        name: "a.csv",
        mimeType: "text/csv",
        buffer: Buffer.from("a,b\n1,2\n"),
      },
    },
  });
  expect(upload.status()).toBe(201);
  const { datasource } = await upload.json();
  const clash = await request.patch(
    `/api/datasources/${datasource.datasource_id}`,
    { data: { name: "示例库", connector_url: "http://x.connector.test/mcp" } },
  );
  expect(clash.status()).toBe(409);
  const moveFile = await request.patch(
    `/api/datasources/${datasource.datasource_id}`,
    { data: { connector_url: "http://x.connector.test/mcp" } },
  );
  expect(moveFile.status()).toBe(422);
  expect((await moveFile.json()).detail).toBe("上传文件的数据源不能修改地址");
});
