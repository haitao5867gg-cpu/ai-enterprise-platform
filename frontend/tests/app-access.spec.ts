import { test, expect } from "@playwright/test";

// Keys are kept by the mock (lib/server/mock-apps.ts); see docs/api/app-access.md.
test("应用页给出 API 和 MCP 用法，新建的密钥只显示一次", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/apps/hello#access");
  await expect(page.getByRole("tab", { name: "接入" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const section = page.getByRole("region", { name: "API 和 MCP" });
  await expect(section).toBeVisible();
  await expect(section).toContainText("/api/v1/apps/hello/runs");
  await expect(section).toContainText("/api/mcp/apps/hello");

  await section.getByText("用 curl 跑一次").click();
  await expect(section.locator("pre").first()).toContainText(
    "Authorization: Bearer $AEP_KEY",
  );
  await section.getByText("MCP 客户端配置").click();
  await expect(section.locator("pre").nth(1)).toContainText('"mcpServers"');

  await section.getByRole("button", { name: "新建密钥" }).click();
  const create = page.getByRole("dialog", { name: "新建 API 密钥" });
  await create.getByLabel("名称").fill("CRM 系统");
  await create.getByRole("button", { name: "新建密钥" }).click();

  const shown = page.getByRole("dialog", { name: "密钥已建好" });
  await expect(shown).toContainText("只显示这一次");
  const secret = (await shown.getByLabel("完整密钥").textContent()) ?? "";
  expect(secret).toMatch(/^aep_.{43}$/);
  await shown.getByRole("button", { name: "复制密钥" }).click();
  await expect(shown.getByRole("button", { name: "复制密钥" })).toHaveText(
    "已复制",
  );
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    secret,
  );
  await shown.getByRole("button", { name: "我已保存好" }).click();
  await expect(shown).toBeHidden();

  const table = section.getByRole("table", { name: "API 密钥" });
  const row = table.getByRole("row", { name: /CRM 系统/ });
  await expect(row).toContainText(`${secret.slice(0, 12)}…`);
  await expect(row).toContainText("还没用过");
  await expect(table).not.toContainText(secret);

  page.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: "删除密钥「CRM 系统」" }).click();
  await expect(section.getByRole("status")).toHaveText(
    "已删除密钥「CRM 系统」。",
  );
  await expect(row).toBeHidden();
});

test("应用页分成概览、编排、评测与发布、接入四个标签", async ({ page }) => {
  await page.goto("/apps/hello");
  const tabs = page.getByRole("tablist", { name: "应用页" });
  await expect(tabs.getByRole("tab")).toHaveText([
    "概览",
    "编排",
    "评测与发布",
    "接入",
  ]);
  // The editor first; the banner (draft or current) goes with editing and release.
  await expect(tabs.getByRole("tab", { name: "编排" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "版本历史" })).toBeHidden();

  await tabs.getByRole("tab", { name: "评测与发布" }).click();
  await expect(page).toHaveURL(/#release$/);
  await expect(page.getByRole("region", { name: "版本历史" })).toBeVisible();
  await expect(page.getByRole("region", { name: "发布应用" })).toBeVisible();
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toBeVisible();

  // Arrow keys move between tabs.
  await tabs.getByRole("tab", { name: "评测与发布" }).press("ArrowLeft");
  await expect(tabs.getByRole("tab", { name: "编排" })).toBeFocused();
  await tabs.getByRole("tab", { name: "编排" }).press("ArrowLeft");
  await expect(page.getByRole("group", { name: "应用说明书" })).toBeVisible();
  await expect(
    page.getByRole("region", { name: "正在查看的配置" }),
  ).toBeHidden();

  // A link straight to a tab opens it.
  await page.goto("/apps/hello#release");
  await expect(page.getByRole("region", { name: "版本历史" })).toBeVisible();
});

test("使用页只跑这一个应用，说明在哪里会问人，列出最近的运行", async ({
  page,
}) => {
  await page.goto("/apps/hello");
  await page.getByRole("link", { name: "使用页" }).click();
  await expect(page).toHaveURL(/\/use\/hello$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("问答示例");
  await expect(page.getByLabel("应用", { exact: true })).toBeHidden();
  await expect(
    page.getByRole("button", { name: "复制使用页链接" }),
  ).toBeVisible();
  const asks = page.getByRole("region", { name: "会在哪里问你" });
  await expect(asks.getByRole("listitem").first()).toBeVisible();

  await page.getByLabel("你想完成什么？").fill("使用页发起的一次运行");
  await page.getByRole("button", { name: "开始运行" }).click();
  await expect(page).toHaveURL(/\/runs\/[^/]+$/);
  await page.goBack();
  const recent = page.getByRole("list", { name: "这个应用最近的运行" });
  await expect(
    recent.getByRole("link", { name: "使用页发起的一次运行" }),
  ).toBeVisible();

  await page.goto("/use/nope");
  await expect(page.getByText("没有找到应用 nope。")).toBeVisible();
});
