import { test, expect } from "@playwright/test";

// Schedules are kept by the mock (lib/server/mock-schedules.ts); see
// docs/api/schedules.md. The mock does not run them on time.
test("在接入页新建、编辑、停用、立即跑一次和删除定时", async ({ page }) => {
  await page.goto("/apps/hello#access");
  const section = page.getByRole("region", { name: "定时运行" });
  await expect(section).toContainText("还没有定时");

  await section.getByRole("button", { name: "新建定时" }).click();
  const create = page.getByRole("dialog", { name: "新建定时" });
  await create.getByLabel("名称").fill("工作日早报");
  await create.getByLabel("什么时候跑").selectOption("weekdays");
  await create.getByLabel("几点").selectOption("9");
  await create.getByLabel("几分").selectOption("30");
  const next = create.getByRole("list", { name: "接下来 3 次" });
  await expect(next.getByRole("listitem")).toHaveCount(3);
  await expect(next.getByRole("listitem").first()).toContainText("09:30");
  await expect(next).not.toContainText(/周六|周日/);

  // Written by hand: what is wrong is said before saving.
  await create.getByLabel("什么时候跑").selectOption("custom");
  await expect(create.getByLabel("cron")).toHaveValue("30 9 * * 1-5");
  await create.getByLabel("cron").fill("30 25 * * 1-5");
  await expect(create.getByRole("alert")).toHaveText(
    "定时写法不对：第 2 段（时）的 25 超出 0–23",
  );
  await create.getByLabel("cron").fill("30 9 * * 1-5");
  await expect(next.getByRole("listitem")).toHaveCount(3);

  // The input is checked as when starting a run.
  await create.getByRole("button", { name: "新建定时" }).click();
  await expect(create).toContainText("你想完成什么？为必填项");
  await create.getByLabel("你想完成什么？").fill("汇总昨天的工单");
  await create.getByRole("button", { name: "新建定时" }).click();
  await expect(create).toBeHidden();
  await expect(section.getByRole("status")).toContainText(
    "已保存定时「工作日早报」，下一次",
  );

  const table = section.getByRole("table", { name: "定时运行" });
  const row = table.getByRole("row", { name: /工作日早报/ });
  await expect(row).toContainText("周一到周五 09:30");
  await expect(row).toContainText("Asia/Shanghai");
  await expect(row).toContainText("还没跑过");

  // Editing opens on what was saved.
  await row.getByRole("button", { name: "编辑定时「工作日早报」" }).click();
  const edit = page.getByRole("dialog", { name: "编辑定时「工作日早报」" });
  await expect(edit.getByLabel("什么时候跑")).toHaveValue("weekdays");
  await expect(edit.getByLabel("你想完成什么？")).toHaveValue("汇总昨天的工单");
  await edit.getByLabel("什么时候跑").selectOption("monthly");
  await edit.getByLabel("几号").selectOption("1");
  await edit.getByRole("button", { name: "保存修改" }).click();
  await expect(edit).toBeHidden();
  await expect(row).toContainText("每月 1 日 09:30");

  // The box follows the saved schedule, so it changes once the change is saved.
  await row.getByLabel("启用定时「工作日早报」").click();
  await expect(row.getByLabel("启用定时「工作日早报」")).not.toBeChecked();
  await expect(section.getByRole("status")).toHaveText(
    "已停用定时「工作日早报」，到点不再运行。",
  );
  await expect(row).toContainText("已停用");

  // Run now works while it is off; the run says which schedule started it.
  await row.getByRole("button", { name: "立即跑一次「工作日早报」" }).click();
  await expect(section.getByRole("status")).toContainText(
    "已按「工作日早报」发起运行",
  );
  const last = row.getByRole("link");
  await expect(last).toBeVisible();
  await last.click();
  await expect(page).toHaveURL(/\/runs\//);
  await expect(page.locator(".detail-meta")).toContainText("定时：工作日早报");

  await page.goto("/apps/hello#access");
  const again = page
    .getByRole("region", { name: "定时运行" })
    .getByRole("row", { name: /工作日早报/ });
  page.once("dialog", (dialog) => dialog.accept());
  await again.getByRole("button", { name: "删除定时「工作日早报」" }).click();
  await expect(
    page.getByRole("region", { name: "定时运行" }).getByRole("status"),
  ).toHaveText("已删除定时「工作日早报」。");
  await expect(again).toBeHidden();
});
