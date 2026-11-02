import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

// docs/api/catalog.md: every asset in one list, its owner and tags, who uses it, and
// proposing it to the assets repository.
const suffix = () =>
  `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

async function newSkill(request: APIRequestContext) {
  const name = `tone-${suffix()}`;
  const made = await request.post("/api/skills", {
    data: {
      name,
      description: "客服说话的语气",
      body: "要客气。",
      references: {},
    },
  });
  expect(made.status()).toBe(201);
  const published = await request.post(`/api/skills/${name}/publish`, {
    data: { note: "第一版" },
  });
  expect(published.ok()).toBeTruthy();
  return name;
}

const row = (page: Page, title: string) =>
  page
    .getByRole("table", { name: "资产列表" })
    .getByRole("row", { name: title, exact: true });

test("the catalog lists assets, keeps owners and tags and proposes them as PRs", async ({
  page,
  request,
}) => {
  const skill = await newSkill(request);
  await page.goto("/catalog");
  await expect(page.getByRole("link", { name: "资产目录" })).toBeVisible();
  await page
    .getByRole("group", { name: "筛选类别" })
    .getByRole("button", { name: /^Skill \d+$/ })
    .click();
  const mine = row(page, skill);
  await expect(mine).toContainText("v1");
  await expect(mine).toContainText("没有应用在用");
  await expect(mine).toContainText("暂无");
  await expect(mine).toContainText("未提交");

  await mine.getByRole("button", { name: `编辑「${skill}」` }).click();
  const edit = page.getByRole("dialog", { name: `编辑「${skill}」` });
  await edit.getByLabel("负责人").fill("王五");
  await edit.getByLabel("标签").fill(`客服、${skill}-组, 客服`);
  await edit.getByRole("button", { name: "保存" }).click();
  await expect(edit).toBeHidden();
  await expect(mine).toContainText("王五");
  await expect(mine.locator(".app-tag")).toHaveText(["客服", `${skill}-组`]);

  // Search covers tags across all kinds.
  await page
    .getByRole("group", { name: "筛选类别" })
    .getByRole("button", { name: /^全部 \d+$/ })
    .click();
  await page.getByLabel("搜索资产").fill(`${skill}-组`);
  await expect(
    page.getByRole("table", { name: "资产列表" }).locator("tbody tr"),
  ).toHaveCount(1);

  await mine
    .getByRole("button", { name: `把「${skill}」提交到资产仓库` })
    .click();
  const propose = page.getByRole("dialog", {
    name: `把「${skill}」提交到资产仓库`,
  });
  await expect(propose).toContainText("提交当前版本 v1。");
  await propose.getByLabel("说明").fill("第一次入库");
  await propose.getByRole("button", { name: "提交", exact: true }).click();
  await expect(propose.getByRole("status")).toContainText(/已开 PR #\d+/);
  const link = propose.getByRole("link", { name: /打开 PR #\d+ ↗/ });
  const href = await link.getAttribute("href");
  expect(href).toMatch(/^https:\/\/github\.com\/acme\/aep-assets\/pull\/\d+$/);
  await propose.getByRole("button", { name: "完成" }).click();
  const number = href!.split("/").at(-1);
  await expect(
    mine.getByRole("link", { name: `PR #${number} ↗` }),
  ).toHaveAttribute("href", href!);

  await mine
    .getByRole("button", { name: `把「${skill}」提交到资产仓库` })
    .click();
  await expect(propose).toContainText(`会更新已有的 PR #${number}。`);
  await propose.getByRole("button", { name: "提交", exact: true }).click();
  await expect(propose.getByRole("status")).toContainText(
    `已更新 PR #${number}`,
  );
});

test("templates list the apps made from them; tools are not proposed", async ({
  page,
  request,
}) => {
  const app = `qa-${suffix()}`;
  const made = await request.post("/api/apps", {
    data: { app, title: `问答 ${app}`, from: { template: "qa-approval" } },
  });
  expect(made.status()).toBe(201);
  const listed = await (await request.get("/api/catalog?kind=template")).json();
  const template = listed.items.find(
    (item: { id: string }) => item.id === "qa-approval",
  );
  expect(template.owner).toBe("平台内置");

  await page.goto("/catalog");
  await page
    .getByRole("group", { name: "筛选类别" })
    .getByRole("button", { name: /^应用模板 \d+$/ })
    .click();
  const found = row(page, template.title);
  await found.locator("summary").click();
  await expect(
    found.getByRole("link", { name: `问答 ${app}` }),
  ).toHaveAttribute("href", `/apps/${app}`);
  await expect(found).toContainText("从模板新建");
  await expect(
    found.getByRole("button", { name: /提交到资产仓库/ }),
  ).toHaveCount(0);

  const tools = await (await request.get("/api/catalog?kind=tool")).json();
  const tool = tools.items[0];
  const refused = await request.post(
    `/api/catalog/tool/${tool.id}/pull-request`,
    { data: {} },
  );
  expect(refused.status()).toBe(409);
  expect((await refused.json()).detail).toBe("数据源和工具不提交到资产仓库");
  expect((await request.get("/api/catalog?kind=nope")).status()).toBe(422);
});
