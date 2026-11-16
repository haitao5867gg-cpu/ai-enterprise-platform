import {
  test,
  expect,
  type APIRequestContext,
  type Locator,
  type Page,
} from "@playwright/test";
import type { AppDetail, EachNode, FlowConfig } from "../lib/apps";

// docs/api/each.md: the 逐项处理 node's settings, its step reading the item, and trying it
// with a list.
test.describe.configure({ timeout: 90_000 });
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
});

const unique = (prefix: string) =>
  `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });
const refField = (scope: Locator, label: string) =>
  scope
    .locator(".ref-field")
    .filter({ has: scope.page().getByText(label, { exact: true }) });

// A blank app whose draft plans shots (a JSON list) and then picks one for each.
async function shotsApp(request: APIRequestContext) {
  const app = unique("each");
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title: "逐个挑镜头", from: { blank: true } },
      })
    ).status(),
  ).toBe(201);
  const detail = (await (
    await request.get(`/api/apps/${app}`)
  ).json()) as AppDetail;
  const config = structuredClone(
    (detail.draft ?? detail.current).config,
  ) as FlowConfig;
  const pick: EachNode = {
    id: "pick",
    type: "each",
    title: "挑镜头",
    items: "{{plan.shots}}",
    do: {
      type: "llm",
      model: null,
      skills: [],
      prompt: "{{item.value}}",
      output: { kind: "text" },
    },
    concurrency: 3,
    on_item_error: "stop",
  };
  config.graph.nodes.splice(
    1,
    0,
    {
      id: "plan",
      type: "llm",
      title: "排镜头",
      model: null,
      skills: [],
      prompt: "按配音排镜头：{{start.question}}",
      output: {
        kind: "json",
        fields: [{ key: "shots", type: "array", description: "镜头" }],
      },
    },
    pick,
  );
  config.graph.edges = [
    { from: "start", to: "plan" },
    { from: "plan", to: "pick" },
    { from: "pick", to: "answer" },
    { from: "answer", to: "end" },
  ];
  const put = await request.put(`/api/apps/${app}/draft`, {
    data: { config },
  });
  expect(put.status(), await put.text()).toBe(200);
  return app;
}

test("the 逐项处理 node takes a list, a step that reads the item, and how items run", async ({
  page,
  request,
}) => {
  const app = await shotsApp(request);
  await page.goto(`/apps/${app}`);
  await page
    .getByRole("complementary", { name: "节点列表" })
    .getByRole("button", { name: /^挑镜头（pick）/ })
    .click();
  const form = panel(page);
  await expect(
    refField(form, "对哪个列表逐项处理").getByRole("textbox"),
  ).toHaveValue("{{plan.shots}}");
  await expect(form.getByLabel("每一项做什么")).toHaveValue("llm");
  const step = form.getByRole("group", { name: "每一项：调用模型" });
  // The step's prompt offers the item besides the nodes before it.
  const prompt = refField(step, "提示词");
  await prompt.getByRole("textbox").fill("");
  await prompt.getByRole("button", { name: "插入变量：提示词" }).click();
  const offered = step.getByRole("group", { name: "可插入「提示词」的变量" });
  await expect(offered).toContainText("{{item.value}}");
  await expect(offered).toContainText("{{item.position}}");
  await expect(offered).toContainText("{{plan.shots}}");
  await offered
    .getByRole("button", { name: "{{item.position}}", exact: true })
    .click();
  await prompt
    .getByRole("textbox")
    .pressSequentially("：{{item.line}}，挑一段素材");
  await form.getByLabel("同时处理几项").fill("2");
  await form.getByLabel("某一项失败时").selectOption("skip");
  const saving = page.waitForRequest(
    (r) => r.url().endsWith(`/api/apps/${app}/draft`) && r.method() === "PUT",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  const sent = (await saving).postDataJSON() as { config: FlowConfig };
  expect(sent.config.graph.nodes.find((node) => node.id === "pick")).toEqual({
    id: "pick",
    type: "each",
    title: "挑镜头",
    items: "{{plan.shots}}",
    do: {
      type: "llm",
      model: null,
      skills: [],
      prompt: "{{item.position}}：{{item.line}}，挑一段素材",
      output: { kind: "text" },
    },
    concurrency: 2,
    on_item_error: "skip",
  });

  // Tried with a list written as JSON: the step runs for every item.
  const trial = form.getByRole("region", { name: "试跑这个节点" });
  await expect(trial.getByLabel("{{item.line}}")).toHaveCount(0);
  await trial
    .getByLabel("{{plan.shots}}")
    .fill('[{"line": "冬天减脂难吗"}, {"line": "关键是热量缺口"}]');
  await trial.getByRole("button", { name: "开始试跑" }).click();
  const result = trial.getByRole("region", { name: "试跑结果" });
  await expect(result).toContainText("试跑成功");
  await expect(result).toContainText("（模拟回答）1：冬天减脂难吗，挑一段素材");
  await expect(result).toContainText(
    "（模拟回答）2：关键是热量缺口，挑一段素材",
  );
  await expect(result).toContainText('"count": 2');

  // Another kind of step starts from that kind's settings.
  await form.getByLabel("每一项做什么").selectOption("template");
  await expect(
    refField(
      form.getByRole("group", { name: "每一项：文本拼接" }),
      "模板",
    ).getByRole("textbox"),
  ).toHaveValue("{{item.value}}");
});

test("the item is only for the step, and the list must be one variable", async ({
  page,
  request,
}) => {
  const app = await shotsApp(request);
  await page.goto(`/apps/${app}`);
  const nodes = page.getByRole("complementary", { name: "节点列表" });
  await nodes.getByRole("button", { name: /^挑镜头（pick）/ }).click();
  const form = panel(page);
  await refField(form, "对哪个列表逐项处理")
    .getByRole("textbox")
    .fill("镜头：{{plan.shots}}");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(form).toContainText("要插一个变量：对哪个列表逐项处理");
  await refField(form, "对哪个列表逐项处理")
    .getByRole("textbox")
    .fill("{{plan.shots}}");
  await refField(
    form.getByRole("group", { name: "每一项：调用模型" }),
    "提示词",
  )
    .getByRole("textbox")
    .fill("{{item.line}}");
  // {{item.*}} anywhere else is refused.
  await nodes.getByRole("button", { name: /^回答（answer）/ }).click();
  await refField(form, "提示词").getByRole("textbox").fill("{{item.line}}");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(form).toContainText(
    "{{item.*}} 只能写在逐项处理节点的「每一项做什么」里",
  );
  // No node may be called item.
  await refField(form, "提示词").getByRole("textbox").fill("{{pick.results}}");
  await form.getByLabel("标识").fill("item");
  await expect(form).toContainText(
    "env、secret、param、project、conversation、item 不能用",
  );
});
