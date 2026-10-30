import {
  test,
  expect,
  type APIRequestContext,
  type Locator,
  type Page,
} from "@playwright/test";
import type { AppDetail, FlowConfig } from "../lib/apps";

// Each test walks through many edits on a dev server.
test.describe.configure({ timeout: 90_000 });

// Exercise the existing list editor explicitly; desktop now defaults to canvas.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
});

const id = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
async function blank(request: APIRequestContext, page: Page) {
  const app = id("editor");
  const response = await request.post("/api/apps", {
    data: { app, title: "编辑测试", from: { blank: true } },
  });
  expect(response.status()).toBe(201);
  await page.goto(`/apps/${app}`);
  await expect(page.getByRole("region", { name: "编排" })).toBeVisible();
  return app;
}
async function draftConfig(request: APIRequestContext, app: string) {
  const detail: AppDetail = await (
    await request.get(`/api/apps/${app}`)
  ).json();
  return detail.draft?.config as FlowConfig;
}
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });
const nodeButton = (page: Page, title: string, node: string) =>
  page
    .getByRole("complementary", { name: "节点列表" })
    .getByRole("button", { name: new RegExp(`^${title}（${node}）`) });
async function insert(field: Locator, label: string, ref: string) {
  await field.getByRole("button", { name: `插入变量：${label}` }).click();
  await field.getByRole("button", { name: ref, exact: true }).click();
}
const refField = (page: Page, label: string) =>
  panel(page)
    .locator(".ref-field")
    .filter({ has: page.getByText(label, { exact: true }) });
async function addNode(page: Page, label: string) {
  await page.getByRole("button", { name: "添加节点", exact: true }).click();
  await page
    .getByRole("dialog", { name: /添加节点|新建节点/ })
    .getByRole("button", { name: label, exact: true })
    .click();
}
async function next(page: Page, branch: string, target: string) {
  await panel(page)
    .getByRole("group", { name: "下一步" })
    .getByLabel(branch, { exact: true })
    .selectOption({ label: target });
}
async function save(page: Page) {
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
}

