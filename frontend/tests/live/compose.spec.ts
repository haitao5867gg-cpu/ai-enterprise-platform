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

test("Compose: create → waiting → approve → done; reload preserves the result", async ({
  page,
  request,
}, testInfo) => {
  const origins = new Set<string>();
  page.on("request", (r) => {
    if (r.resourceType() === "fetch") origins.add(new URL(r.url()).origin);
  });
  await page.goto("/runs/new");
  await expect(page.getByText("服务状态：正常")).toBeVisible();
  await expect(page.getByText("本地模拟模式")).toHaveCount(0);
  await page
    .getByLabel("问题")
    .fill("请整理一份客户数据清洗方案，并在执行前让我确认。");
  await capture(page, "new-run");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  const id = new URL(page.url()).pathname.split("/").at(-1)!;
  testInfo.annotations.push({ type: "run_id", description: id });
  await capture(page, "approval");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await capture(page, "mobile-approval");
  await page.setViewportSize({ width: 1360, height: 960 });
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  await capture(page, "completed");
  const response = await request.get(`/api/runs/${id}`);
  expect((await response.json()).status).toBe("done");
  expect(
    (await response.json()).messages.map((m: { role: string }) => m.role),
  ).toEqual(["user", "assistant", "user"]);
  await page.reload();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  expect([...origins]).toEqual([new URL(page.url()).origin]);
});

test("Compose: reject with feedback → revised answer → approve → done", async ({
  page,
  request,
}, testInfo) => {
  const comment = "请先补充空值与重复数据的处理规则。";
  await page.goto("/runs/new");
  await page.getByLabel("问题").fill("检查订单数据的字段映射，执行前请确认。");
  await page.getByLabel("你的名字").fill("李雷");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  const id = new URL(page.url()).pathname.split("/").at(-1)!;
  testInfo.annotations.push({ type: "run_id", description: id });
  await page.getByLabel("驳回意见").fill(comment);
  await page.getByRole("button", { name: "驳回", exact: true }).click();
  const messages = page.getByRole("list", { name: "消息记录" });
  await expect(messages.getByRole("listitem")).toHaveCount(4);
  await expect(messages).toContainText(`驳回：${comment}`);
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  await capture(page, "rejected");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  const response = await request.get(`/api/runs/${id}`);
  expect((await response.json()).status).toBe("done");
  expect((await response.json()).messages).toHaveLength(5);
  await page.getByRole("link", { name: "返回运行记录" }).click();
  const row = page
    .getByRole("row")
    .filter({ has: page.locator(`a[href="/runs/${id}"]`) });
  await expect(row).toContainText("检查订单数据的字段映射，执行前请确认。");
  await expect(row).toContainText("李雷");
  await expect(row).toContainText("问答示例");
  await expect(row).toContainText("机器");
  await capture(page, "run-list");
});

test("Compose: upload CSV selects a persistent file source and validation names the failing file", async ({
  page,
}) => {
  await page.goto("/runs/new");
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  await page.getByRole("button", { name: "上传文件", exact: true }).click();
  const panel = page.getByRole("region", { name: "上传文件面板" });
  const name = `真实上传验收-${Date.now()}`;
  await panel.getByLabel("数据集名称").fill(name);
  await panel.getByLabel("选择多个文件，或拖拽到这里").setInputFiles({
    name: "orders.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("id,status,amount\n1,paid,12.5\n2,open,20\n"),
  });
  const uploaded = page.waitForResponse((r) =>
    r.url().endsWith("/api/datasources/files"),
  );
  await panel.getByRole("button", { name: "上传并创建数据源" }).click();
  const response = await uploaded;
  expect(response.status()).toBe(201);
  const body = await response.json();
  await expect(panel).toContainText("orders · 3 列 · 2 行");
  await expect(page.getByLabel("数据源", { exact: true })).toHaveValue(
    body.datasource.datasource_id,
  );
  await panel.getByLabel("数据集名称").fill(`${name}-bad`);
  await panel.getByLabel("选择多个文件，或拖拽到这里").setInputFiles({
    name: "bad.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("id,id\n1,2\n"),
  });
  await panel.getByRole("button", { name: "上传并创建数据源" }).click();
  await expect(panel.getByRole("alert")).toContainText(
    "文件「bad.csv」：表头重复",
  );
  await expect(page.getByLabel("数据源", { exact: true })).toHaveValue(
    body.datasource.datasource_id,
  );
  await page.reload();
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  await expect(
    page
      .getByLabel("数据源", { exact: true })
      .getByRole("option", { name: `${name} · file` }),
  ).toBeAttached();
});
