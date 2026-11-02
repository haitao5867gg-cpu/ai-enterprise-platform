import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type { Model } from "../lib/models";

// The mock keeps its models for the whole dev server, so every added model gets a unique
// alias. Keys starting with "sk-bad" fail the mock's test call (lib/server/mock-models.ts).
test.describe.configure({ timeout: 60_000 });

const unique = (prefix: string) =>
  `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;
const row = (page: Page, table: string, alias: string) =>
  page
    .getByRole("table", { name: table, exact: true })
    .getByRole("row")
    .filter({ has: page.getByText(alias, { exact: true }) });
async function addClaude(
  request: APIRequestContext,
  alias: string,
): Promise<Model> {
  const response = await request.post("/api/models", {
    data: {
      alias,
      display_name: "Claude 测试",
      purpose: "chat",
      provider: "anthropic",
      model: "claude-sonnet-5-5",
      key: { source: "page", value: "sk-ant-test" },
    },
  });
  expect(response.status()).toBe(201);
  return response.json();
}

test("列表按用途分组，显示默认模型、来源和近 30 天调用", async ({ page }) => {
  await page.goto("/models");
  await expect(
    page.getByRole("heading", { name: "模型", exact: true, level: 1 }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "模型", exact: true }),
  ).toHaveClass(/active/);
  const mock = row(page, "对话模型", "mock");
  await expect(mock).toContainText("Mock（测试）");
  await expect(mock).toContainText("默认");
  await expect(mock).toContainText("配置文件");
  await expect(mock).toContainText("42 次");
  await expect(mock.getByRole("button", { name: /^删除/ })).toHaveCount(0);
  await expect(row(page, "向量模型", "mock-embed")).toContainText("默认");
  await expect(page.getByLabel("默认对话模型")).toHaveValue("mock");
  await expect(page.getByLabel("默认向量模型")).toHaveValue("mock-embed");

  await mock.getByRole("button", { name: "测试「Mock（测试）」" }).click();
  await expect(mock).toContainText("正常 · 820 毫秒");
  await mock.getByRole("button", { name: /^哪些应用在用/ }).click();
  await expect(page.getByText("零售数据 ETL", { exact: true })).toBeVisible();
  await expect(page.getByText(/用的是平台默认/)).toBeVisible();
});

test("添加 Claude：填 key 后测试通过才保存，key 不再显示", async ({ page }) => {
  const alias = unique("claude");
  await page.goto("/models");
  await page.getByRole("button", { name: "添加模型", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "添加模型" });
  const save = dialog.getByRole("button", { name: "保存", exact: true });
  await expect(save).toBeDisabled();
  await dialog.getByLabel("供应商").selectOption("anthropic");
  await expect(
    dialog.getByText(/Claude\.ai 的 Pro \/ Max 订阅额度不能在这里用/),
  ).toBeVisible();
  await dialog.getByLabel("型号").fill("claude-sonnet-5-5");
  await expect(dialog.getByLabel("别名")).toHaveValue("claude-sonnet-5-5");
  await dialog.getByLabel("别名").fill(alias);
  await dialog.getByLabel("显示名").fill("Claude Sonnet");

  // A bad key fails the test: nothing is saved and the dialog stays open.
  await dialog.getByLabel("key", { exact: true }).fill("sk-bad-123");
  await save.click();
  await expect(dialog.getByRole("alert")).toContainText(
    "测试没有通过：认证失败：key 不对或已失效。",
  );
  await expect(
    page.getByRole("table", { name: "对话模型" }).getByText(alias),
  ).toHaveCount(0);

  await dialog.getByLabel("key", { exact: true }).fill("sk-ant-good");
  await save.click();
  await expect(dialog).toBeHidden();
  await expect(
    page.getByText("已添加「Claude Sonnet」，测试通过"),
  ).toBeVisible();
  const added = row(page, "对话模型", alias);
  await expect(added).toContainText("Anthropic（Claude）");
  await expect(added).toContainText("页面上填的");
  await expect(added).not.toContainText("sk-ant-good");
});

test("环境变量名不合格时不能保存", async ({ page }) => {
  await page.goto("/models");
  await page.getByRole("button", { name: "添加模型", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "添加模型" });
  await dialog.getByLabel("供应商").selectOption("openai_compatible");
  await dialog.getByLabel("型号").fill("deepseek-v4");
  await dialog.getByLabel("显示名").fill("公司网关");
  await dialog.getByLabel("地址").fill("https://aigw.example.com/v1");
  await dialog.getByLabel("用服务器上的环境变量").check();
  await dialog.getByLabel("环境变量名").fill("COMPANY_KEY");
  await expect(
    dialog.getByRole("button", { name: "保存", exact: true }),
  ).toBeDisabled();
  await expect(
    dialog.getByText(/环境变量名要以 _API_KEY 结尾/).first(),
  ).toBeVisible();
  await dialog.getByLabel("环境变量名").fill("COMPANY_API_KEY");
  await expect(
    dialog.getByRole("button", { name: "保存", exact: true }),
  ).toBeEnabled();
});

test("修改显示名和删除页面上加的模型；默认模型要先换掉才能删", async ({
  page,
  request,
}) => {
  const alias = unique("claude");
  await addClaude(request, alias);
  await page.goto("/models");
  const added = row(page, "对话模型", alias);
  await added.getByRole("button", { name: "修改「Claude 测试」" }).click();
  const dialog = page.getByRole("dialog", { name: "修改「Claude 测试」" });
  await expect(dialog.getByLabel("不改（用原来的 key）")).toBeChecked();
  await dialog.getByLabel("显示名").fill("Claude 改名");
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(added).toContainText("Claude 改名");

  // Made the default, it cannot be deleted.
  await page.getByLabel("默认对话模型").selectOption(alias);
  await expect(page.getByText("已保存。")).toBeVisible();
  await expect(added).toContainText("默认");
  page.once("dialog", (confirm) => void confirm.accept());
  await added.getByRole("button", { name: "删除「Claude 改名」" }).click();
  await expect(page.locator(".error-notice")).toContainText(
    `「${alias}」是平台默认的对话模型，请先换一个默认模型`,
  );

  await page.getByLabel("默认对话模型").selectOption("mock");
  await expect(page.getByText("已保存。")).toBeVisible();
  page.once("dialog", (confirm) => void confirm.accept());
  await added.getByRole("button", { name: "删除「Claude 改名」" }).click();
  await expect(page.getByText("已删除「Claude 改名」。")).toBeVisible();
  await expect(added).toHaveCount(0);
});

test("节点的模型下拉框显示模型的显示名", async ({ page }) => {
  await page.goto("/apps/etl");
  await page
    .getByRole("group", { name: "流程图", exact: true })
    .getByRole("button", { name: /^出方案/ })
    .click();
  const select = page
    .getByRole("region", { name: "节点 出方案", exact: true })
    .getByLabel("模型", { exact: true });
  await expect(
    select.getByRole("option", { name: "DeepSeek（deepseek）", exact: true }),
  ).toHaveCount(1);
  // Embedding models are not offered to nodes.
  await expect(select.getByRole("option", { name: /mock-embed/ })).toHaveCount(
    0,
  );
  await select.selectOption("deepseek");
  await expect(select).toHaveValue("deepseek");
});
