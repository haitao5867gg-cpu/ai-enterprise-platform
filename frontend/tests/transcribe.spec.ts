import { test, expect, type Page } from "@playwright/test";
import type { AppDetail, FlowConfig } from "../lib/apps";

// docs/api/transcription.md: transcription models on the models page, the 语音转写 node's
// settings, and which media a project lets go to models.
test.describe.configure({ timeout: 60_000 });
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
});

const unique = (prefix: string) =>
  `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;
const modelRow = (page: Page, alias: string) =>
  page
    .getByRole("table", { name: "转写模型", exact: true })
    .getByRole("row")
    .filter({ has: page.getByText(alias, { exact: true }) });

test("transcription models are listed apart, counted in audio minutes and can be tested", async ({
  page,
}) => {
  await page.goto("/models");
  const row = modelRow(page, "transcribe");
  await expect(row).toContainText("语音转写（faster-whisper）");
  await expect(row).toContainText("3 次");
  await expect(row).toContainText("6.3 音频分钟");
  await row
    .getByRole("button", { name: "测试「语音转写（faster-whisper）」" })
    .click();
  await expect(row).toContainText("正常 · 820 毫秒");
});

test("the 语音转写 node picks a transcription model, the file, the language and a prompt", async ({
  page,
  request,
}) => {
  const app = unique("asr");
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title: "配音转字幕", from: { blank: true } },
      })
    ).status(),
  ).toBe(201);
  const detail = (await (
    await request.get(`/api/apps/${app}`)
  ).json()) as AppDetail;
  const config = structuredClone(
    (detail.draft ?? detail.current).config,
  ) as FlowConfig;
  config.graph.nodes.push({
    id: "asr",
    type: "transcribe",
    title: "转写配音",
    model: "transcribe",
    audio: "{{start.question}}",
    language: "zh",
    prompt: "",
  });
  config.graph.edges = [
    { from: "start", to: "asr" },
    { from: "asr", to: "answer" },
    { from: "answer", to: "end" },
  ];
  const put = await request.put(`/api/apps/${app}/draft`, {
    data: { config },
  });
  expect(put.status(), await put.text()).toBe(200);

  await page.goto(`/apps/${app}`);
  await page
    .getByRole("button", { name: /转写配音/ })
    .first()
    .click();
  const form = page.getByRole("region", { name: /^节点设置：/ });
  const model = form.getByLabel("转写模型");
  await expect(model).toHaveValue("transcribe");
  // Only transcription models are offered, by their display names.
  await expect(model.locator("option")).toHaveText([
    "语音转写（faster-whisper）（transcribe）",
  ]);
  await expect(form.getByLabel("语言")).toHaveValue("zh");
  await form.getByLabel("语言").selectOption("");
  await form
    .getByRole("textbox", { name: "提示（选填）" })
    .fill("以下是普通话的健身教学。卧推、硬拉、深蹲。");
  const saving = page.waitForRequest(
    (r) => r.url().endsWith(`/api/apps/${app}/draft`) && r.method() === "PUT",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  const sent = (await saving).postDataJSON() as { config: FlowConfig };
  expect(
    sent.config.graph.nodes.find((node) => node.id === "asr"),
  ).toMatchObject({
    model: "transcribe",
    audio: "{{start.question}}",
    language: null,
    prompt: "以下是普通话的健身教学。卧推、硬拉、深蹲。",
  });

  // Later nodes can refer to what it puts out.
  await page.getByRole("button", { name: /回答/ }).first().click();
  await form.getByRole("button", { name: "插入变量：提示词" }).click();
  await expect(
    form.getByRole("group", { name: "可插入「提示词」的变量" }),
  ).toContainText("{{asr.segments}}");
});

test("a project chooses which media its runs may send to models", async ({
  page,
  request,
}) => {
  const {
    created_at: _made,
    updated_at: _changed,
    ...seed
  } = await (await request.get("/api/projects/sales-forecast")).json();
  void [_made, _changed];
  const id = unique("media");
  const made = await request.post("/api/projects", {
    data: { ...seed, project_id: id, name: `媒体 ${id}` },
  });
  expect(made.status(), await made.text()).toBe(201);
  const bad = await request.post("/api/projects", {
    data: {
      ...seed,
      project_id: unique("bad"),
      name: unique("坏"),
      policy: { ...seed.policy, media: ["video"] },
    },
  });
  expect(bad.status()).toBe(422);

  await page.goto(`/projects/${id}`);
  const policy = page.getByRole("region", { name: "数据外发策略" });
  await expect(policy).toContainText("平台默认（音频、图片）");
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  const edit = page.getByRole("form", { name: "编辑项目" });
  await edit.getByLabel("可以发给模型的媒体").selectOption("own");
  const kinds = edit.getByRole("group", { name: "允许发的媒体" });
  await expect(kinds.getByLabel("音频")).toBeChecked();
  await kinds.getByLabel("图片").uncheck();
  await edit.getByRole("button", { name: "保存" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "已保存" }),
  ).toBeVisible();
  expect(
    (await (await request.get(`/api/projects/${id}`)).json()).policy.media,
  ).toEqual(["audio"]);
  await expect(policy).toContainText("可以发给模型的媒体音频");
});
