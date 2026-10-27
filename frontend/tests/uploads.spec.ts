import { expect, test } from "@playwright/test";

test("upload CSVs selects the dataset and reaches plan approval in the mock workbench", async ({
  page,
  request,
}) => {
  await page.goto("/runs/new");
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  await page.getByRole("button", { name: "上传文件", exact: true }).click();
  const panel = page.getByRole("region", { name: "上传文件面板" });
  const name = `上传验收-${Date.now()}`;
  await panel.getByLabel("数据集名称").fill(name);
  await panel.getByLabel("选择多个文件，或拖拽到这里").setInputFiles([
    {
      name: "Orders.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("id,amount\n1,12.5\n2,20\n"),
    },
    {
      name: "Orders.CSV",
      mimeType: "text/csv",
      buffer: Buffer.from("id,status\n3,paid\n"),
    },
  ]);
  const uploaded = page.waitForResponse((response) =>
    response.url().endsWith("/api/datasources/files"),
  );
  await panel.getByRole("button", { name: "上传并创建数据源" }).click();
  const body = await (await uploaded).json();
  await expect(panel).toContainText(`上传成功，已选中「${name}」`);
  await expect(panel).toContainText("orders · 2 列 · 2 行");
  await expect(panel).toContainText("orders_2 · 2 列 · 1 行");
  await expect(page.getByLabel("数据源", { exact: true })).toHaveValue(
    body.datasource.datasource_id,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page
    .getByLabel("你想完成什么？")
    .fill("清理上传的订单数据，保留原始列，执行前确认方案。");
  await page.getByRole("button", { name: "开始运行" }).click();
  const plan = page.getByRole("region", { name: "ETL 方案" });
  await expect(plan).toBeVisible();
  await expect(plan).toContainText(`${body.schema}.orders`);
  await expect(
    page.getByRole("button", { name: "确认", exact: true }),
  ).toBeVisible();
  const id = new URL(page.url()).pathname.split("/").at(-1)!;
  const run = await (await request.get(`/api/runs/${id}`)).json();
  expect(run.status).toBe("waiting");
  expect(run.pending.details.type).toBe("etl_plan");
  expect(run.result).toBeNull();
  // Acceptance deliberately stops at the plan approval, before Databricks.
});

test("a rejected upload names the file and preserves the selected source", async ({
  page,
}) => {
  await page.goto("/runs/new");
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  await page
    .getByLabel("数据源", { exact: true })
    .selectOption("sample-postgres");
  await page.getByRole("button", { name: "上传文件", exact: true }).click();
  const panel = page.getByRole("region", { name: "上传文件面板" });
  await panel.getByLabel("数据集名称").fill(`失败验收-${Date.now()}`);
  await panel.getByLabel("选择多个文件，或拖拽到这里").setInputFiles({
    name: "bad.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("id,ID\n1,2\n"),
  });
  await panel.getByRole("button", { name: "上传并创建数据源" }).click();
  await expect(panel.getByRole("alert")).toContainText(
    "文件「bad.csv」：表头重复",
  );
  await expect(page.getByLabel("数据源", { exact: true })).toHaveValue(
    "sample-postgres",
  );
  await expect(
    panel.getByRole("button", { name: "上传并创建数据源" }),
  ).toBeEnabled();
});

test("drag and drop works on mobile and invalid files have a clear error", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/runs/new");
  await page.getByLabel("应用", { exact: true }).selectOption("etl");
  await page.getByRole("button", { name: "上传文件", exact: true }).click();
  const panel = page.getByRole("region", { name: "上传文件面板" });
  await panel.getByLabel("数据集名称").fill("拖拽测试");
  const files = await page.evaluateHandle(() => {
    const data = new DataTransfer();
    data.items.add(
      new File(["content"], "超长文件名".repeat(30) + ".txt", {
        type: "text/plain",
      }),
    );
    return data;
  });
  await panel
    .locator(".upload-dropzone")
    .dispatchEvent("drop", { dataTransfer: files });
  await panel.getByRole("button", { name: "上传并创建数据源" }).click();
  await expect(panel.getByRole("alert")).toContainText("只支持 .csv 和 .xlsx");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
