import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type { Document, KnowledgeBase } from "../lib/knowledge";

// docs/api/knowledge-governance.md: owners, validity dates, versions, templates and
// retrieval regression on the knowledge base page.
test.describe.configure({ timeout: 90_000 });

const unique = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
const policy =
  "退换货期限为签收后七天。请保留原包装和购买凭证，申请通过后原路退还货款。";
const members =
  "会员积分可以在下单时抵扣现金，一百积分抵扣一元，积分有效期一年。";

async function readyBase(request: APIRequestContext): Promise<KnowledgeBase> {
  const made = await request.post("/api/knowledge-bases", {
    data: { name: unique("治理") },
  });
  expect(made.status()).toBe(201);
  const base: KnowledgeBase = await made.json();
  const uploaded = await request.post(
    `/api/knowledge-bases/${base.kb_id}/documents`,
    {
      multipart: {
        files: {
          name: "退货.txt",
          mimeType: "text/plain",
          buffer: Buffer.from(policy),
        },
      },
    },
  );
  expect(uploaded.status()).toBe(202);
  await request.post(`/api/knowledge-bases/${base.kb_id}/documents`, {
    multipart: {
      files: {
        name: "会员.md",
        mimeType: "text/markdown",
        buffer: Buffer.from(members),
      },
    },
  });
  // The mock processes documents as they are listed.
  await expect
    .poll(async () => {
      const docs: Document[] = await (
        await request.get(`/api/knowledge-bases/${base.kb_id}/documents`)
      ).json();
      return docs.every((doc) => doc.status === "ready");
    })
    .toBe(true);
  return base;
}

const docRow = (page: Page, name: string) =>
  page
    .getByRole("table", { name: "文档列表" })
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name, exact: true }) });

test("documents get owners, validity dates and versions", async ({
  page,
  request,
}) => {
  const base = await readyBase(request);
  await page.goto(`/knowledge/${base.kb_id}`);
  const row = docRow(page, "退货.txt");
  await expect(row).toContainText("长期有效");
  await expect(row).toContainText("v1");
  await expect(row).toContainText("近 30 天没有运行用到");

  await row.getByRole("button", { name: "编辑「退货.txt」" }).click();
  const edit = page.getByRole("dialog", { name: "编辑「退货.txt」" });
  await edit.getByLabel("负责人").fill("张三");
  await edit.getByLabel("生效日期").fill("2026-03-01");
  await edit.getByLabel("失效日期").fill("2026-01-01");
  await edit.getByRole("button", { name: "保存" }).click();
  await expect(edit).toContainText("失效日期不能早于生效日期");
  await edit.getByLabel("生效日期").fill("2020-01-01");
  await edit.getByLabel("失效日期").fill("2020-12-31");
  await edit.getByRole("button", { name: "保存" }).click();
  await expect(edit).toBeHidden();
  await expect(row).toContainText("张三");
  await expect(row).toContainText("2020-01-01 至 2020-12-31");
  await expect(row).toContainText("已失效");
  await expect(page.getByText("1 个已失效")).toBeVisible();

  // The search test leaves expired documents out unless asked.
  await page.getByRole("tab", { name: "检索测试" }).click();
  await page.getByLabel("问题").fill("退货期限");
  await page.getByRole("button", { name: "检索", exact: true }).click();
  const results = page.getByRole("region", { name: "检索结果" });
  await expect(results).toContainText("条命中");
  await expect(results.getByRole("button", { name: "退货.txt" })).toHaveCount(
    0,
  );
  await page.getByLabel(/也查没生效和已失效的文档/).check();
  await page.getByRole("button", { name: "检索", exact: true }).click();
  await expect(
    results.getByRole("button", { name: "退货.txt" }).first(),
  ).toBeVisible();

  // Replacing an upload keeps the document and adds a version.
  await page.getByRole("tab", { name: "文档" }).click();
  await page.getByLabel("同名文件替换原文档（版本加 1）").check();
  await page.getByLabel("上传文档").setInputFiles({
    name: "退货.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("退货期限改为签收后十五天，请保留购买凭证。"),
  });
  await expect(row).toContainText("v2");
  await expect(
    page.getByRole("table", { name: "文档列表" }).getByRole("row"),
  ).toHaveCount(3);
  await row.getByRole("button", { name: "「退货.txt」的版本记录" }).click();
  const versions = page.getByRole("dialog", { name: "「退货.txt」的版本记录" });
  await expect(
    versions.getByRole("table", { name: "版本记录" }).getByRole("row"),
  ).toHaveCount(3);
  await expect(versions.getByRole("row").nth(1)).toContainText("v2");
});

test("templates write a definition document", async ({ page, request }) => {
  const base = await readyBase(request);
  await page.goto(`/knowledge/${base.kb_id}`);
  await page.getByRole("button", { name: "用模板写文档" }).click();
  const dialog = page.getByRole("dialog", { name: "用模板写文档" });
  await dialog.getByRole("button", { name: "表说明" }).click();
  await dialog.getByLabel("名称", { exact: true }).fill("订单明细");
  await expect(dialog).toContainText("保存为 表说明-订单明细.md");
  await expect(dialog.getByLabel("正文")).toHaveValue(/^# 订单明细\n/);
  await expect(dialog.getByLabel("正文")).toHaveValue(/## 主键/);
  await dialog.getByRole("button", { name: "保存到知识库" }).click();
  await expect(dialog).toBeHidden();
  await expect(docRow(page, "表说明-订单明细.md")).toBeVisible();
});

test("regression cases run against the documents in effect", async ({
  page,
  request,
}) => {
  const base = await readyBase(request);
  await page.goto(`/knowledge/${base.kb_id}`);
  await page.getByRole("tab", { name: "检索回归" }).click();
  const panel = page.getByRole("region", { name: "检索回归" });
  await expect(panel).toContainText("还没有用例");
  await expect(panel.getByRole("button", { name: "运行回归" })).toBeDisabled();

  await panel.getByRole("button", { name: "加一条用例" }).click();
  await panel.getByLabel("第 1 条的问题").fill("退货期限");
  await panel
    .getByLabel("第 1 条应找到的文档")
    .selectOption(["退货.txt", "会员.md"]);
  await panel.getByRole("button", { name: "运行回归" }).click();
  await expect(panel.getByLabel("回归结果")).toContainText("通过 1 / 1");
  await expect(panel.getByLabel("第 1 条的结果")).toContainText("通过");

  // Expiring a document makes the run stale and the case then fails.
  const docs: Document[] = await (
    await request.get(`/api/knowledge-bases/${base.kb_id}/documents`)
  ).json();
  const policyDoc = docs.find((doc) => doc.name === "退货.txt")!;
  await request.patch(
    `/api/knowledge-bases/${base.kb_id}/documents/${policyDoc.document_id}`,
    { data: { valid_until: "2020-12-31" } },
  );
  await page.reload();
  await page.getByRole("tab", { name: "检索回归" }).click();
  await expect(panel).toContainText("文档改过，建议重跑回归。");
  await panel.getByRole("button", { name: "运行回归" }).click();
  await expect(panel.getByLabel("回归结果")).toContainText("通过 0 / 1");
  await expect(panel.getByLabel("第 1 条的结果")).toContainText("缺：退货.txt");

  const listed = await (
    await request.get(`/api/catalog?kind=knowledge`)
  ).json();
  const item = listed.items.find(
    (found: { id: string }) => found.id === base.kb_id,
  );
  expect(item.quality).toEqual({
    score: 0,
    basis: "检索回归通过率",
    detail: "1 条用例，通过 0 条",
  });
});
