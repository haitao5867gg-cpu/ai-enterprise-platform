import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type { FlowConfig } from "../lib/apps";
import type { MediaItem, MediaLibrary, MediaPage } from "../lib/media";

// docs/api/media-search.md: a library that has AI describe its footage, and the 检索素材
// node that takes usable items out of a bound library.
test.describe.configure({ timeout: 90_000 });
test.use({ actionTimeout: 15_000 });
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
});

const unique = (prefix: string) =>
  `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });
const file = (name: string, mimeType: string) => ({
  name,
  mimeType,
  buffer: Buffer.from(`fake ${name}`),
});

async function newLibrary(request: APIRequestContext): Promise<MediaLibrary> {
  const made = await request.post("/api/media-libraries", {
    data: { name: unique("教练素材") },
  });
  expect(made.status(), await made.text()).toBe(201);
  return (await made.json()) as MediaLibrary;
}
async function items(
  request: APIRequestContext,
  library: string,
): Promise<MediaItem[]> {
  const page = (await (
    await request.get(`/api/media-libraries/${library}/items`)
  ).json()) as MediaPage;
  return page.items;
}

test("a library has AI describe its footage, and a person's words stay theirs", async ({
  page,
  request,
}) => {
  const library = await newLibrary(request);
  const id = library.library_id;
  await page.goto(`/media/${id}`);
  await expect(page.getByText("不自动写画面描述")).toBeVisible();

  // A model that cannot see images is refused.
  const refused = await request.patch(`/api/media-libraries/${id}`, {
    data: { describe: { model: "gpt" } },
  });
  expect(refused.status()).toBe(422);
  expect((await refused.json()).detail).toBe(
    "模型「gpt」没标能看图，换一个能看图的模型",
  );

  await page.getByRole("button", { name: "自动描述", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "自动描述" });
  await dialog.getByLabel("上传的视频和图片由 AI 看画面写描述").check();
  const model = dialog.getByLabel("用哪个模型");
  // Only chat models that can see images are offered.
  await expect(model.locator("option")).toHaveText(["通义千问"]);
  await model.selectOption("qwen");
  await dialog.getByLabel("给模型的说明（选填）").fill("教练自己拍的训练素材");
  const saving = page.waitForRequest(
    (r) =>
      r.url().endsWith(`/api/media-libraries/${id}`) && r.method() === "PATCH",
  );
  await dialog.getByRole("button", { name: "保存" }).click();
  expect((await saving).postDataJSON()).toEqual({
    describe: { model: "qwen", hint: "教练自己拍的训练素材" },
  });
  await expect(dialog).toBeHidden();
  await expect(page.getByText("AI 写画面描述：qwen")).toBeVisible();

  await page
    .getByLabel("上传素材")
    .setInputFiles([
      file("卧推侧面.mp4", "video/mp4"),
      file("配音.m4a", "audio/mp4"),
    ]);
  await expect(page.getByText("上传了 2 个素材。")).toBeVisible();
  const grid = page.getByRole("list", { name: "素材列表" });
  const bench = grid.getByRole("listitem").filter({ hasText: "卧推侧面.mp4" });
  const voice = grid.getByRole("listitem").filter({ hasText: "配音.m4a" });
  await expect(bench).toContainText("卧推侧面的画面（模拟描述）");
  await expect(bench.getByText("AI 写的")).toBeVisible();
  // Audio is not described.
  await expect(voice.getByRole("button", { name: /AI 重写/ })).toHaveCount(0);

  // AI 重写 clears the description; the model writes it again.
  const again = page.waitForResponse(
    (r) => r.url().endsWith("/describe") && r.request().method() === "POST",
  );
  await bench
    .getByRole("button", { name: "AI 重写「卧推侧面.mp4」的描述" })
    .click();
  expect((await again).status()).toBe(202);
  await expect(bench.getByText("AI 正在写描述")).toBeVisible();
  await expect(bench).toContainText("卧推侧面的画面（模拟描述）", {
    timeout: 15_000,
  });
  await expect(bench.getByText("AI 写的")).toBeVisible();

  // Changing only the tags keeps the model's description the model's.
  await bench.getByRole("button", { name: "编辑" }).click();
  const edit = page.getByRole("dialog", { name: "卧推侧面.mp4" });
  await edit.getByLabel("标签").fill("胸");
  const tagging = page.waitForRequest(
    (r) => r.url().includes("/items/") && r.method() === "PATCH",
  );
  await edit.getByRole("button", { name: "保存" }).click();
  expect((await tagging).postDataJSON()).not.toHaveProperty("description");
  await expect(edit).toBeHidden();
  await expect(bench.getByText("AI 写的")).toBeVisible();

  // A person's description is theirs from then on.
  await bench.getByRole("button", { name: "编辑" }).click();
  await edit.getByLabel("说明").fill("教练卧推，侧面全景");
  await edit.getByRole("button", { name: "保存" }).click();
  await expect(edit).toBeHidden();
  await expect(bench).toContainText("教练卧推，侧面全景");
  await expect(bench.getByText("AI 写的")).toHaveCount(0);
  const [stored] = (await items(request, id)).filter(
    (item) => item.name === "卧推侧面.mp4",
  );
  expect(stored.description_by).toBe("person");

  // Turned off: nothing more is described, and AI 重写 is gone.
  await page.getByRole("button", { name: "自动描述", exact: true }).click();
  await dialog.getByLabel("上传的视频和图片由 AI 看画面写描述").uncheck();
  const off = page.waitForRequest(
    (r) =>
      r.url().endsWith(`/api/media-libraries/${id}`) && r.method() === "PATCH",
  );
  await dialog.getByRole("button", { name: "保存" }).click();
  expect((await off).postDataJSON()).toEqual({ describe: null });
  await expect(page.getByText("不自动写画面描述")).toBeVisible();
  await expect(bench.getByRole("button", { name: /AI 重写/ })).toHaveCount(0);
  const item = (await items(request, id)).find(
    (x) => x.name === "卧推侧面.mp4",
  )!;
  expect(
    (
      await request.post(
        `/api/media-libraries/${id}/items/${item.item_id}/describe`,
      )
    ).status(),
  ).toBe(409);
});

test("the 检索素材 node searches a bound library and is tried with it", async ({
  page,
  request,
}) => {
  const library = await newLibrary(request);
  const app = unique("footage");
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title: "找素材", from: { blank: true } },
      })
    ).status(),
  ).toBe(201);
  await page.goto(`/apps/${app}`);
  await page.getByRole("button", { name: "添加节点", exact: true }).click();
  await page
    .getByRole("dialog", { name: "添加节点" })
    .getByRole("region", { name: "工具" })
    .getByRole("button", { name: "检索素材", exact: true })
    .click();
  const form = panel(page);
  await expect(form).toContainText(
    "先在应用设置的「媒体库」一节绑定一个媒体库。",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(form).toContainText("要选一个绑定到应用的媒体库");

  const section = page.getByRole("region", { name: "媒体库" });
  await section.getByLabel("再绑定一个").selectOption(library.library_id);
  await form.getByLabel("从哪个媒体库找").selectOption(library.library_id);
  await form
    .locator(".ref-field")
    .filter({ has: page.getByText("搜索词（选填）", { exact: true }) })
    .getByRole("textbox")
    .fill("卧推 侧面");
  await form.getByLabel("图片").uncheck();
  await form.getByLabel("要带的标签（选填）").fill("胸, 卧推");
  await form.getByLabel("最多取几个").fill("10");
  const saving = page.waitForRequest(
    (r) => r.url().endsWith(`/api/apps/${app}/draft`) && r.method() === "PUT",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  const sent = (await saving).postDataJSON() as { config: FlowConfig };
  expect(sent.config.media).toEqual({
    libraries: [{ library_id: library.library_id, access: "read" }],
  });
  const node = sent.config.graph.nodes.find(
    (item) => item.type === "media_search",
  );
  expect(node).toMatchObject({
    type: "media_search",
    library: library.library_id,
    query: "卧推 侧面",
    kinds: ["video"],
    tags: ["胸", "卧推"],
    limit: 10,
  });
  await expect(form).not.toContainText("要选一个绑定到应用的媒体库");

  // Tried on its own: the items come with what they show.
  const trial = form.getByRole("region", { name: "试跑这个节点" });
  await trial.getByRole("button", { name: "开始试跑" }).click();
  const result = trial.getByRole("region", { name: "试跑结果" });
  await expect(result).toContainText("试跑成功");
  await expect(result).toContainText("健身房杠铃卧推，侧面全景");
  await expect(result).toContainText('"count": 2');
});
