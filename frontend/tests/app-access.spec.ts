import { test, expect } from "@playwright/test";

// Keys are kept by the mock (lib/server/mock-apps.ts); see docs/api/app-access.md.
test("应用页给出 API 和 MCP 用法，新建的密钥只显示一次", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/apps/hello");
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
