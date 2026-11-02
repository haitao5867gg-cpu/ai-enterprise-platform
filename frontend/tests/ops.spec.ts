import { test, expect, type Page } from "@playwright/test";
import type { Summary } from "../lib/ops";

// The mock dashboard (lib/server/mock-ops.ts) has fixed pretend usage for the first two
// apps; budgets and the price stay for the whole dev server, so each test cleans up.
test.describe.configure({ timeout: 60_000 });

const card = (page: Page, label: string) =>
  page
    .getByRole("region", { name: "总览" })
    .locator(".ops-card")
    .filter({ has: page.getByText(label, { exact: true }) });

async function summary(page: Page, days = 30): Promise<Summary> {
  const response = await page.request.get(`/api/ops/summary?days=${days}`);
  expect(response.ok()).toBe(true);
  return response.json();
}

test("看板显示总览、每天的柱状图和各应用，能切换统计期", async ({ page }) => {
  const month = await summary(page, 30);
  const week = await summary(page, 7);
  await page.goto("/ops");
  await expect(
    page.getByRole("heading", { name: "运营看板", level: 1 }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "运营看板", exact: true }),
  ).toHaveClass(/active/);
  await expect(page.getByRole("button", { name: "近 30 天" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(card(page, "运行数").locator("strong")).toHaveText(
    String(month.totals.runs),
  );
  await expect(card(page, "Databricks")).toContainText("没设单价");

  const apps = page.getByRole("table", { name: "各应用" });
  const first = month.apps[0];
  await expect(apps.getByRole("row").nth(1)).toContainText(first.title);
  await expect(apps.getByRole("row").nth(1)).toContainText(
    `成功 ${first.done} · 失败 ${first.error}`,
  );

  // Hovering a bar names the day; the same numbers are in the table view.
  const chart = page.getByRole("img", { name: /每天的运行数/ });
  await expect(chart.locator("path.ops-bar")).not.toHaveCount(0);
  await chart.locator("g").last().hover();
  const today = month.daily.at(-1)!;
  await expect(page.locator("figcaption").first()).toContainText(
    `${today.date}：${today.runs} 次运行`,
  );
  await page.getByText("按天看表格").click();
  await expect(
    page.getByRole("table", { name: "每天的数字" }).getByRole("row"),
  ).toHaveCount(31);

  await page.getByRole("button", { name: "近 7 天" }).click();
  await expect(card(page, "运行数").locator("strong")).toHaveText(
    String(week.totals.runs),
  );
  await expect(
    page.getByRole("table", { name: "每天的数字" }).getByRole("row"),
  ).toHaveCount(8);

  await expect(page.getByRole("region", { name: "资产复用" })).toContainText(
    "资产目录里有",
  );
  expect((await page.request.get("/api/ops/summary?days=5")).status()).toBe(
    422,
  );
});

test("管理员给应用设模型预算，看板和应用页都显示本月用量", async ({ page }) => {
  const { apps } = await summary(page);
  const app = apps[0];
  await page.request.delete(`/api/apps/${app.app}/budget`);
  await page.goto("/ops");
  await page.getByRole("button", { name: `设「${app.title}」的预算` }).click();
  const dialog = page.getByRole("dialog", {
    name: `「${app.title}」的模型预算`,
  });
  await dialog.getByRole("button", { name: "保存" }).click();
  await expect(dialog).toContainText("每月预算要在 $0.01");

  await dialog.getByLabel("每月预算（美元）").fill("0.5");
  await dialog.getByLabel("用到多少时提醒（%）").fill("90");
  await dialog.getByLabel("用完后拦下这个应用的模型调用").uncheck();
  await dialog.getByRole("button", { name: "保存" }).click();
  await expect(dialog).toBeHidden();
  await expect(
    page.getByRole("status").filter({ hasText: "已保存" }),
  ).toHaveText(`已保存「${app.title}」的预算。`);
  const row = page
    .getByRole("table", { name: "各应用" })
    .getByRole("row")
    .filter({ hasText: app.title });
  await expect(row).toContainText("/ $0.50");
  await expect(row).toContainText("已用完（只提醒）");

  const saved = await (
    await page.request.get(`/api/apps/${app.app}/budget`)
  ).json();
  expect(saved).toMatchObject({ monthly_usd: 0.5, warn_at: 0.9, block: false });

  // The app page's release tab shows it too, and an admin can cancel it there.
  await page.goto(`/apps/${app.app}#release`);
  const section = page.getByRole("region", { name: "模型预算" });
  await expect(section).toContainText("已用完（只提醒）");
  await expect(section).toContainText("提醒线 90%");
  await section.getByRole("button", { name: "修改预算" }).click();
  page.once("dialog", (confirm) => confirm.accept());
  await page
    .getByRole("dialog", { name: `「${app.title}」的模型预算` })
    .getByRole("button", { name: "取消预算" })
    .click();
  await expect(section).toContainText("没设预算");
  expect(
    await (await page.request.get(`/api/apps/${app.app}/budget`)).json(),
  ).toBeNull();
});

test("管理员设 Databricks 单价后按时长估算费用，清空就不估算", async ({
  page,
}) => {
  await page.request.put("/api/ops/settings", {
    data: { compute_hourly_usd: null },
  });
  await page.goto("/ops");
  const price = page.getByRole("region", { name: "Databricks 单价" });
  await price.getByLabel("每小时多少美元").fill("2000");
  await price.getByRole("button", { name: "保存" }).click();
  await expect(price).toContainText("单价要在 0 到 1000 之间");

  await price.getByLabel("每小时多少美元").fill("2.5");
  await price.getByRole("button", { name: "保存" }).click();
  await expect(card(page, "Databricks")).toContainText("按单价估算 $");
  const { totals } = await summary(page);
  expect(totals.compute_cost_usd).toBeCloseTo(
    (totals.compute_seconds / 3600) * 2.5,
    3,
  );

  await price.getByLabel("每小时多少美元").fill("");
  await price.getByRole("button", { name: "保存" }).click();
  await expect(card(page, "Databricks")).toContainText("没设单价");
});

test("通知渠道可以订阅模型预算提醒", async ({ page }) => {
  await page.goto("/notification-channels");
  await page.getByRole("button", { name: "新建 Webhook" }).click();
  await expect(page.getByRole("checkbox", { name: "模型预算" })).toBeVisible();
});