test("build intent routing without code: add nodes, connect with dropdowns, insert variables, save", async ({
  page,
  request,
}) => {
  const app = await blank(request, page);
  await addNode(page, "意图分类");
  await expect(panel(page)).toHaveAccessibleName("节点设置：意图分类");
  // Not connected yet, so there is nothing to reference.
  await refField(page, "要分类的文本")
    .getByRole("button", { name: "插入变量：要分类的文本" })
    .click();
  await expect(panel(page).locator(".ref-menu")).toContainText(
    "这里还没有可引用的变量",
  );
  await nodeButton(page, "开始", "start").click();
  await next(page, "下一步", "意图分类（classifier）");
  await nodeButton(page, "意图分类", "classifier").click();
  const text = refField(page, "要分类的文本");
  await text.getByRole("textbox").fill("");
  await insert(text, "要分类的文本", "{{start.question}}");
  await expect(text.getByRole("textbox")).toHaveValue("{{start.question}}");
  await next(page, "产品咨询", "回答（answer）");
  // "新建节点…" creates the node and connects the branch to it.
  await next(page, "售后", "新建节点…");
  await page
    .getByRole("dialog", { name: /新建节点并连到「意图分类」/ })
    .getByRole("button", { name: "调用模型", exact: true })
    .click();
  await expect(panel(page)).toHaveAccessibleName("节点设置：调用模型");
  await panel(page).getByLabel("标题", { exact: true }).fill("起草售后回复");
  const prompt = refField(page, "提示词");
  await prompt.getByRole("textbox").fill("售后问题：");
  await insert(prompt, "提示词", "{{start.question}}");
  await expect(prompt.getByRole("textbox")).toHaveValue(
    "售后问题：{{start.question}}",
  );
  await next(page, "下一步", "新建节点…");
  await page
    .getByRole("dialog", { name: /新建节点并连到「起草售后回复」/ })
    .getByRole("button", { name: "人工", exact: true })
    .click();
  const show = refField(page, "给处理人看的内容");
  await insert(show, "给处理人看的内容", "{{llm.text}}");
  await next(page, "确认", "结束（end）");
  await next(page, "驳回", "起草售后回复（llm）");
  await nodeButton(page, "意图分类", "classifier").click();
  await next(page, "都不是", "新建节点…");
  await page
    .getByRole("dialog", { name: /新建节点/ })
    .getByRole("button", { name: "文本拼接", exact: true })
    .click();
  await next(page, "下一步", "结束（end）");
  await nodeButton(page, "结束", "end").click();
  const output = panel(page).getByRole("group", { name: "结果字段 1" });
  await output.getByRole("button", { name: "添加取值" }).click();
  await output.getByRole("button", { name: "添加取值" }).click();
  await insert(
    output.getByRole("group", { name: "取值 2" }),
    "变量",
    "{{llm.text}}",
  );
  await insert(
    output.getByRole("group", { name: "取值 3" }),
    "变量",
    "{{template.text}}",
  );
  await expect(page.getByText("有未保存的修改").first()).toBeVisible();
  await save(page);
  await expect(page.locator(".asset-notice")).toContainText("草稿已保存");
  const diagram = page.getByRole("group", { name: "流程图", exact: true });
  expect(
    (await diagram.locator(".flow-edge-label").allTextContents()).sort(),
  ).toEqual(["售后", "产品咨询", "都不是", "确认", "驳回"].sort());
  const config = await draftConfig(request, app);
  expect(config.graph.nodes.map((node) => node.id)).toEqual([
    "start",
    "classifier",
    "template",
    "llm",
    "human",
    "answer",
    "end",
  ]);
  expect(config.graph.edges).toEqual(
    expect.arrayContaining([
      { from: "start", to: "classifier" },
      { from: "classifier", to: "llm", branch: "after_sales" },
      { from: "classifier", to: "answer", branch: "product" },
      { from: "classifier", to: "template", branch: "other" },
      { from: "llm", to: "human" },
      { from: "human", to: "end", branch: "approve" },
      { from: "human", to: "llm", branch: "reject" },
      { from: "template", to: "end" },
      { from: "answer", to: "end" },
    ]),
  );
  expect(config.graph.edges).toHaveLength(9);
  expect(config.graph.nodes.find((node) => node.id === "end")).toMatchObject({
    outputs: [
      {
        key: "answer",
        from: ["{{answer.text}}", "{{llm.text}}", "{{template.text}}"],
      },
    ],
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("save errors land on the node and setting: missing branch, unknown variable, loop without exit", async ({
  page,
  request,
}) => {
  const app = await blank(request, page);
  const alert = page.getByRole("main").getByRole("alert");
  await nodeButton(page, "回答", "answer").click();
  await next(page, "下一步", "（未连接）");
  await save(page);
  await expect(alert).toContainText("草稿没有保存");
  await expect(
    panel(page).getByRole("group", { name: "下一步" }).locator(".field-error"),
  ).toHaveText("每个分支须连接一条出边");
  await expect(nodeButton(page, "回答", "answer")).toHaveAccessibleName(
    /有问题/,
  );
  await expect(
    page.getByRole("button", { name: /^回答，模型生成.*有问题$/ }),
  ).toHaveClass(/invalid/);
  await next(page, "下一步", "结束（end）");

  const prompt = refField(page, "提示词");
  await prompt.getByRole("textbox").fill("{{start.nothing}}");
  await save(page);
  await expect(prompt.locator(".field-error")).toHaveText(
    "引用了不存在的变量 {{start.nothing}}",
  );
  await expect(prompt.getByRole("textbox")).toHaveAttribute(
    "aria-invalid",
    "true",
  );
  await prompt.getByRole("textbox").fill("{{start.question}}");

  await next(page, "下一步", "回答（answer）");
  await save(page);
  await expect(panel(page).locator(".node-issue")).toContainText(
    "循环需要一个出口",
  );
  await nodeButton(page, "结束", "end").click();
  await expect(panel(page).locator(".node-issue")).toContainText(
    "从开始节点到不了这个节点",
  );
  await nodeButton(page, "回答", "answer").click();
  await next(page, "下一步", "结束（end）");
  await prompt.getByRole("textbox").fill("修正后：{{start.question}}");
  await save(page);
  await expect(page.locator(".asset-notice")).toContainText("草稿已保存");
  await expect(nodeButton(page, "回答", "answer")).not.toHaveAccessibleName(
    /有问题/,
  );
  expect((await draftConfig(request, app)).graph.edges).toHaveLength(2);
});

test("renaming a node updates edges and references; deleting lists what is affected", async ({
  page,
  request,
}) => {
  const app = await blank(request, page);
  await nodeButton(page, "回答", "answer").click();
  const idField = panel(page).getByLabel("标识", { exact: true });
  await idField.fill("start");
  await expect(panel(page)).toContainText("已有节点用了这个标识");
  await idField.fill("reply");
  await idField.press("Enter");
  await expect(nodeButton(page, "回答", "reply")).toBeVisible();
  await nodeButton(page, "结束", "end").click();
  await expect(
    panel(page).getByRole("group", { name: "取值 1" }).getByRole("textbox"),
  ).toHaveValue("{{reply.text}}");
  await nodeButton(page, "开始", "start").click();
  await expect(
    panel(page).getByRole("group", { name: "下一步" }).getByRole("combobox"),
  ).toHaveValue("reply");

  await page.getByRole("button", { name: "删除节点 回答" }).click();
  const confirm = page.getByRole("dialog", { name: "删除节点「回答」" });
  await expect(confirm).toContainText("开始 → 回答");
  await expect(confirm).toContainText("回答 → 结束");
  await expect(confirm).toContainText("结束 · outputs.0.from.0");
  await confirm.getByRole("button", { name: "取消" }).click();
  await expect(nodeButton(page, "回答", "reply")).toBeVisible();
  await page.getByRole("button", { name: "删除节点 回答" }).click();
  await confirm.getByRole("button", { name: "确认删除" }).click();
  await expect(nodeButton(page, "回答", "reply")).toHaveCount(0);
  await save(page);
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "草稿没有保存",
  );
  expect((await draftConfig(request, app)) ?? null).toBeNull();
});

test("tool node: arguments from the tool schema, retry twice then the error branch", async ({
  page,
  request,
}) => {
  const app = await blank(request, page);
  await nodeButton(page, "回答", "answer").click();
  await addNode(page, "调用工具");
  await panel(page)
    .getByLabel("数据源", { exact: true })
    .selectOption("sample-postgres");
  await panel(page)
    .getByLabel("工具", { exact: true })
    .selectOption("describe_table");
  await expect(panel(page).getByText("schema（必填）")).toBeVisible();
  await refField(page, "schema（必填）").getByRole("textbox").fill("public");
  await refField(page, "table（必填）").getByRole("textbox").fill("orders");
  const errors = panel(page).getByRole("group", { name: "出错时" });
  await errors.getByLabel("重试次数").fill("2");
  await expect(errors.getByLabel("重试间隔秒数")).toHaveValue("1");
  await expect(
    panel(page).getByRole("group", { name: "下一步" }).getByLabel("出错"),
  ).toHaveCount(0);
  await errors.getByLabel("重试后仍失败").selectOption("branch");
  const nextSteps = panel(page).getByRole("group", { name: "下一步" });
  await expect(nextSteps.getByLabel("出错", { exact: true })).toBeVisible();
  await next(page, "下一步", "结束（end）");
  await next(page, "出错", "结束（end）");
  await nodeButton(page, "回答", "answer").click();
  await next(page, "下一步", "调用工具（tool）");
  await save(page);
  await expect(page.locator(".asset-notice")).toContainText("草稿已保存");
  const tool = (await draftConfig(request, app)).graph.nodes.find(
    (node) => node.id === "tool",
  );
  expect(tool).toMatchObject({
    datasource: "sample-postgres",
    tool: "describe_table",
    args: { schema: "public", table: "orders" },
    on_error: { retry: { max: 2, interval_seconds: 1 }, then: "branch" },
  });
  expect((await draftConfig(request, app)).graph.edges).toContainEqual({
    from: "tool",
    to: "end",
    branch: "error",
  });
});

test("single-node test uses unsaved settings and shows outputs", async ({
  page,
  request,
}) => {
  await blank(request, page);
  await nodeButton(page, "回答", "answer").click();
  await refField(page, "提示词")
    .getByRole("textbox")
    .fill("问：{{start.question}}");
  const test = panel(page).getByRole("region", { name: "试跑这个节点" });
  await test.getByLabel("{{start.question}}").fill("单节点");
  const sent = page.waitForRequest((request) =>
    request.url().endsWith("/nodes/answer/test"),
  );
  await test.getByRole("button", { name: "开始试跑" }).click();
  expect((await sent).postDataJSON()).toMatchObject({
    values: { "start.question": "单节点" },
    config: { graph: { nodes: expect.any(Array) } },
  });
  const result = test.getByRole("region", { name: "试跑结果" });
  await expect(result).toContainText("试跑成功");
  await expect(result).toContainText("（模拟回答）问：单节点");
  await nodeButton(page, "开始", "start").click();
  await expect(
    panel(page).getByRole("region", { name: "试跑这个节点" }),
  ).toHaveCount(0);
});

test("constants save with the draft; secrets show set without echoing the value", async ({
  page,
  request,
}) => {
  const app = await blank(request, page);
  const env = page.getByRole("region", { name: "常量和密钥" });
  await env.getByRole("button", { name: "添加常量" }).click();
  const constant = env.getByRole("group", { name: "常量 1" });
  await constant.getByLabel("名称").fill("refund_limit");
  await constant.getByLabel("类型").selectOption("number");
  await constant.getByLabel("值", { exact: true }).fill("500");
  await env.getByLabel("新密钥名称").fill("crm_token");
  await env.getByRole("button", { name: "添加密钥" }).click();
  const secret = env.getByRole("listitem", { name: "密钥 crm_token" });
  await expect(secret).toContainText("未设置");
  await secret.getByLabel("crm_token 的值").fill("never-show-me");
  await secret.getByRole("button", { name: "设置" }).click();
  await expect(secret).toContainText("已设置");
  await expect(secret.getByLabel("crm_token 的值")).toHaveValue("");
  await expect(page.locator("body")).not.toContainText("never-show-me");
  await nodeButton(page, "回答", "answer").click();
  await refField(page, "提示词")
    .getByRole("button", { name: "插入变量：提示词" })
    .click();
  await expect(
    panel(page).getByRole("button", { name: "{{env.refund_limit}}" }),
  ).toBeVisible();
  // Secrets are only offered in tool arguments.
  await expect(
    panel(page).getByRole("button", { name: "{{secret.crm_token}}" }),
  ).toHaveCount(0);
  await save(page);
  await expect(page.locator(".asset-notice")).toContainText("草稿已保存");
  expect((await draftConfig(request, app)).env).toEqual({
    vars: [
      {
        key: "refund_limit",
        type: "number",
        value: 500,
        description: "",
      },
    ],
    secrets: ["crm_token"],
  });
  const detail: AppDetail = await (
    await request.get(`/api/apps/${app}`)
  ).json();
  expect(detail.secrets).toContainEqual(
    expect.objectContaining({ name: "crm_token", set: true }),
  );
});

test("llm 节点「回答不写进运行过程」保存后保留", async ({ page, request }) => {
  const app = await blank(request, page);
  await nodeButton(page, "回答", "answer").click();
  const quiet = panel(page).getByRole("checkbox", {
    name: "回答不写进运行过程",
    exact: true,
  });
  await expect(quiet).not.toBeChecked();
  await quiet.check();
  await save(page);
  await expect(page.locator(".asset-notice")).toContainText("草稿已保存");
  expect(
    (await draftConfig(request, app)).graph.nodes.find(
      (node) => node.id === "answer",
    ),
  ).toMatchObject({ quiet: true });
  // The answer is still the result; the run process only has the question.
  const created = await request.post("/api/runs", {
    data: { app, draft: true, input: { question: "退货" } },
  });
  const { run_id } = await created.json();
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/runs/${run_id}`)).json()).status,
    )
    .toBe("done");
  const run = await (await request.get(`/api/runs/${run_id}`)).json();
  expect(run.result.answer).toContain("退货");
  expect(run.messages.map((message: { node: string }) => message.node)).toEqual(
    ["start"],
  );
  // JSON output never goes into the run process, so there is nothing to choose.
  await panel(page)
    .getByLabel("输出方式", { exact: true })
    .selectOption("json");
  await expect(quiet).toHaveCount(0);
});

test("a human node's extended settings are shown, kept on save and follow a renamed option", async ({
  page,
  request,
}) => {
  const app = await blank(request, page);
  const source: AppDetail = await (
    await request.get("/api/apps/plan-questions")
  ).json();
  const saved = await request.put(`/api/apps/${app}/draft`, {
    data: { config: source.current.config },
  });
  expect(saved.status(), await saved.text()).toBe(200);
  await page.reload();
  await nodeButton(page, "确认方案", "review").click();
  const extras = panel(page).getByRole("region", { name: "扩展设置" });
  await expect(extras).toContainText("只能查看，保存时原样保留");
  await expect(extras).toContainText("etl_plan_review");
  await expect(extras).toContainText("previous：{{review.record}}");
  await expect(extras).toContainText("统计哪段时间？、要不要去掉测试订单？");
  await expect(extras).toContainText("「按我的意见修改方案」处理");

  // Renaming the option renames changed_branch, comment_required and the edge.
  await panel(page)
    .getByRole("group", { name: "选项 2" })
    .getByLabel("标识", { exact: true })
    .fill("rewrite");
  await save(page);
  await expect(page.locator(".asset-notice")).toContainText("草稿已保存");
  const review = (await draftConfig(request, app)).graph.nodes.find(
    (node) => node.id === "review",
  );
  expect(review).toMatchObject({
    changed_branch: "rewrite",
    comment_required: ["rewrite"],
    view: "etl_plan_review",
    data: { plan: "{{draft.text}}", previous: "{{review.record}}" },
    fields: [{ key: "q1" }, { key: "q2" }],
  });
  expect(
    (await draftConfig(request, app)).graph.edges.find(
      (edge) => edge.from === "review" && edge.to === "draft",
    ),
  ).toMatchObject({ branch: "rewrite" });

  // A form has none of them: switching takes them away.
  await panel(page)
    .getByLabel("处理方式", { exact: true })
    .selectOption("form");
  await expect(extras).toHaveCount(0);
});

test("draft trial run asks to save first, then runs the draft and is labelled", async ({
  page,
  request,
}) => {
  await blank(request, page);
  const run = page.getByRole("button", { name: "用草稿试运行" });
  await expect(run).toBeDisabled();
  await expect(page.locator("#draft-run-reason")).toHaveText(
    "保存草稿后可以用草稿试运行。",
  );
  await nodeButton(page, "回答", "answer").click();
  await refField(page, "提示词")
    .getByRole("textbox")
    .fill("草稿：{{start.question}}");
  await expect(page.locator("#draft-run-reason")).toHaveText(
    "有未保存的修改，请先保存草稿。",
  );
  await save(page);
  await expect(page.locator(".asset-notice")).toContainText("草稿已保存");
  await run.click();
  const form = page.getByRole("form", { name: "用草稿试运行" });
  await form.getByRole("button", { name: "开始试运行" }).click();
  await expect(form.locator("#draft-input-question-error")).toContainText(
    "必填",
  );
  await form.getByLabel("客户问题").fill("退货");
  const created = page.waitForRequest(
    (request) =>
      request.method() === "POST" && request.url().endsWith("/api/runs"),
  );
  await form.getByRole("button", { name: "开始试运行" }).click();
  expect((await created).postDataJSON()).toMatchObject({
    input: { question: "退货" },
    draft: true,
  });
  await expect(page).toHaveURL(/\/runs\/[^/]+$/);
  await expect(page.getByText("试运行（未发布版本）").first()).toBeVisible();
  await expect(page.locator(".result-json")).toContainText("草稿：退货");
});

test("start inputs fold under 高级选项 and carry examples; a required input without a default cannot fold", async ({
  page,
  request,
}) => {
  const app = await blank(request, page);
  await nodeButton(page, "开始", "start").click();
  const input = panel(page).getByRole("group", { name: "输入字段 1" });
  await input.getByLabel("收进高级选项").check();
  await save(page);
  await expect(input.locator(".field-error").last()).toContainText(
    "必填且没有默认值的输入不能收进高级选项",
  );
  await input.getByLabel("收进高级选项").uncheck();
  await input.getByRole("button", { name: "添加例子" }).click();
  await input
    .getByRole("group", { name: "例子 1" })
    .getByRole("textbox", { name: "例子" })
    .fill("退货要几天到账？");
  await save(page);
  await expect(page.locator(".asset-notice")).toContainText("草稿已保存");
  let start = (await draftConfig(request, app)).graph.nodes[0];
  expect(start.type === "start" && start.inputs[0]).toMatchObject({
    key: "question",
    examples: ["退货要几天到账？"],
  });
  expect(start.type === "start" && "advanced" in start.inputs[0]).toBe(false);

  // Examples are for text only: changing the type drops them before saving.
  await input.getByLabel("类型", { exact: true }).selectOption("number");
  await expect(input.getByRole("button", { name: "添加例子" })).toHaveCount(0);
  await save(page);
  await expect(page.locator(".asset-notice")).toContainText("草稿已保存");
  start = (await draftConfig(request, app)).graph.nodes[0];
  expect(start.type === "start" && "examples" in start.inputs[0]).toBe(false);
});
