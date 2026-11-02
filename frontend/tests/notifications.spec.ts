import { test, expect, type BrowserContext, type Page } from "@playwright/test";

// The mock (lib/server/mock-notifications.ts) announces its runs; with the cookie
// aep_mock_auth=on approvals go to approvers, and every account's password is
// "correct-horse".
test.describe.configure({ timeout: 60_000 });

const bell = (page: Page) =>
  page.getByRole("banner").getByRole("link", { name: /^通知/ });

async function startWaitingRun(page: Page) {
  await page.goto("/runs/new");
  await page
    .getByLabel("你想完成什么？")
    .fill("整理客户数据清洗方案，执行前确认。");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  return new URL(page.url()).pathname;
}

async function readAll(page: Page) {
  await page.goto("/notifications");
  await expect(
    page
      .getByRole("list", { name: "通知" })
      .or(page.getByRole("heading", { name: "还没有通知" })),
  ).toBeVisible();
  const all = page.getByRole("button", { name: "全部标为已读" });
  if (await all.isEnabled()) await all.click();
  await expect(all).toBeDisabled();
  await expect(bell(page)).toHaveAccessibleName("通知");
}

async function signIn(context: BrowserContext, page: Page, username: string) {
  await context.addCookies([
    { name: "aep_mock_auth", value: "on", url: "http://127.0.0.1:3100" },
  ]);
  await page.goto("/login");
  await page.getByLabel("登录名").fill(username);
  await page.getByLabel("密码", { exact: true }).fill("correct-horse");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
}

test("运行等确认时铃铛有未读，点通知直达确认页", async ({ page }) => {
  await readAll(page);
  const runPath = await startWaitingRun(page);
  // The bell checks every 30 seconds and on every page; a reload is quicker.
  await page.reload();
  await expect(bell(page)).toHaveAccessibleName("通知，1 条未读");
  await bell(page).click();
  await expect(page).toHaveURL(/\/notifications$/);
  const list = page.getByRole("list", { name: "通知" });
  const item = list.getByRole("listitem").first();
  await expect(item).toContainText("等你确认");
  await expect(item).toContainText("整理客户数据清洗方案");
  await expect(item).toContainText("（未读）");

  await page.getByLabel("只看未读").check();
  await expect(list.getByRole("listitem")).toHaveCount(1);
  await item.getByRole("button").click();
  await expect(page).toHaveURL(new RegExp(`${runPath}$`));
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  await expect(bell(page)).toHaveAccessibleName("通知");

  // Approving finishes the run: a notice that it is done.
  await page.locator("#approval-actor").fill("韩梅梅");
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
  await page.goto("/notifications");
  await expect(list.getByRole("listitem").first()).toContainText("运行完成");
  await page.getByRole("button", { name: "全部标为已读" }).click();
  await expect(bell(page)).toHaveAccessibleName("通知");
});

test("登录打开时，确认通知只发给审批者", async ({ page, context }) => {
  await signIn(context, page, "hanmeimei");
  await readAll(page);
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page).toHaveURL(/\/login$/);

  await signIn(context, page, "lilei");
  await readAll(page);
  await startWaitingRun(page);
  await page.goto("/notifications");
  await expect(bell(page)).toHaveAccessibleName("通知");
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page).toHaveURL(/\/login$/);

  await signIn(context, page, "hanmeimei");
  await expect(bell(page)).toHaveAccessibleName("通知，1 条未读");
  // Accounts can have an email for email notices; the channels page is admins'.
  await page.goto("/notification-channels");
  await expect(page.getByRole("heading", { name: "没有权限" })).toBeVisible();
});

test("管理员配置 Webhook：新建、发测试消息、修改、停用、删除", async ({
  page,
}) => {
  await page.goto("/notification-channels");
  await expect(
    page.getByRole("heading", { name: "通知渠道", level: 1 }),
  ).toBeVisible();
  await expect(page.getByText("AEP_SMTP_HOST")).toBeVisible();

  await page.getByRole("button", { name: "新建 Webhook" }).click();
  const dialog = page.getByRole("dialog", { name: "新建 Webhook" });
  await dialog.getByLabel("名称").fill("数据组群");
  await dialog.getByLabel("发到哪里").selectOption("dingtalk");
  await expect(dialog.getByText(/关键词」并填 AEP/)).toBeVisible();
  await dialog.getByLabel("Webhook 地址").fill("ftp://bad");
  await expect(dialog.getByRole("button", { name: "保存" })).toBeDisabled();
  await dialog
    .getByLabel("Webhook 地址")
    .fill("https://oapi.dingtalk.com/robot/send?access_token=fail-9f2e");
  await dialog.getByLabel("完成").check();
  await dialog.getByRole("button", { name: "保存" }).click();
  await expect(dialog).toBeHidden();
  const row = page.getByRole("row").filter({ hasText: "数据组群" });
  await expect(row).toContainText("钉钉群机器人");
  await expect(row).toContainText("https://oapi.dingtalk.com/…9f2e");
  await expect(row).not.toContainText("access_token");
  await expect(row).toContainText("待确认、完成、失败");
  await expect(row).toContainText("所有应用");

  await row.getByRole("button", { name: "给「数据组群」发测试消息" }).click();
  await expect(
    page.getByText("测试消息没发成功：机器人返回错误 93000"),
  ).toBeVisible();
  await expect(row).toContainText("失败");

  await row.getByRole("button", { name: "修改「数据组群」" }).click();
  const edit = page.getByRole("dialog", { name: "修改「数据组群」" });
  await expect(edit.getByLabel("Webhook 地址")).toHaveAttribute(
    "placeholder",
    /9f2e，不改就留空/,
  );
  await edit
    .getByLabel("Webhook 地址")
    .fill("https://oapi.dingtalk.com/robot/send?access_token=good-1a2b");
  await edit.getByLabel("完成").uncheck();
  await edit.getByRole("button", { name: "保存" }).click();
  await expect(edit).toBeHidden();
  await expect(row).toContainText("待确认、失败");
  await row.getByRole("button", { name: "给「数据组群」发测试消息" }).click();
  await expect(
    page.getByText("测试消息已发到「数据组群」，请到群里看一下。"),
  ).toBeVisible();
  await expect(row).toContainText("成功");

  await row.getByRole("button", { name: "停用「数据组群」" }).click();
  await expect(row).toContainText("已停用");
  page.once("dialog", (d) => void d.accept());
  await row.getByRole("button", { name: "删除「数据组群」" }).click();
  await expect(page.getByText("已删除「数据组群」。")).toBeVisible();
  await expect(page.getByText("还没有 Webhook。")).toBeVisible();
});
