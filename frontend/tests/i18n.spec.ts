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
  await expect(
    page.getByRole("heading", { name: "Notifications" }),
  ).toBeVisible();
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
  await expect(
    page.getByRole("link", { name: "New run" }).first(),
  ).toBeVisible();
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

test("resource lists read in English; one Chinese word can have two translations", async ({
  page,
  context,
}) => {
  await context.addCookies([
    { name: "aep_locale", value: "en", url: "http://127.0.0.1:3100" },
  ]);
  await page.goto("/catalog");
  await expect(
    page.getByRole("heading", { name: "Asset catalog" }),
  ).toBeVisible();
  const kinds = page.getByRole("group", { name: "Filter by category" });
  // 应用 is "Apps" in the filter and "App" as a row's category.
  await expect(kinds.getByRole("button", { name: /^Apps \d+$/ })).toBeVisible();
  await expect(
    page.getByRole("columnheader", { name: "Category" }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "App", exact: true }).first(),
  ).toBeVisible();

  await page.goto("/models");
  await expect(
    page.getByRole("heading", { name: "Models", level: 1 }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Add model" })).toBeVisible();
  await expect(page.getByText("key: in the config file").first()).toBeVisible();

  await page.goto("/runs/new");
  await expect(page.getByRole("heading", { name: "New run" })).toBeVisible();
  await expect(page.getByLabel("App", { exact: true })).toBeVisible();
});

test("app pages read in English, including the canvas and node settings", async ({
  page,
  context,
}) => {
  await context.addCookies([
    { name: "aep_locale", value: "en", url: "http://127.0.0.1:3100" },
  ]);
  await page.goto("/apps");
  const hello = page.getByRole("article", { name: "问答示例" });
  await expect(
    hello.getByRole("link", { name: "Use “问答示例”" }),
  ).toBeVisible();
  await hello.getByRole("link", { name: "Design “问答示例”" }).click();
  await page.getByRole("tab", { name: "Flow" }).click();
  const canvas = page.getByRole("group", { name: "Flow canvas", exact: true });
  await expect(canvas.getByText("Call model", { exact: true })).toBeVisible();
  await canvas.locator('.react-flow__node[data-id="draft"]').click();
  await expect(page.getByLabel("Model", { exact: true })).toBeVisible();
  await expect(page.getByText("0 / 500 chars")).toBeVisible();
  await expect(page.getByText("0 constants, 0 secrets")).toBeVisible();
});
