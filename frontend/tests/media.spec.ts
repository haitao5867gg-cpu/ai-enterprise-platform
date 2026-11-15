import { test, expect, type APIRequestContext } from "@playwright/test";
import type { AppDetail, FlowConfig } from "../lib/apps";
import type { MediaItem, MediaLibrary, MediaPage } from "../lib/media";
import { runTab } from "./run-page";

// docs/api/media.md: the media library pages, binding libraries to a flow app, picking
// an item when starting a run, and the files a run made on its page.

test.describe.configure({ timeout: 90_000 });
test.use({ actionTimeout: 15_000 });
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
});

const unique = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
const file = (name: string, mimeType: string) => ({
  name,
  mimeType,
  buffer: Buffer.from(`fake ${name}`),
});

async function libraryNamed(
  request: APIRequestContext,
  name: string,
): Promise<MediaLibrary> {
  const libraries = (await (
    await request.get("/api/media-libraries")
  ).json()) as MediaLibrary[];
  const found = libraries.find((library) => library.name === name);
  expect(found, name).toBeTruthy();
  return found!;
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

test("a media library is made, filled, searched, edited and emptied on its pages", async ({
  page,
  request,
}) => {
  const name = unique("教练素材");
  await page.goto("/media");
  await expect(
    page.getByRole("heading", { name: "媒体库", level: 1 }),
  ).toBeVisible();
  await page.getByRole("button", { name: "新建媒体库" }).click();
  const dialog = page.getByRole("dialog", { name: "新建媒体库" });
  await dialog.getByLabel("名称").fill(name);
  await dialog.getByLabel("说明").fill("教练自己拍的镜头");
  await dialog.getByRole("button", { name: "保存" }).click();
  await expect(dialog).toBeHidden();
  await page.getByRole("link", { name: new RegExp(name) }).click();
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();

  // A file of another kind is refused before anything is sent.
  await page
    .getByLabel("上传素材")
    .setInputFiles([file("说明.txt", "text/plain")]);
  await expect(page.locator(".error-notice")).toContainText(
    "文件「说明.txt」：这里只收视频、音频、图片",
  );

  await page
    .getByLabel("上传素材")
    .setInputFiles([
      file("卧推侧面.mp4", "video/mp4"),
      file("配音.m4a", "audio/mp4"),
    ]);
  await expect(page.getByText("上传了 2 个素材。")).toBeVisible();
  const grid = page.getByRole("list", { name: "素材列表" });
  await expect(grid.getByRole("listitem")).toHaveCount(2);
  await expect(page.locator(".knowledge-overview")).toContainText("2 个素材");
  // Each plays where it is, through the proxy.
  const video = grid.locator("video");
  await expect(video).toHaveAttribute(
    "src",
    /\/api\/media-libraries\/.+\/file$/,
  );

  await page.getByLabel("搜索素材").fill("卧推");
  await expect(grid.getByRole("listitem")).toHaveCount(1);
  await page.getByLabel("搜索素材").fill("");
  await page.getByLabel("素材类型").selectOption("audio");
  await expect(grid.getByRole("listitem")).toHaveCount(1);
  await expect(grid).toContainText("配音.m4a");
  await page.getByLabel("素材类型").selectOption("");

  // Described and tagged for flows to find, and kept out of their picks.
  const card = grid.getByRole("listitem").filter({ hasText: "卧推侧面.mp4" });
  await card.getByRole("button", { name: "编辑" }).click();
  const edit = page.getByRole("dialog", { name: "卧推侧面.mp4" });
  await edit.getByLabel("说明").fill("健身房杠铃卧推，侧面全景");
  await edit.getByLabel("标签").fill("胸，卧推, 胸");
  await edit.getByLabel("流程可以选用它").uncheck();
  await edit.getByRole("button", { name: "保存" }).click();
  await expect(edit).toBeHidden();
  await expect(card).toContainText("健身房杠铃卧推，侧面全景");
  await expect(card.locator(".app-tag")).toHaveText(["胸", "卧推"]);
  await expect(card).toContainText("流程不选用");
  const library = await libraryNamed(request, name);
  const saved = (await items(request, library.library_id)).find(
    (item) => item.name === "卧推侧面.mp4",
  )!;
  expect(saved).toMatchObject({ tags: ["胸", "卧推"], usable: false });
  await page.getByLabel("能否选用").selectOption("false");
  await expect(grid.getByRole("listitem")).toHaveCount(1);
  await page.getByLabel("能否选用").selectOption("");

  page.once("dialog", (d) => void d.accept());
  await card.getByRole("button", { name: "删除" }).click();
  await expect(grid.getByRole("listitem")).toHaveCount(1);

  page.once("dialog", (d) => void d.accept());
  await page.getByRole("button", { name: "删除媒体库" }).click();
  await expect(page).toHaveURL(/\/media$/);
  await expect(page.getByRole("link", { name: new RegExp(name) })).toHaveCount(
    0,
  );
});

test("a flow app binds a library, its runs pick an item, and the run page lists what it made", async ({
  page,
  request,
}) => {
  const app = unique("coach");
  const created = await request.post("/api/apps", {
    data: { app, title: "教练做视频", from: { blank: true } },
  });
  expect(created.status()).toBe(201);
  const library = await libraryNamed(request, "教练素材");

  // Bound on the editor page: the draft carries `media.libraries`.
  await page.goto(`/apps/${app}`);
  const section = page.getByRole("region", { name: "媒体库" });
  await expect(section).toContainText("还没有绑定媒体库。");
  await section.getByLabel("再绑定一个").selectOption(library.library_id);
  await section.getByLabel("权限").selectOption("write");
  const saving = page.waitForRequest(
    (r) => r.url().endsWith(`/api/apps/${app}/draft`) && r.method() === "PUT",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  const sent = (await saving).postDataJSON() as { config: FlowConfig };
  expect(sent.config.media).toEqual({
    libraries: [{ library_id: library.library_id, access: "write" }],
  });

  // A media start input: the editor offers 素材 and the bound library.
  const detail = (await (
    await request.get(`/api/apps/${app}`)
  ).json()) as AppDetail;
  const config = structuredClone(
    (detail.draft ?? detail.current).config,
  ) as FlowConfig;
  const start = config.graph.nodes.find((node) => node.type === "start")!;
  if (start.type !== "start") throw new Error("no start node");
  start.inputs = [
    ...start.inputs.map((input) => ({ ...input, required: false })),
    {
      key: "voiceover",
      label: "配音",
      type: "media",
      required: true,
      library: library.library_id,
      kinds: ["audio"],
    },
  ];
  config.media = {
    libraries: [{ library_id: library.library_id, access: "write" }],
  };
  const put = await request.put(`/api/apps/${app}/draft`, {
    data: { config },
  });
  expect(put.status(), await put.text()).toBe(200);
  expect(
    (
      await request.post(`/api/apps/${app}/draft/publish`, {
        data: { note: "配音输入" },
      })
    ).status(),
  ).toBe(200);

  await page.goto(`/apps/${app}`);
  await page.getByRole("button", { name: /开始/ }).first().click();
  const settings = page
    .getByRole("region", { name: /^节点设置：/ })
    .getByRole("group", { name: "输入字段 2" });
  await expect(settings.getByLabel("类型")).toHaveValue("media");
  await expect(settings.getByLabel("从哪个媒体库选")).toHaveValue(
    library.library_id,
  );
  await expect(settings.getByLabel("音频")).toBeChecked();
  await expect(settings.getByLabel("视频")).not.toBeChecked();

  // The new-run page picks from the library: only audio, and an upload is used at once.
  await page.goto(`/runs/new?app=${app}`);
  const form = page.getByRole("form", { name: "新建运行" });
  const picker = form.locator("#input-voiceover");
  await expect(picker.locator("option")).toContainText([
    "请选择素材",
    "配音0115.m4a",
  ]);
  await expect(
    picker.locator("option", { hasText: "卧推侧面.mp4" }),
  ).toHaveCount(0);
  await form
    .getByLabel("上传一个素材")
    .setInputFiles([file("配音0116.m4a", "audio/mp4")]);
  await expect(picker.locator("option:checked")).toContainText("配音0116.m4a");
  await expect(form.locator("audio")).toHaveCount(1);
  const voice = (await items(request, library.library_id)).find(
    (item) => item.name === "配音0116.m4a",
  )!;
  const starting = page.waitForRequest(
    (r) => r.url().endsWith("/api/runs") && r.method() === "POST",
  );
  await form.getByRole("button", { name: "开始运行" }).click();
  expect((await starting).postDataJSON()).toMatchObject({
    app,
    input: { voiceover: voice.item_id },
  });
  await page.waitForURL(/\/runs\/(?!new\b)[^/?]+$/);
  const runId = page.url().split("/").pop()!;

  // A file the run made into the library: listed with a download on the run page.
  const made = (await (
    await request.post(`/api/media-libraries/${library.library_id}/items`, {
      multipart: {
        files: {
          name: "成片.mp4",
          mimeType: "video/mp4",
          buffer: Buffer.from("cut"),
        },
      },
    })
  ).json()) as MediaItem[];
  expect(
    (
      await request.patch(
        `/api/media-libraries/${library.library_id}/items/${made[0].item_id}`,
        { data: { source: { kind: "run", run_id: runId } } },
      )
    ).status(),
  ).toBe(200);
  await page.reload();
  const files = page.getByRole("region", { name: "产出的文件" }).first();
  await expect(files).toContainText("成片.mp4");
  await expect(files.getByRole("link", { name: "下载" })).toHaveAttribute(
    "href",
    `/api/media-libraries/${library.library_id}/items/${made[0].item_id}/file`,
  );

  // The item the run was started with: named and playable under 详情.
  await runTab(page, "详情");
  const info = page.locator(".run-info");
  await expect(info.locator(".run-media-input")).toContainText("配音0116.m4a");
  await expect(info.locator(".run-media-input audio")).toHaveAttribute(
    "src",
    `/api/media-libraries/${library.library_id}/items/${voice.item_id}/file`,
  );
});
