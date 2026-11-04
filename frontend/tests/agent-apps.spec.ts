import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type { AgentAppConfig, AppDetail, FlowConfig } from "../lib/apps";

// Agent apps and conversations on the local mock (docs/api/agent-apps.md, "页面").
const id = (prefix: string) =>
  `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
async function detail(
  request: APIRequestContext,
  app: string,
): Promise<AppDetail> {
  const response = await request.get(`/api/apps/${app}`);
  expect(response.status()).toBe(200);
  return response.json();
}
async function create(request: APIRequestContext, app: string, from: object) {
  const response = await request.post("/api/apps", {
    data: { app, title: "报销政策助手", from },
  });
  expect(response.status()).toBe(201);
  return response.json() as Promise<AppDetail>;
}
async function fits(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
}

test("新建智能体应用：先选类型，模板按类型列出", async ({ page, request }) => {
  const app = id("helper");
  await page.goto("/apps");
  await page.getByRole("button", { name: "新建应用", exact: true }).click();
  const kinds = page.getByRole("radiogroup", { name: "应用类型" });
  await expect(kinds.getByRole("radio", { name: /^智能体/ })).toBeChecked();
  const sources = page.getByRole("radiogroup", { name: "应用来源" });
  await expect(sources.getByRole("radio", { name: /通用助手/ })).toBeChecked();
  await expect(
    sources.getByRole("radio", { name: /知识库助手/ }),
  ).toBeVisible();
  await expect(
    sources.getByRole("radio", { name: /意图分类路由/ }),
  ).toHaveCount(0);
  await expect(
    sources.getByRole("radio", { name: /复制已有智能体应用/ }),
  ).toBeVisible();
  await kinds.getByRole("radio", { name: /^编排/ }).check();
  await expect(sources.getByRole("radio", { name: /空白应用/ })).toBeChecked();
  await expect(sources.getByRole("radio", { name: /通用助手/ })).toHaveCount(0);
  await kinds.getByRole("radio", { name: /^智能体/ }).check();
  await page.getByRole("button", { name: "下一步", exact: true }).click();
  await page.getByLabel("标识", { exact: true }).fill(app);
  await page.getByLabel("名称", { exact: true }).fill("报销政策助手");
  await page.getByRole("button", { name: "创建应用", exact: true }).click();
  await expect(page).toHaveURL(`/apps/${app}`);
  await expect(page.getByText(`${app} · 智能体应用`)).toBeVisible();
  const made = await detail(request, app);
  expect(made.kind).toBe("agent");
  expect((made.current.config as AgentAppConfig).agent.instructions).toContain(
    "助手",
  );
  await page.goto("/apps");
  await expect(
    page
      .getByRole("article", { name: "报销政策助手" })
      .first()
      .getByText("智能体应用"),
  ).toBeVisible();
});

test("设计页：表单保存草稿，调试对话用草稿回答并能存为评测用例", async ({
  page,
  request,
}) => {
  const app = id("design");
  await create(request, app, { blank_agent: true });
  await page.goto(`/apps/${app}`);
  await page.getByRole("tab", { name: "设计" }).click();
  const chat = page.getByRole("region", { name: "调试对话" });
  await expect(chat).toContainText("还没有草稿，用当前版本 v1 回答。");
  await expect(chat).toContainText("你好，有什么可以帮你？");
  await page.getByLabel("指令", { exact: true }).fill("你是报销政策助手。");
  await page.getByRole("button", { name: "添加推荐问题" }).click();
  await page
    .getByLabel("推荐问题 1", { exact: true })
    .fill("出差住宿标准是多少？");
  // Unsaved changes block the debug chat until the draft is saved.
  await expect(chat.getByRole("textbox")).toHaveAttribute(
    "placeholder",
    "有未保存的修改，保存草稿后再试",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(chat).toContainText("用已保存的草稿回答");
  const draft = (await detail(request, app)).draft!.config as AgentAppConfig;
  expect(draft.agent.instructions).toBe("你是报销政策助手。");
  expect(draft.chat.suggestions).toEqual(["出差住宿标准是多少？"]);
  await chat.getByRole("button", { name: "出差住宿标准是多少？" }).click();
  await expect(chat).toContainText("（模拟回答）出差住宿标准是多少？");
  await chat.getByRole("textbox").fill("那机票呢？");
  await chat.getByRole("textbox").press("Enter");
  await expect(chat).toContainText("（模拟回答）那机票呢？");
  await chat.getByRole("button", { name: "查看过程" }).first().click();
  await expect(chat).toContainText("这一轮没有调用工具。");
  await chat.getByRole("button", { name: "存为评测用例" }).first().click();
  await expect(chat.getByRole("status")).toContainText("已存为评测用例");
  // The debug chat's conversations stay out of the use page's list.
  const listed = await (
    await request.get(`/api/conversations?app=${app}&draft=false`)
  ).json();
  expect(listed).toEqual([]);
  await page.getByRole("tab", { name: "评测与发布" }).click();
  await expect(page.getByRole("region", { name: "评测集" })).toContainText(
    "出差住宿标准是多少？",
  );
  await fits(page);
});

test("设计页的代码视图：YAML 写错时不能回到表单，改好后保存", async ({
  page,
  request,
}) => {
  const app = id("code");
  await create(request, app, { blank_agent: true });
  await page.goto(`/apps/${app}`);
  await page.getByRole("tab", { name: "设计" }).click();
  await page.getByRole("button", { name: "代码", exact: true }).click();
  const code = page.getByLabel("配置（YAML）");
  await expect(code).toHaveValue(/instructions:/);
  await code.fill("agent: [");
  await expect(page.getByText("YAML 改好之前不能回到表单。")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "表单", exact: true }),
  ).toBeDisabled();
  const config = (await detail(request, app)).current.config as AgentAppConfig;
  config.agent.instructions = "只回答报销问题。";
  config.agent.max_steps = 3;
  await code.fill(JSON.stringify(config, null, 2));
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect
    .poll(
      async () =>
        ((await detail(request, app)).draft?.config as AgentAppConfig)?.agent,
    )
    .toMatchObject({ instructions: "只回答报销问题。", max_steps: 3 });
  await page.getByRole("button", { name: "表单", exact: true }).click();
  await expect(page.getByLabel("指令", { exact: true })).toHaveValue(
    "只回答报销问题。",
  );
});

test("使用页是聊天：会话列表、接着聊、改名、删除，运行页链回对话", async ({
  page,
  request,
}) => {
  const app = id("chat");
  await create(request, app, { template: "agent-blank" });
  await page.goto(`/use/${app}`);
  const main = page.getByRole("region", { name: "对话", exact: true });
  await expect(main).toContainText("你好，有什么可以帮你？");
  await expect(
    page.getByText("还没有对话，在右边发一条消息就开始。"),
  ).toBeVisible();
  await main.getByRole("textbox").fill("差旅报销需要哪些单据？");
  await main.getByRole("button", { name: "发送" }).click();
  await expect(main).toContainText("（模拟回答）差旅报销需要哪些单据？");
  const list = page.getByRole("list", { name: "我的对话" });
  await expect(list).toContainText("差旅报销需要哪些单据？");
  await expect(list).toContainText("1 轮");
  await main.getByRole("textbox").fill("发票丢了怎么办？");
  await main.getByRole("textbox").press("Enter");
  await expect(main).toContainText("（模拟回答）发票丢了怎么办？");
  await expect(list).toContainText("2 轮");
  await main.getByRole("button", { name: "回答得对" }).first().click();
  await expect(
    main.getByRole("button", { name: "回答得对" }).first(),
  ).toHaveAttribute("aria-pressed", "true");
  await fits(page);

  const [conversation] = await (
    await request.get(`/api/conversations?app=${app}&draft=false`)
  ).json();
  const talk = await (
    await request.get(`/api/conversations/${conversation.conversation_id}`)
  ).json();
  expect(talk.turns.map((turn: { message: string }) => turn.message)).toEqual([
    "差旅报销需要哪些单据？",
    "发票丢了怎么办？",
  ]);
  // The run page says which conversation and turn the run is, and links back.
  await page.goto(`/runs/${talk.turns[1].run_id}`);
  await expect(page.locator(".detail-meta")).toContainText(
    "对话「差旅报销需要哪些单据？」第 2 轮",
  );
  await page.getByRole("tab", { name: "详情" }).click();
  await page
    .getByRole("link", { name: "对话「差旅报销需要哪些单据？」第 2 轮" })
    .click();
  await expect(page).toHaveURL(
    `/use/${app}?conversation=${conversation.conversation_id}`,
  );
  await expect(main).toContainText("（模拟回答）发票丢了怎么办？");

  page.once("dialog", (dialog) => dialog.accept("报销单据"));
  await page
    .getByRole("button", { name: "给「差旅报销需要哪些单据？」改名" })
    .click();
  await expect(list).toContainText("报销单据");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除「报销单据」" }).click();
  await expect(
    page.getByText("还没有对话，在右边发一条消息就开始。"),
  ).toBeVisible();
  const cleared = await (
    await request.get(`/api/runs/${talk.turns[0].run_id}`)
  ).json();
  expect(cleared).toMatchObject({ cleared: true, conversation: null });
  await page.goto(`/runs/${talk.turns[0].run_id}`);
  await expect(
    page.getByText("这次运行属于已过期或已删除的对话，内容已清除。"),
  ).toBeVisible();
});

test("入口开关、使用情况和转成编排应用", async ({ page, request }) => {
  const callee = id("callee");
  const caller = id("caller");
  await create(request, callee, { blank_agent: true });
  const made = await create(request, caller, { blank_agent: true });
  const config = made.current.config as AgentAppConfig;
  config.agent.apps = [
    { app: callee, when: "用户问报销政策时", enabled: true },
  ];
  expect(
    (
      await request.put(`/api/apps/${caller}/draft`, { data: { config } })
    ).status(),
  ).toBe(200);
  expect(
    (
      await request.post(`/api/apps/${caller}/draft/publish`, {
        data: { note: "调用政策助手" },
      })
    ).status(),
  ).toBe(200);

  await page.goto(`/apps/${callee}`);
  await page.getByRole("tab", { name: "接入" }).click();
  const entries = page.getByRole("region", { name: "入口" });
  const apps = entries.getByRole("switch", { name: "开放「被其他应用调用」" });
  await expect(apps).toBeChecked();
  await expect(entries).toContainText("在调用它的应用：");
  await expect(
    entries.getByRole("link", { name: "报销政策助手" }),
  ).toBeVisible();
  page.once("dialog", (dialog) => dialog.dismiss());
  await apps.click();
  await expect(apps).toBeChecked();
  page.once("dialog", (dialog) => dialog.accept());
  await apps.click();
  await expect(apps).not.toBeChecked();
  const access = await (await request.get(`/api/apps/${callee}/access`)).json();
  expect(access.apps.enabled).toBe(false);
  expect(access.use.enabled).toBe(true);

  // One conversation turn, then the overview counts it.
  const opened = await (
    await request.post("/api/conversations", { data: { app: callee } })
  ).json();
  await request.post(`/api/conversations/${opened.conversation_id}/messages`, {
    data: { message: "住宿标准？" },
  });
  await page.getByRole("tab", { name: "概览" }).click();
  const usage = page.getByRole("region", { name: "使用情况" });
  await expect(usage.getByRole("group", { name: "总览" })).toContainText(
    "运行数",
  );
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/apps/${callee}/usage?days=7`)).json())
          .totals.runs,
    )
    .toBe(1);
  await usage.getByRole("button", { name: "近 7 天" }).click();
  await expect(usage.getByRole("button", { name: "近 7 天" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(usage).toContainText("会话数");

  const flow = id("as-flow");
  await page.getByRole("button", { name: "转成编排应用" }).click();
  await page.getByLabel("新应用标识").fill(flow);
  await page.getByRole("button", { name: "复制成编排应用" }).click();
  await expect(page).toHaveURL(`/apps/${flow}`);
  const converted = await detail(request, flow);
  expect(converted.kind).toBe("flow");
  expect((converted.current.config as FlowConfig).conversation).toMatchObject({
    enabled: true,
    reply: "reply",
  });
});
