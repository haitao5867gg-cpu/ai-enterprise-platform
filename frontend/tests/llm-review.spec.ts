import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type { AppDetail, FlowConfig } from "../lib/apps";

// The review node llm_review (docs/api/flow-apps.md, "llm_review 模型审查") in the
// editor, and a project's conventions and cap on model checks (docs/api/projects.md) on
// the project pages.
const id = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });

async function newApp(request: APIRequestContext, app: string) {
  const made = await request.post("/api/apps", {
    data: { app, title: "审查测试", from: { blank: true } },
  });
  expect(made.status()).toBe(201);
  return (await made.json()) as AppDetail;
}

async function choose(page: Page, label: string, target: string) {
  await panel(page)
    .getByRole("group", { name: "下一步" })
    .getByLabel(label, { exact: true })
    .selectOption(target);
}

async function select(page: Page, nodeId: string) {
  await page
    .getByRole("complementary", { name: "节点列表" })
    .getByRole("button", { name: new RegExp(`（${nodeId}）$`) })
    .click();
}

test("the review node is added, checked and saved with a rule and a reference", async ({
  page,
  request,
}) => {
  const app = id("review");
  await newApp(request, app);
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
  await page.goto(`/apps/${app}`);
  await page.getByRole("button", { name: "添加节点", exact: true }).click();
  await page
    .getByRole("dialog", { name: "添加节点" })
    .getByRole("region", { name: "模型" })
    .getByRole("button", { name: "模型审查", exact: true })
    .click();
  await expect(panel(page).getByLabel("没改过来之后")).toHaveValue("pass");
  await expect(panel(page).getByLabel("连续几次没改过来就停")).toHaveValue("2");

  // answer → review; 通过 to the end, 重写 back to the answer.
  await select(page, "answer");
  await choose(page, "下一步", "llm_review");
  await select(page, "llm_review");
  await choose(page, "通过", "end");
  await choose(page, "重写", "answer");

  // Checked before saving: no content, a rule without a title.
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(panel(page)).toContainText("要审查的内容不能为空");
  await expect(panel(page)).toContainText("规则名称不能为空");

  await panel(page)
    .getByLabel("要审查的内容", { exact: true })
    .fill("{{answer.text}}");
  // What people already decided: kept and checked, not reported (5th retest I-20, I-21).
  await panel(page)
    .getByLabel("人已经做的决定", { exact: true })
    .fill("{{answer.text}}");
  // What outranks the review besides people (6th retest I-23, I-24).
  await panel(page)
    .getByLabel("检查结果", { exact: true })
    .fill("{{answer.text}}");
  const rules = panel(page).getByRole("group", {
    name: "审查清单",
    exact: true,
  });
  const first = rules.getByRole("group", { name: "审查清单 1" });
  await first.getByLabel("规则名称").fill("不能承诺退款时间");
  await first.getByLabel("怎么判断").fill("写了具体天数就算承诺");
  await rules.getByRole("button", { name: "添加审查清单" }).click();
  const second = rules.getByRole("group", { name: "审查清单 2" });
  await second.getByLabel("这一项").selectOption("ref");
  // Plain text is not a list of rules.
  await second.getByRole("textbox", { name: "规则的引用" }).fill("不能说脏话");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(panel(page)).toContainText(
    "清单里的文字要引用一组规则，比如 {{project.code_rules}}",
  );
  await second
    .getByRole("textbox", { name: "规则的引用" })
    .fill("{{env.rules}}");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(panel(page)).toContainText("没有常量 rules");
  await second
    .getByRole("textbox", { name: "规则的引用" })
    .fill("{{answer.text}}");
  await panel(page).getByLabel("没改过来之后").selectOption("branch");
  await choose(page, "交给人", "end");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "草稿已保存" }),
  ).toBeVisible();

  const saved = (await (
    await request.get(`/api/apps/${app}`)
  ).json()) as AppDetail;
  const config = saved.draft!.config as FlowConfig;
  expect(
    config.graph.nodes.find((node) => node.id === "llm_review"),
  ).toMatchObject({
    type: "llm_review",
    subject: "{{answer.text}}",
    decided: "{{answer.text}}",
    evidence: "{{answer.text}}",
    rules: [
      {
        key: "rule_1",
        title: "不能承诺退款时间",
        instruction: "写了具体天数就算承诺",
      },
      "{{answer.text}}",
    ],
    max_attempts: 2,
    on_stuck: "branch",
  });
  expect(config.graph.edges).toEqual(
    expect.arrayContaining([
      { from: "answer", to: "llm_review" },
      { from: "llm_review", to: "end", branch: "ok" },
      { from: "llm_review", to: "answer", branch: "retry" },
      { from: "llm_review", to: "end", branch: "stuck" },
    ]),
  );
});

test("a project's conventions and cap on model checks are edited on its page and listed on it", async ({
  page,
}) => {
  const project = `conv-${Date.now().toString(36)}`;
  const made = await page.request.post("/api/projects", {
    data: {
      project_id: project,
      name: `约定测试 ${project}`,
      table_prefix: "cv_",
      environments: [
        { key: "dev", label: "开发", catalog: "main", schema: "cv_dev" },
      ],
    },
  });
  expect(made.status(), await made.text()).toBe(201);
  await page.goto(`/projects/${project}`);
  await expect(page.getByRole("region", { name: "约定" })).toContainText(
    "没有。",
  );

  await page.getByRole("button", { name: "编辑", exact: true }).click();
  const edit = page.getByRole("form", { name: "编辑项目" });
  const list = edit.getByRole("group", { name: "约定", exact: true });
  await list.getByRole("button", { name: "添加约定" }).click();
  await list.getByRole("button", { name: "添加约定" }).click();
  const [one, two] = [1, 2].map((n) =>
    list.getByRole("group", { name: `约定 ${n}` }),
  );
  await one.getByLabel("约定", { exact: true }).fill("读 table1 先按版本筛选");
  await one.getByLabel("什么时候遵守").selectOption("code");
  await two.getByLabel("标识").fill("rule_1");
  await edit.getByRole("button", { name: "保存" }).click();
  // Checked on the page: a repeated key, a convention without words.
  await expect(one).toContainText("约定 rule_1 重复");
  await expect(two).toContainText("请写这条约定");
  await two.getByLabel("标识").fill("ask_open");
  await two
    .getByLabel("约定", { exact: true })
    .fill("资料说没定的事要写成问题");
  await two.getByLabel("补充说明（选填）").fill("不要替业务做决定");
  await two.getByLabel("什么时候遵守").selectOption("plan");
  // Checks the model proposes in a plan run at warning at most.
  await edit.getByLabel("方案里模型建议的检查").selectOption("warn");
  await edit.getByRole("button", { name: "保存" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "已保存" }),
  ).toBeVisible();

  const shown = page.getByRole("region", { name: "约定" });
  await expect(shown).toContainText("读 table1 先按版本筛选");
  await expect(shown).toContainText("写代码时");
  await expect(shown).toContainText("不要替业务做决定");
  const saved = await (
    await page.request.get(`/api/projects/${project}`)
  ).json();
  await expect(page.getByRole("region", { name: "验收检查" })).toContainText(
    "方案里模型建议的检查：最高是警告",
  );
  expect(saved.model_check_max_severity).toBe("warn");
  expect(saved.conventions).toEqual([
    {
      key: "rule_1",
      title: "读 table1 先按版本筛选",
      text: "",
      applies_to: "code",
    },
    {
      key: "ask_open",
      title: "资料说没定的事要写成问题",
      text: "不要替业务做决定",
      applies_to: "plan",
    },
  ]);
});
