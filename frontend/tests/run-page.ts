import type { Page } from "@playwright/test";

// The run page splits into tabs: 待处理 or 结果, 过程 (the steps and the conversation) and
// 详情 (the run's facts and its manual).
export async function runTab(
  page: Page,
  name: "待处理" | "结果" | "过程" | "详情",
) {
  await page.getByRole("tab", { name, exact: true }).click();
}
