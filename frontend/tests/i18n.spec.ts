import { test, expect } from "@playwright/test";

// The interface language switch (lib/i18n): Chinese by default, English on request,
// remembered in the aep_locale cookie so the server renders the same language.
test("switches the interface between Chinese and English", async ({
  page,
  context,
}) => {
  await page.goto("/notifications");
  const nav = page.getByRole("navigation", { name: "主导航" });
  await expect(nav.getByRole("link", { name: "运行记录" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.getByRole("button", { name: "中文" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  await page.getByRole("button", { name: "English" }).click();
  const enNav = page.getByRole("navigation", { name: "Main navigation" });
  await expect(enNav.getByRole("link", { name: "Runs" })).toBeVisible();
  await expect(
    enNav.getByRole("link", { name: "Data sources & tools" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Notifications" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page).toHaveTitle("Notifications · AEP");
  expect(
    (await context.cookies()).find((cookie) => cookie.name === "aep_locale")
      ?.value,
  ).toBe("en");

  // The server renders English from the cookie: no Chinese flash after a reload.
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.getByRole("heading", { name: "Runs" })).toBeVisible();
  await expect(page.getByRole("link", { name: "New run" }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "English" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  await page.getByRole("button", { name: "中文" }).click();
  await expect(page.getByRole("heading", { name: "运行记录" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
});

test("the sign-in page has the language switch", async ({ page, context }) => {
  await context.addCookies([
    { name: "aep_mock_auth", value: "on", url: "http://127.0.0.1:3100" },
    { name: "aep_locale", value: "en", url: "http://127.0.0.1:3100" },
  ]);
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByLabel("Username")).toBeVisible();
  await page.getByRole("button", { name: "中文" }).click();
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
});

test("an unknown language in the cookie falls back to Chinese", async ({
  page,
  context,
}) => {
  await context.addCookies([
    { name: "aep_locale", value: "fr", url: "http://127.0.0.1:3100" },
  ]);
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.getByRole("heading", { name: "运行记录" })).toBeVisible();
});
