import { test, expect, type BrowserContext, type Page } from "@playwright/test";

// The mock turns sign-in on with the cookie aep_mock_auth (lib/server/mock-auth.ts);
// its accounts all have the password "correct-horse".
test.describe.configure({ timeout: 60_000 });

const PASSWORD = "correct-horse";
async function signInMode(context: BrowserContext, value = "on") {
  await context.addCookies([
    { name: "aep_mock_auth", value, url: "http://127.0.0.1:3100" },
  ]);
}
async function signIn(page: Page, username: string, next = "/") {
  await page.goto(`/login?next=${encodeURIComponent(next)}`);
  await page.getByLabel("登录名").fill(username);
  await page.getByLabel("密码", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(next === "/" ? /\/$/ : new RegExp(next));
}
const nav = (page: Page) =>
  page.getByRole("navigation", { name: "主导航" }).getByRole("link");

test("登录关着时和以前一样：不用登录，没有账号页", async ({ page }) => {
  await page.goto("/");
  await expect(nav(page)).toHaveText([
    "运行记录",
    "应用",
    "Skill",
    "知识库",
    "数据源与工具",
    "模型",
    "项目",
    "新建运行",
  ]);
  await expect(page.getByRole("button", { name: "退出" })).toHaveCount(0);
  await page.goto("/users");
  await expect(
    page.getByRole("heading", { name: "没有打开登录" }),
  ).toBeVisible();
  await page.goto("/runs/new");
  await expect(page.getByLabel("你的名字")).toBeVisible();
});

test("没登录跳到登录页，登录后回到原来的页面；使用者看不到搭建页面", async ({
  page,
  context,
}) => {
  await signInMode(context);
  await page.goto("/runs/new");
  await expect(page).toHaveURL(/\/login\?next=%2Fruns%2Fnew$/);
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  await page.getByLabel("登录名").fill("lilei");
  await page.getByLabel("密码", { exact: true }).fill("wrong-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.locator(".error-notice")).toContainText("账号或密码不对");

  await page.getByLabel("密码", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/runs\/new$/);
  await expect(nav(page)).toHaveText(["运行记录", "新建运行"]);
  await expect(page.locator(".account")).toContainText("李雷");
  await expect(page.locator(".account")).toContainText("使用者");
  // The run is started under the signed-in name.
  await expect(page.getByLabel("你的名字")).toHaveCount(0);
  await expect(page.getByText("以「李雷」的身份发起")).toBeVisible();

  await page.goto("/apps");
  await expect(page.getByRole("heading", { name: "没有权限" })).toBeVisible();
  await expect(page.getByText("这个页面需要搭建者权限")).toBeVisible();

  await page.getByRole("button", { name: "退出" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login\?next=%2F$/);
});

test("待确认的一步只有审批者能处理，记录写登录人", async ({
  page,
  context,
}) => {
  await signInMode(context);
  await signIn(page, "lilei", "/runs/new");
  await page
    .getByLabel("你想完成什么？")
    .fill("整理客户数据清洗方案，执行前确认。");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(
    page.getByRole("heading", { name: "需要你的确认" }),
  ).toBeVisible();
  const url = page.url();
  await expect(page.getByText("这一步要审批者确认")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "确认", exact: true }),
  ).toBeDisabled();

  await page.getByRole("button", { name: "退出" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await signIn(page, "hanmeimei", new URL(url).pathname);
  await expect(page.getByText("以「韩梅梅」的身份处理")).toBeVisible();
  await expect(page.locator("#approval-actor")).toHaveCount(0);
  await expect(page.getByText("这一步要审批者确认")).toHaveCount(0);
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
});

test("管理员建账号、分角色、停用、重设密码，并在操作记录里看到", async ({
  page,
  context,
}) => {
  await signInMode(context);
  await signIn(page, "admin");
  await expect(nav(page)).toContainText(["账号", "操作记录"]);
  await nav(page).filter({ hasText: "账号" }).click();
  await expect(
    page.getByRole("heading", { name: "账号", level: 1 }),
  ).toBeVisible();
  const username = `u${Date.now().toString(36)}`;
  await page.getByRole("button", { name: "新建账号" }).click();
  const dialog = page.getByRole("dialog", { name: "新建账号" });
  await dialog.getByLabel("登录名").fill(username);
  await dialog.getByLabel("显示名").fill("赵审批");
  await dialog.getByLabel(/^审批者/).check();
  await dialog.getByLabel("初始密码").fill("short");
  await expect(dialog.getByRole("button", { name: "保存" })).toBeDisabled();
  await dialog.getByLabel("初始密码").fill("initial-pass");
  await dialog.getByRole("button", { name: "保存" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText("已建好「赵审批」的账号。")).toBeVisible();
  const row = page.getByRole("row").filter({ hasText: username });
  await expect(row).toContainText("使用者");
  await expect(row).toContainText("审批者");

  await row.getByRole("button", { name: "修改「赵审批」" }).click();
  const edit = page.getByRole("dialog", { name: "修改「赵审批」" });
  await edit.getByLabel(/^搭建者/).check();
  await edit.getByRole("button", { name: "保存" }).click();
  await expect(row).toContainText("搭建者");

  await row.getByRole("button", { name: "停用「赵审批」" }).click();
  await expect(row).toContainText("已停用");
  await row.getByRole("button", { name: "重设「赵审批」的密码" }).click();
  const reset = page.getByRole("dialog", { name: "重设「赵审批」的密码" });
  await reset.getByLabel("新密码").fill("another-pass");
  await reset.getByRole("button", { name: "重设密码" }).click();
  await expect(page.getByText("已重设「赵审批」的密码。")).toBeVisible();

  // The admin's own row cannot be disabled or deleted.
  const own = page.getByRole("row").filter({ hasText: "王管理" });
  await expect(own.getByRole("button", { name: /^停用/ })).toHaveCount(0);
  await expect(own.getByRole("button", { name: /^删除/ })).toHaveCount(0);

  await nav(page).filter({ hasText: "操作记录" }).click();
  const table = page.getByRole("table", { name: "操作记录" });
  await expect(table).toContainText("新建账号");
  await expect(table).toContainText("重设密码");
  await expect(table).toContainText("王管理");
});

test("还没有账号时先建第一个管理员", async ({ page, context }) => {
  await signInMode(context, "setup");
  await page.goto("/");
  await expect(page).toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "建第一个管理员" }),
  ).toBeVisible();
  const username = `boss${Date.now().toString(36)}`;
  await page.getByLabel("登录名").fill(username);
  await page.getByLabel("显示名").fill("老板");
  await page.getByLabel("密码", { exact: true }).fill(PASSWORD);
  await page.getByLabel("再填一次密码").fill("different-pass");
  const create = page.getByRole("button", { name: "建管理员并登录" });
  await expect(create).toBeDisabled();
  await expect(page.getByText("两次填的密码不一样。")).toBeVisible();
  await page.getByLabel("再填一次密码").fill(PASSWORD);
  await create.click();
  await expect(page).toHaveURL(/\/$/);
  await signInMode(context, "on");
  await page.reload();
  await expect(page.locator(".account")).toContainText("老板");
  await expect(page.locator(".account")).toContainText("管理员");
});

test("搭建者的页面会把原来的名字栏换成登录人", async ({ page, context }) => {
  await signInMode(context);
  await signIn(page, "builder", "/skills/etl-plan");
  await expect(nav(page)).not.toContainText(["账号"]);
  await expect(page.getByText("以「王搭建」的身份保存")).toBeVisible();
  await expect(page.getByLabel("你的名字")).toHaveCount(0);
});
