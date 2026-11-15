import { test, expect, type Page } from "@playwright/test";
import type { FlowConfig } from "../lib/apps";
import type { Model } from "../lib/models";

// docs/api/vision.md: models that can see images are marked on the models page and in
// node dropdowns, and the 调用模型 node lists the images it sends.
test.describe.configure({ timeout: 60_000 });
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
});

const unique = (prefix: string) =>
  `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;
const chatRow = (page: Page, alias: string) =>
  page
    .getByRole("table", { name: "对话模型", exact: true })
    .getByRole("row")
    .filter({ has: page.getByText(alias, { exact: true }) });

test("models that can see images are marked, and a chat model can be marked on the page", async ({
  page,
  request,
}) => {
  await page.goto("/models");
  await expect(chatRow(page, "qwen")).toContainText("能看图");
  await expect(chatRow(page, "mock")).not.toContainText("能看图");

  const alias = unique("vl");
  await page.getByRole("button", { name: "添加模型", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "添加模型" });
  await dialog.getByLabel("供应商").selectOption("openai_compatible");
  await dialog.getByLabel("型号").fill("qwen-vl-max");
  await dialog.getByLabel("别名").fill(alias);
  await dialog.getByLabel("显示名").fill("通义看图");
  await dialog.getByLabel("地址").fill("https://aigw.example.com/v1");
  await dialog.getByLabel("key", { exact: true }).fill("sk-good");
  await dialog.getByLabel("能看图").check();
  const creating = page.waitForRequest(
    (r) => r.url().endsWith("/api/models") && r.method() === "POST",
  );
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  expect((await creating).postDataJSON()).toMatchObject({ vision: true });
  await expect(dialog).toBeHidden();
  const added = chatRow(page, alias);
  await expect(added).toContainText("能看图");

  // Unmarked again without a test call.
  await added.getByRole("button", { name: "修改「通义看图」" }).click();
  const edit = page.getByRole("dialog", { name: "修改「通义看图」" });
  await edit.getByLabel("能看图").uncheck();
  await expect(edit.getByText(/保存前先调一次模型试试/)).toHaveCount(0);
  await edit.getByRole("button", { name: "保存", exact: true }).click();
  await expect(edit).toBeHidden();
  await expect(added).not.toContainText("能看图");
  const saved = (await (
    await request.get(`/api/models/${alias}`)
  ).json()) as Model;
  expect(saved.vision).toBe(false);
});

test("the 调用模型 node lists the images it sends, and warns about a model that cannot see them", async ({
  page,
  request,
}) => {
  const app = unique("look");
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title: "看画面", from: { blank: true } },
      })
    ).status(),
  ).toBe(201);
  await page.goto(`/apps/${app}`);
  await page.getByRole("button", { name: /回答/ }).first().click();
  const form = page.getByRole("region", { name: /^节点设置：/ });
  const model = form.getByLabel("模型", { exact: true });
  await expect(
    model.getByRole("option", { name: "通义千问（qwen）（能看图）" }),
  ).toHaveCount(1);

  await form.getByRole("button", { name: "添加图片", exact: true }).click();
  await form
    .getByRole("textbox", { name: "第 1 张图片" })
    .fill("{{start.question}}");
  // The platform default (mock) is not marked as able to see images.
  const warning = form.getByText(/这个模型可能看不了图/);
  await expect(warning).toBeVisible();
  await model.selectOption("qwen");
  await expect(warning).toHaveCount(0);

  const saving = page.waitForRequest(
    (r) => r.url().endsWith(`/api/apps/${app}/draft`) && r.method() === "PUT",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  const sent = (await saving).postDataJSON() as { config: FlowConfig };
  expect(
    sent.config.graph.nodes.find((node) => node.id === "answer"),
  ).toMatchObject({ model: "qwen", images: ["{{start.question}}"] });

  // Removing the last image takes the setting away.
  await form.getByRole("button", { name: "删除图片 1" }).click();
  await expect(form.getByRole("textbox", { name: "第 1 张图片" })).toHaveCount(
    0,
  );
});

test("a draft with a bad images setting shows where", async ({ request }) => {
  const app = unique("look");
  await request.post("/api/apps", {
    data: { app, title: "看画面", from: { blank: true } },
  });
  const detail = (await (await request.get(`/api/apps/${app}`)).json()) as {
    draft: { config: FlowConfig } | null;
    current: { config: FlowConfig };
  };
  const config = structuredClone((detail.draft ?? detail.current).config);
  const answer = config.graph.nodes.find((node) => node.id === "answer");
  Object.assign(answer!, { images: ["{{start.question}}", " "] });
  const put = await request.put(`/api/apps/${app}/draft`, {
    data: { config },
  });
  expect(put.status()).toBe(422);
  expect((await put.json()).detail).toContainEqual(
    expect.objectContaining({
      loc: ["body", "config", "graph", "nodes", "answer", "images", 1],
      msg: "图片要写最多 10 项，每项一个引用或媒体路径",
    }),
  );
});
