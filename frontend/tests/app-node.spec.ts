import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { runTab } from "./run-page";
import type { AppDetail, FlowConfig } from "../lib/apps";

// docs/api/flow-apps.md, "app 调用应用": one flow app calls another as a step.
const id = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });

async function newApp(request: APIRequestContext, app: string, title: string) {
  const made = await request.post("/api/apps", {
    data: { app, title, from: { blank: true } },
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

test("an app node is set up in the editor and its run links the called run", async ({
  page,
  request,
}) => {
  const callee = id("callee");
  const front = id("front");
  await newApp(request, callee, "查订单");
  await newApp(request, front, "前台");
  await page.addInitScript(() =>
    localStorage.setItem("aep.flow-editor.view", "list"),
  );
  await page.goto(`/apps/${front}`);
  await page.getByRole("button", { name: "添加节点", exact: true }).click();
  await page
    .getByRole("dialog", { name: "添加节点" })
    .getByRole("region", { name: "工具" })
    .getByRole("button", { name: "调用应用", exact: true })
    .click();
  // It starts a real run: no trying it alone.
  await expect(
    panel(page).getByRole("heading", { name: "试跑这个节点" }),
  ).toHaveCount(0);

  // Saving without a called app says what is missing.
  await select(page, "answer");
  await choose(page, "下一步", "app");
  await select(page, "app");
  await choose(page, "下一步", "end");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(panel(page)).toContainText("要选择被调用的应用");

  // Code apps are not offered; the called app's inputs and results are.
  const which = panel(page).getByLabel("调用哪个应用");
  await expect(which.locator("option", { hasText: "（etl）" })).toHaveCount(0);
  await which.selectOption(callee);
  await panel(page)
    .getByLabel("输入：客户问题（必填）", { exact: true })
    .fill("订单 {{start.question}}");
  const results = panel(page).getByRole("group", { name: "取出的结果字段" });
  await results.getByLabel("answer", { exact: true }).check();
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "草稿已保存" }),
  ).toBeVisible();

  const saved = (await (
    await request.get(`/api/apps/${front}`)
  ).json()) as AppDetail;
  const config = saved.draft!.config as FlowConfig;
  expect(config.graph.nodes.find((node) => node.id === "app")).toMatchObject({
    type: "app",
    app: callee,
    input: { question: "订单 {{start.question}}" },
    outputs: ["answer"],
    timeout_seconds: 600,
  });

  const run = await request.post("/api/runs", {
    data: { app: front, input: { question: "42" }, draft: true },
  });
  expect(run.status()).toBe(201);
  const { run_id } = await run.json();
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/runs/${run_id}`)).json()).status,
    )
    .toBe("done");
  await page.goto(`/runs/${run_id}`);
  await runTab(page, "过程");
  await page.getByText(/^运行过程 · \d+ 条消息$/).click();
  await expect(
    page.getByText(/调用「查订单」：运行 \w{8} 已完成。/),
  ).toBeVisible();
  await page
    .getByRole("region", { name: "工作流程" })
    .getByRole("list", { name: "本次运行的步骤" })
    .getByRole("button", { name: /调用应用/ })
    .click();
  const link = page
    .getByRole("group", { name: "调用的运行" })
    .getByRole("link", { name: "查看被调用的运行" });
  await expect(link).toBeVisible();
  await link.click();
  await expect(page.locator(".detail-meta")).toContainText("前台");
});

test("an app that waits for a person cannot be called", async ({ request }) => {
  const front = id("front");
  const created = await newApp(request, front, "前台");
  const config = structuredClone(created.current.config) as FlowConfig;
  config.graph.nodes.splice(1, 1, {
    id: "call",
    type: "app",
    title: "问一下",
    app: "hello",
    input: { text: "{{start.question}}" },
    outputs: ["answer"],
  });
  const end = config.graph.nodes.at(-1)!;
  if (end.type === "end")
    end.outputs = [{ key: "answer", from: ["{{call.answer}}"] }];
  config.graph.edges = [
    { from: "start", to: "call" },
    { from: "call", to: "end" },
  ];
  const saved = await request.put(`/api/apps/${front}/draft`, {
    data: { config },
  });
  expect(saved.status()).toBe(200);
  const made = await request.post("/api/runs", {
    data: { app: front, input: { question: "42" }, draft: true },
  });
  const { run_id } = await made.json();
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/runs/${run_id}`)).json()).status,
    )
    .toBe("error");
  const run = await (await request.get(`/api/runs/${run_id}`)).json();
  expect(run.error).toContain(
    "要人工处理，不能被调用；人工确认请放在调用它的应用里",
  );
});
