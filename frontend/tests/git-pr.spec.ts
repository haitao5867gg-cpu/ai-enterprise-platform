import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { runTab } from "./run-page";
import type { AppDetail, FlowConfig } from "../lib/apps";

// docs/api/git.md: code repositories on the data sources page and the 提交 PR node.
const id = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });

async function newApp(request: APIRequestContext, app: string) {
  const made = await request.post("/api/apps", {
    data: { app, title: "提交测试", from: { blank: true } },
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

test("code repositories have their own section and are checked for the Git tools", async ({
  page,
}) => {
  await page.goto("/datasources");
  const section = page.getByRole("region", { name: "代码仓库" });
  await expect(section.getByRole("row", { name: /资产仓库/ })).toBeVisible();
  // Not among the data sources.
  await expect(
    page
      .getByRole("region", { name: "数据源", exact: true })
      .getByRole("row", { name: /资产仓库/ }),
  ).toHaveCount(0);

  await section.getByRole("button", { name: "登记代码仓库" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("类型")).toHaveValue("git");
  await expect(dialog).toContainText("只用在编排应用的「提交 PR」节点里");
  await dialog.getByLabel("名称").fill(id("仓库"));
  await dialog
    .getByLabel("连接器地址", { exact: true })
    .fill("http://tools.connector.test:8010/mcp");
  await dialog.getByRole("button", { name: "测试连接" }).click();
  await expect(dialog).toContainText(
    "缺少代码仓库需要的工具：list_repos、list_files",
  );
  await dialog
    .getByLabel("连接器地址", { exact: true })
    .fill("http://git.connector.test:8006/mcp");
  await dialog.getByRole("button", { name: "测试连接" }).click();
  await expect(dialog).toContainText("open_pull_request");
});

test("the 提交 PR node picks a repository, is saved and its run links the PR", async ({
  page,
  request,
}) => {
  const app = id("ship");
  await newApp(request, app);
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
  await page.goto(`/apps/${app}`);
  await page.getByRole("button", { name: "添加节点", exact: true }).click();
  await page
    .getByRole("dialog", { name: "添加节点" })
    .getByRole("region", { name: "工具" })
    .getByRole("button", { name: "提交 PR", exact: true })
    .click();
  // It opens a real pull request: no trying it alone.
  await expect(
    panel(page).getByRole("heading", { name: "试跑这个节点" }),
  ).toHaveCount(0);

  await select(page, "answer");
  await choose(page, "下一步", "git_pr");
  await select(page, "git_pr");
  await choose(page, "下一步", "end");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(panel(page)).toContainText("要选择代码仓库");

  // Only code repositories are offered; their repositories come from the connector.
  const source = panel(page).getByLabel("代码仓库", { exact: true });
  await expect(source.locator("option", { hasText: "示例库" })).toHaveCount(0);
  await source.selectOption("assets-repo");
  // A text box until the connector lists the repositories, then a list.
  await panel(page)
    .getByRole("combobox", { name: "仓库", exact: true })
    .selectOption("acme/data-pipelines");
  await panel(page)
    .getByLabel("要提交的文件", { exact: true })
    .fill("{{answer.text}}");
  await panel(page)
    .getByLabel("放在哪个目录", { exact: true })
    .fill("pipelines/demo");
  await panel(page)
    .getByLabel("用这些文件替换整个目录（删掉目录里没有提交的旧文件）")
    .check();
  await panel(page)
    .getByLabel("PR 标题", { exact: true })
    .fill("新增 {{start.question}}");
  await panel(page)
    .getByLabel("提交信息", { exact: true })
    .fill("DATA-7 新增管道");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "草稿已保存" }),
  ).toBeVisible();
  const saved = (await (
    await request.get(`/api/apps/${app}`)
  ).json()) as AppDetail;
  const config = saved.draft!.config as FlowConfig;
  expect(config.graph.nodes.find((node) => node.id === "git_pr")).toMatchObject(
    {
      type: "git_pr",
      datasource: "assets-repo",
      repo: "acme/data-pipelines",
      files: "{{answer.text}}",
      directory: "pipelines/demo",
      replace_directory: true,
      pr_title: "新增 {{start.question}}",
      message: "DATA-7 新增管道",
    },
  );

  // A tool node may not use a code repository.
  const tool = structuredClone(config);
  tool.graph.nodes = tool.graph.nodes.map((node) =>
    node.id === "answer"
      ? {
          id: "answer",
          type: "tool",
          title: "读仓库",
          datasource: "assets-repo",
          tool: "read_file",
          args: {},
        }
      : node.id === "git_pr"
        ? { ...node, files: "{{answer.result}}" }
        : node,
  );
  const toolEnd = tool.graph.nodes.find((node) => node.type === "end");
  if (toolEnd?.type === "end")
    toolEnd.outputs = [{ key: "pr", from: ["{{git_pr.url}}"] }];
  const refused = await request.put(`/api/apps/${app}/draft`, {
    data: { config: tool },
  });
  expect(refused.status()).toBe(422);
  expect((await refused.json()).detail).toContainEqual({
    loc: ["body", "config", "graph", "nodes", "answer", "datasource"],
    msg: "「资产仓库」是代码仓库，只能用在提交 PR 节点里",
  });

  // Run with files from a code node: the run page links the pull request.
  const run = structuredClone(config);
  run.graph.nodes = run.graph.nodes.map((node) =>
    node.id === "answer"
      ? {
          id: "answer",
          type: "code",
          title: "写代码",
          code: "def main(inputs):\n    return inputs",
          inputs: { files: { "src/job.py": "print(1)\n" } },
          outputs: [{ key: "files", type: "object" }],
          timeout_seconds: 10,
        }
      : node.id === "git_pr"
        ? { ...node, files: "{{answer.files}}" }
        : node,
  );
  const end = run.graph.nodes.find((node) => node.type === "end");
  if (end?.type === "end")
    end.outputs = [{ key: "pr", from: ["{{git_pr.url}}"] }];
  expect(
    (
      await request.put(`/api/apps/${app}/draft`, { data: { config: run } })
    ).status(),
  ).toBe(200);
  const made = await request.post("/api/runs", {
    data: { app, input: { question: "订单" }, draft: true },
  });
  expect(made.status()).toBe(201);
  const { run_id } = await made.json();
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/runs/${run_id}`)).json()).status,
    )
    .toBe("done");
  await page.goto(`/runs/${run_id}`);
  await runTab(page, "过程");
  await page
    .getByRole("region", { name: "工作流程" })
    .getByRole("list", { name: "本次运行的步骤" })
    .getByRole("button", { name: /提交 PR/ })
    .click();
  const shown = page.getByRole("group", { name: "提交的 PR" });
  await expect(shown).toContainText(
    "已在 acme/data-pipelines 开 PR #1：https://github.com/acme/data-pipelines/pull/1（改动 1 个文件）",
  );
  await expect(
    shown.getByRole("link", { name: "打开 PR #1 ↗" }),
  ).toHaveAttribute("href", "https://github.com/acme/data-pipelines/pull/1");
});
