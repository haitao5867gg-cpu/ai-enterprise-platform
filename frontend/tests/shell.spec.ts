import { test, expect } from "@playwright/test";

// The shell (界面改版第 2 批): grouped navigation that folds, a sidebar that folds to
// icons, the new-run button and the account menu in the top bar.
test("navigation groups fold, the page's own group stays open, and both are remembered", async ({
  page,
}) => {
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "主导航" });
  const build = nav.getByRole("button", { name: "搭建" });
  await expect(build).toHaveAttribute("aria-expanded", "true");
  await expect(nav.getByRole("link", { name: "知识库" })).toBeVisible();

  await build.click();
  await expect(build).toHaveAttribute("aria-expanded", "false");
  await expect(nav.getByRole("link", { name: "知识库" })).toBeHidden();
  await page.reload();
  await expect(nav.getByRole("link", { name: "知识库" })).toBeHidden();

  // On a page of a folded group the group shows anyway.
  await page.goto("/skills");
  await expect(nav.getByRole("link", { name: "Skill" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(nav.getByRole("link", { name: "知识库" })).toBeVisible();

  await page.goto("/");
  await nav.getByRole("button", { name: "搭建" }).click();
  await expect(nav.getByRole("link", { name: "知识库" })).toBeVisible();
});

test("the sidebar folds to icons and keeps the page names for screen readers", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "收起侧栏" }).click();
  const nav = page.getByRole("navigation", { name: "主导航" });
  const models = nav.getByRole("link", { name: "模型" });
  await expect(models).toBeVisible();
  await expect(models).toHaveAttribute("title", "模型");
  expect((await page.locator(".sidebar").boundingBox())?.width).toBeLessThan(
    80,
  );
  await page.reload();
  await expect(page.getByRole("button", { name: "展开侧栏" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await page.getByRole("button", { name: "展开侧栏" }).click();
  expect((await page.locator(".sidebar").boundingBox())?.width).toBeGreaterThan(
    150,
  );
});

test("the top bar starts a new run, except on the new-run page", async ({
  page,
}) => {
  await page.goto("/models");
  const banner = page.getByRole("banner");
  await banner.getByRole("link", { name: "新建运行" }).click();
  await expect(page).toHaveURL(/\/runs\/new$/);
  await expect(banner.getByRole("link", { name: "新建运行" })).toHaveCount(0);
});

test("the account menu opens from the top bar and closes with Escape", async ({
  page,
  context,
}) => {
  await context.addCookies([
    { name: "aep_mock_auth", value: "on", url: "http://127.0.0.1:3100" },
  ]);
  await page.goto("/login");
  await page.getByLabel("登录名").fill("hanmeimei");
  await page.getByLabel("密码", { exact: true }).fill("correct-horse");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);

  const banner = page.getByRole("banner");
  const signOut = page.getByRole("button", { name: "退出" });
  await expect(signOut).toBeHidden();
  await banner.locator(".account summary").click();
  await expect(signOut).toBeVisible();
  await expect(page.locator(".account-menu")).toContainText("韩梅梅");
  await page.keyboard.press("Escape");
  await expect(signOut).toBeHidden();
  await banner.locator(".account summary").click();
  await page.getByRole("button", { name: "修改密码" }).click();
  await expect(page.getByRole("dialog", { name: "修改密码" })).toBeVisible();
});
