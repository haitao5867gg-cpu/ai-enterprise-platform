import {
  test,
  expect,
  type APIRequestContext,
  type Locator,
  type Page,
} from "@playwright/test";
import { mkdir } from "node:fs/promises";
import type { AppDetail, FlowConfig } from "../lib/apps";

test.describe.configure({ timeout: 90_000 });
const panel = (page: Page) => page.getByRole("region", { name: /^节点设置：/ });
const canvas = (page: Page) =>
  page.getByRole("group", { name: "编排画布", exact: true });
const node = (page: Page, id: string) =>
  canvas(page).locator(`.react-flow__node[data-id="${id}"]`);
const outlet = (page: Page, id: string, branch = "next") =>
  page.getByTestId(`canvas-out-${id}-${branch}`);
const edgePath = (page: Page, from: string, branch = "") =>
  canvas(page).locator(
    `.react-flow__edge[data-id="${from}:${branch}"] .react-flow__edge-path`,
  );

async function nodeRects(page: Page) {
  return canvas(page)
    .locator(".react-flow__node")
    .evaluateAll((nodes) =>
      nodes.map((node) => {
        const { x, y, width, height } = node.getBoundingClientRect();
        return { id: node.getAttribute("data-id")!, x, y, width, height };
      }),
    );
}
async function expectNoOverlap(page: Page) {
  const rects = await nodeRects(page);
  for (let i = 0; i < rects.length; i++)
    for (let j = i + 1; j < rects.length; j++) {
      const a = rects[i],
        b = rects[j];
      expect(
        a.x + a.width <= b.x ||
          b.x + b.width <= a.x ||
          a.y + a.height <= b.y ||
          b.y + b.height <= a.y,
        `${a.id} and ${b.id} overlap`,
      ).toBe(true);
    }
}
async function clickEdge(page: Page, from: string, branch = "") {
  await canvas(page).scrollIntoViewIfNeeded();
  const point = await edgePath(page, from, branch).evaluate((element) => {
    const path = element as SVGPathElement;
    const point = path
      .getPointAtLength(path.getTotalLength() / 2)
      .matrixTransform(path.getScreenCTM()!);
    return { x: point.x, y: point.y };
  });
  await page.mouse.click(point.x, point.y);
  await expect(edgePath(page, from, branch).locator("..")).toHaveClass(
    /selected/,
  );
}
async function expectPathOutsideCards(page: Page, from: string, branch = "") {
  const points = await edgePath(page, from, branch).evaluate((element) => {
    const path = element as SVGPathElement;
    return Array.from({ length: 201 }, (_, i) => {
      const point = path
        .getPointAtLength((path.getTotalLength() * i) / 200)
        .matrixTransform(path.getScreenCTM()!);
      return { x: point.x, y: point.y };
    });
  });
  for (const rect of await nodeRects(page))
    expect(
      points.some(
        (point) =>
          point.x > rect.x + 2 && point.x < rect.x + rect.width - 2 &&
          point.y > rect.y + 2 && point.y < rect.y + rect.height - 2,
      ),
      `${from}:${branch} crosses ${rect.id}`,
    ).toBe(false);
}

async function create(
  request: APIRequestContext,
  page: Page,
  template?: string,
) {
  const app = `canvas-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const response = await request.post("/api/apps", {
    data: {
      app,
      title: "画布编排测试",
      from: template ? { template } : { blank: true },
    },
  });
  expect(response.status()).toBe(201);
  await page.goto(`/apps/${app}`);
  await expect(canvas(page)).toBeVisible();
  await expect(node(page, "start")).toBeVisible();
  return app;
}
async function configOf(request: APIRequestContext, app: string) {
  const data: AppDetail = await (await request.get(`/api/apps/${app}`)).json();
  return (data.draft ?? data.current).config as FlowConfig;
}
async function settle(page: Page) {
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
}
async function fit(page: Page) {
  const close = page.getByRole("button", { name: "关闭节点设置", exact: true });
  if (await close.isVisible()) await close.click();
  await settle(page);
  await page.getByRole("button", { name: "适应画布", exact: true }).click();
  await settle(page);
}
async function middle(locator: Locator) {
  const rect = await locator.boundingBox();
  expect(rect).not.toBeNull();
  return { x: rect!.x + rect!.width / 2, y: rect!.y + rect!.height / 2 };
}
async function drag(
  page: Page,
  from: { x: number; y: number },
  to: { x: number; y: number },
) {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps: 20 });
  await page.mouse.up();
  await settle(page);
}
async function connect(page: Page, from: string, branch: string, to: string) {
  await fit(page);
  await outlet(page, from, branch).scrollIntoViewIfNeeded();
  await drag(
    page,
    await middle(outlet(page, from, branch)),
    await middle(page.getByTestId(`canvas-in-${to}`)),
  );
}
async function createFromOutlet(
  page: Page,
  from: string,
  branch: string,
  label: string,
) {
  await fit(page);
  await canvas(page).scrollIntoViewIfNeeded();
  const blank = await canvas(page).evaluate((element, from) => {
    const rect = element.getBoundingClientRect();
    const source = element
      .querySelector(`.react-flow__node[data-id="${from}"]`)!
      .getBoundingClientRect();
    const occupied = [
      ...element.querySelectorAll(".react-flow__node, .react-flow__minimap"),
    ].map((node) => node.getBoundingClientRect());
    const columns = [
      source.right + 100,
      rect.right - 50,
      ...[0.8, 0.2, 0.65, 0.35].map((x) => rect.left + rect.width * x),
    ];
    for (const x of columns)
      for (const y of [0.3, 0.5, 0.7, 0.15]) {
        const point = {
          x,
          y: rect.top + rect.height * y,
        };
        if (
          point.x < rect.right - 24 &&
          occupied.every(
            (node) =>
              point.x < node.left - 30 ||
              point.x > node.right + 30 ||
              point.y < node.top - 30 ||
              point.y > node.bottom + 30,
          )
        )
          return point;
      }
    throw new Error("No empty canvas position");
  }, from);
  await drag(page, await middle(outlet(page, from, branch)), blank);
  const menu = page.getByRole("dialog", { name: /新建节点并连到/ });
  await expect(menu).toBeVisible();
  await menu.getByRole("button", { name: label, exact: true }).click();
  await expect(panel(page)).toBeVisible();
  await expectNoOverlap(page);
}
async function select(page: Page, id: string) {
  await fit(page);
  await node(page, id).locator(".canvas-node-heading").click();
  await expect(panel(page)).toBeVisible();
}
async function save(page: Page, status = 200) {
  const response = page.waitForResponse(
    (response) =>
      response.url().endsWith("/draft") &&
      response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  expect((await response).status()).toBe(status);
}

test("鼠标搭出意图分类路由，保存的配置与列表版一致（除坐标）", async ({
  page,
  request,
}) => {
  const app = await create(request, page);
  const original = await configOf(request, app);
  await createFromOutlet(page, "start", "next", "意图分类");
  await expect(
    panel(page).getByLabel("要分类的文本", { exact: true }),
  ).toHaveValue("{{start.question}}");
  await connect(page, "classifier", "product", "answer");
  await createFromOutlet(page, "classifier", "after_sales", "调用模型");
  await panel(page).getByLabel("标题", { exact: true }).fill("起草售后回复");
  await panel(page)
    .getByLabel("提示词", { exact: true })
    .fill("售后问题：{{start.question}}");
  await createFromOutlet(page, "llm", "next", "人工");
  await panel(page)
    .getByLabel("给处理人看的内容", { exact: true })
    .fill("{{llm.text}}");
  await connect(page, "human", "approve", "end");
  await connect(page, "human", "reject", "llm");
  await createFromOutlet(page, "classifier", "other", "文本拼接");
  await connect(page, "template", "next", "end");
  await select(page, "end");
  const output = panel(page).getByRole("group", {
    name: "结果字段 1",
    exact: true,
  });
  await output.getByRole("button", { name: "添加取值" }).click();
  await output.getByRole("button", { name: "添加取值" }).click();
  await output
    .getByRole("group", { name: "取值 2", exact: true })
    .getByRole("textbox")
    .fill("{{llm.text}}");
  await output
    .getByRole("group", { name: "取值 3", exact: true })
    .getByRole("textbox")
    .fill("{{template.text}}");
  await save(page);
  const config = await configOf(request, app);
  const withoutPositions = config.graph.nodes.map((item) => {
    const copy = { ...item };
    delete copy.ui;
    return copy;
  });
  expect(withoutPositions).toEqual([
    original.graph.nodes[0],
    {
      id: "classifier",
      type: "classifier",
      title: "意图分类",
      model: null,
      skills: [],
      text: "{{start.question}}",
      instruction: "根据客户问题选择最合适的类别。",
      classes: [
        {
          key: "after_sales",
          label: "售后",
          description: "退换货、维修、投诉",
        },
        {
          key: "product",
          label: "产品咨询",
          description: "产品功能、规格和使用方法",
        },
      ],
    },
    {
      id: "template",
      type: "template",
      title: "文本拼接",
      template: "{{start.question}}",
    },
    {
      id: "llm",
      type: "llm",
      title: "起草售后回复",
      model: null,
      skills: [],
      prompt: "售后问题：{{start.question}}",
      output: { kind: "text" },
    },
    {
      id: "human",
      type: "human",
      title: "人工",
      mode: "approval",
      prompt: "请确认",
      show: "{{llm.text}}",
      options: [
        { key: "approve", label: "确认" },
        { key: "reject", label: "驳回" },
      ],
      comment_required: [],
    },
    original.graph.nodes[1],
    {
      ...original.graph.nodes[2],
      outputs: [
        {
          key: "answer",
          from: ["{{answer.text}}", "{{llm.text}}", "{{template.text}}"],
        },
      ],
    },
  ]);
  expect(config.graph.edges).toHaveLength(9);
  expect(config.graph.edges).toEqual(
    expect.arrayContaining([
      { from: "start", to: "classifier" },
      { from: "classifier", to: "answer", branch: "product" },
      { from: "classifier", to: "llm", branch: "after_sales" },
      { from: "classifier", to: "template", branch: "other" },
      { from: "llm", to: "human" },
      { from: "human", to: "end", branch: "approve" },
      { from: "human", to: "llm", branch: "reject" },
      { from: "template", to: "end" },
      { from: "answer", to: "end" },
    ]),
  );
  expect(config.env).toEqual(original.env);
  await expect(
    canvas(page).getByRole("button", {
      name: "意图分类 → 回答，产品咨询",
      exact: true,
    }),
  ).toBeAttached();
});

test("拖动节点只保存该节点坐标，刷新仍使用保存位置", async ({
  page,
  request,
}) => {
  const app = await create(request, page);
  await fit(page);
  await canvas(page).scrollIntoViewIfNeeded();
  const start = await middle(
    node(page, "answer").locator(".canvas-node-heading"),
  );
  await drag(page, start, { x: start.x + 90, y: start.y + 45 });
  await expect(page.getByText("有未保存的修改", { exact: true })).toBeVisible();
  await save(page);
  const config = await configOf(request, app);
  const position = config.graph.nodes.find((node) => node.id === "answer")!.ui;
  expect(position).toEqual({ x: expect.any(Number), y: expect.any(Number) });
  expect(config.graph.nodes.filter((node) => node.ui)).toHaveLength(1);
  await page.reload();
  await expect(node(page, "answer")).toBeVisible();
  const rendered = await node(page, "answer").evaluate((element) => {
    const matrix = new DOMMatrix(getComputedStyle(element).transform);
    return { x: matrix.e, y: matrix.f };
  });
  // CSS serializes matrices to fewer decimal places than the saved JSON.
  expect(rendered.x).toBeCloseTo(position!.x, 2);
  expect(rendered.y).toBeCloseTo(position!.y, 2);
  expect(
    (await configOf(request, app)).graph.nodes.find(
      (node) => node.id === "answer",
    )!.ui,
  ).toEqual(position);
});

test("无坐标模板自动排布不重叠，自动整理才写入全部坐标", async ({
  page,
  request,
}) => {
  const app = await create(request, page, "intent-routing");
  const original = await configOf(request, app);
  expect(original.graph.nodes.every((node) => !node.ui)).toBe(true);
  await expect(page.getByText("有未保存的修改", { exact: true })).toHaveCount(
    0,
  );
  await fit(page);
  const rects = await nodeRects(page);
  expect(rects.length).toBe(7);
  await expectNoOverlap(page);
  const byId = Object.fromEntries(rects.map((rect) => [rect.id, rect]));
  expect(byId.draft.y).toBeLessThan(byId.answer.y);
  expect(byId.answer.y).toBeLessThan(byId.handoff.y);
  for (const edge of original.graph.edges) {
    const path = edgePath(page, edge.from, edge.branch ?? "");
    if (edge.from === "review" && edge.branch === "reject") {
      await expect(path).toHaveAttribute("d", /Q/);
      await expectPathOutsideCards(page, edge.from, edge.branch);
      continue;
    }
    const source = rects.find((rect) => rect.id === edge.from)!;
    const target = rects.find((rect) => rect.id === edge.to)!;
    expect(target.x, `${edge.from} → ${edge.to}`).toBeGreaterThan(source.x);
    await expect(path).toHaveAttribute("d", /C/);
  }
  const labels = canvas(page).locator(".canvas-edge-label");
  await expect(labels.getByText("下一步", { exact: true })).toHaveCount(0);
  for (const label of ["售后", "产品咨询", "都不是", "确认回复", "驳回重写"])
    await expect(labels.getByText(label, { exact: true })).toBeVisible();
  const inlet = await middle(page.getByTestId("canvas-in-route"));
  const heading = await middle(node(page, "route").locator(".canvas-node-heading"));
  expect(Math.abs(inlet.x - byId.route.x)).toBeLessThan(2);
  expect(Math.abs(inlet.y - heading.y)).toBeLessThan(2);
  // Persisted edge order must not override the classifier's outlet order.
  const positions = await canvas(page)
    .locator(".react-flow__node")
    .evaluateAll((nodes) =>
      nodes.map((node) => ({
        id: node.getAttribute("data-id")!,
        transform: (node as HTMLElement).style.transform,
      })),
    );
  const reordered = {
    ...original,
    graph: { ...original.graph, edges: [...original.graph.edges].reverse() },
  };
  const response = await request.put(`/api/apps/${app}/draft`, {
    data: { config: reordered },
  });
  expect(response.ok()).toBe(true);
  await page.reload();
  await fit(page);
  for (const position of positions)
    await expect(node(page, position.id)).toHaveAttribute(
      "style",
      expect.stringContaining(position.transform),
    );
  await page.getByRole("button", { name: "自动整理", exact: true }).click();
  await expect(page.getByText("有未保存的修改", { exact: true })).toBeVisible();
  await save(page);
  expect(
    (await configOf(request, app)).graph.nodes.every(
      (node) => Number.isFinite(node.ui?.x) && Number.isFinite(node.ui?.y),
    ),
  ).toBe(true);
  if (process.env.AEP_CAPTURE_FLOW_SCREENSHOTS === "1") {
    await page.setViewportSize({ width: 1920, height: 1080 });
    await fit(page);
    const directory =
      process.env.AEP_FLOW_SCREENSHOT_DIR ?? "docs/screenshots/issue-72";
    await mkdir(directory, { recursive: true });
    await page.getByRole("region", { name: "编排", exact: true }).screenshot({
      path: `${directory}/flow-canvas.png`,
      animations: "disabled",
    });
  }
});

test("普通连线没有标签，点击线条后可用 Delete 和中点按钮删除", async ({
  page,
  request,
}) => {
  await create(request, page);
  await fit(page);
  const edge = canvas(page).getByRole("button", {
    name: "开始 → 回答，下一步",
    exact: true,
  });
  await expect(edge).toBeAttached();
  await expect(canvas(page).locator(".canvas-edge-label")).toHaveCount(0);
  await clickEdge(page, "start");
  const remove = page.getByRole("button", { name: "删除选中连线", exact: true });
  await expect(remove).toBeVisible();
  await expect(remove.locator("..")).toHaveText("×");
  const midpoint = await middle(remove);
  const pathMidpoint = await edgePath(page, "start").evaluate((element) => {
    const path = element as SVGPathElement;
    const point = path
      .getPointAtLength(path.getTotalLength() / 2)
      .matrixTransform(path.getScreenCTM()!);
    return { x: point.x, y: point.y };
  });
  expect(midpoint.x).toBeCloseTo(pathMidpoint.x, 0);
  expect(midpoint.y).toBeCloseTo(pathMidpoint.y, 0);
  await page.keyboard.press("Delete");
  await expect(edge).toHaveCount(0);
  await expect(node(page, "start")).toBeVisible();
  await expect(node(page, "answer")).toBeVisible();
  await connect(page, "start", "next", "answer");
  await clickEdge(page, "start");
  await remove.click();
  await expect(edge).toHaveCount(0);
});

test("已保存的纵向坐标保持不变，自动整理才改为横向", async ({
  page,
  request,
}) => {
  const app = await create(request, page);
  const config = await configOf(request, app);
  config.graph.nodes.forEach((node, index) => {
    node.ui = { x: 48, y: 48 + index * 200 };
  });
  const response = await request.put(`/api/apps/${app}/draft`, { data: { config } });
  expect(response.ok()).toBe(true);
  await page.reload();
  await fit(page);
  for (const item of config.graph.nodes) {
    const position = await node(page, item.id).evaluate((element) => {
      const matrix = new DOMMatrix(getComputedStyle(element).transform);
      return { x: matrix.e, y: matrix.f };
    });
    expect(position).toEqual(item.ui);
  }
  await expect(page.getByText("有未保存的修改", { exact: true })).toHaveCount(0);
  await expect(edgePath(page, "answer")).toHaveAttribute("d", /Q/);
  await expectPathOutsideCards(page, "answer");
  await page.getByRole("button", { name: "自动整理", exact: true }).click();
  await fit(page);
  await expectNoOverlap(page);
  const rects = await nodeRects(page);
  const byId = Object.fromEntries(rects.map((rect) => [rect.id, rect]));
  expect(byId.answer.x).toBeGreaterThan(byId.start.x);
  expect(byId.end.x).toBeGreaterThan(byId.answer.x);
  await save(page);
});

test("删除连线后 422 定位到分支下拉和红色节点，支持连线删除按钮", async ({
  page,
  request,
}) => {
  await create(request, page, "intent-routing");
  await fit(page);
  const edge = canvas(page).getByRole("button", {
    name: "意图分类 → 回答产品问题，产品咨询",
    exact: true,
  });
  await edge.focus();
  await edge.press("Enter");
  await page.keyboard.press("Delete");
  await expect(edge).toHaveCount(0);
  await save(page, 422);
  await expect(node(page, "route")).toHaveAccessibleName(
    "意图分类（route），有问题",
  );
  await expect(node(page, "route").locator(".canvas-node-card")).toHaveClass(
    /invalid/,
  );
  await select(page, "route");
  const field = panel(page).getByLabel("产品咨询", { exact: true });
  await expect(field).toHaveValue("");
  await expect(field).toHaveAttribute("aria-invalid", "true");
  await expect(field.locator("..").locator(".field-error")).toHaveText(
    "每个分支须连接一条出边",
  );
  await field.selectOption("answer");
  await fit(page);
  await edge.focus();
  await edge.press("Enter");
  await page.getByRole("button", { name: "删除选中连线", exact: true }).click();
  await expect(edge).toHaveCount(0);
});

test("删除节点列出影响，输入框 Backspace 不删节点，复制坐标偏移 40", async ({
  page,
  request,
}) => {
  await create(request, page);
  await select(page, "answer");
  await node(page, "answer").focus();
  await page.keyboard.press("Delete");
  const confirm = page.getByRole("dialog", {
    name: "删除节点「回答」",
    exact: true,
  });
  await expect(confirm).toContainText("开始 → 回答");
  await expect(confirm).toContainText("回答 → 结束");
  await expect(confirm).toContainText("结束 · outputs.0.from.0");
  await confirm.getByRole("button", { name: "取消", exact: true }).click();
  const title = panel(page).getByLabel("标题", { exact: true });
  await title.fill("回答一");
  await title.press("Backspace");
  await expect(title).toHaveValue("回答");
  await expect(confirm).toHaveCount(0);
  await expect(node(page, "answer")).toBeVisible();
  const sourcePosition = await node(page, "answer").evaluate(
    (element) => element.style.transform,
  );
  await panel(page).getByRole("button", { name: "复制", exact: true }).click();
  await expect(panel(page)).toHaveAccessibleName("节点设置：回答（副本）");
  const copyPosition = await node(page, "answer_2").evaluate(
    (element) => element.style.transform,
  );
  const coordinates = (value: string) =>
    value
      .match(/translate\(([-\d.]+)px,\s*([-\d.]+)px\)/)!
      .slice(1)
      .map(Number);
  expect(coordinates(copyPosition)).toEqual(
    coordinates(sourcePosition).map((value) => value + 40),
  );
  await node(page, "answer_2").focus();
  await page.keyboard.press("Backspace");
  await page
    .getByRole("dialog", { name: "删除节点「回答（副本）」", exact: true })
    .getByRole("button", { name: "确认删除", exact: true })
    .click();
  await expect(node(page, "answer_2")).toHaveCount(0);
});

test("列表和画布共用设置、记住选择，390px 手机只有列表且不横向滚动", async ({
  page,
  request,
}) => {
  const app = await create(request, page);
  await select(page, "answer");
  await panel(page).getByLabel("标题", { exact: true }).fill("画布中的回答");
  await page.getByRole("button", { name: "列表", exact: true }).click();
  await expect(
    page
      .getByRole("complementary", { name: "节点列表" })
      .getByRole("button", { name: "画布中的回答（answer）", exact: true }),
  ).toBeVisible();
  await panel(page).getByLabel("标题", { exact: true }).fill("列表中的回答");
  await panel(page)
    .getByRole("group", { name: "下一步", exact: true })
    .getByRole("combobox")
    .selectOption("answer");
  await page.getByRole("button", { name: "画布", exact: true }).click();
  await expect(node(page, "answer")).toHaveAccessibleName(
    "列表中的回答（answer）",
  );
  await expect(
    canvas(page).getByRole("button", {
      name: "列表中的回答 → 列表中的回答，下一步",
      exact: true,
    }),
  ).toBeAttached();
  await fit(page);
  await expectPathOutsideCards(page, "answer");
  await select(page, "answer");
  await panel(page)
    .getByRole("group", { name: "下一步", exact: true })
    .getByRole("combobox")
    .selectOption("end");
  await save(page);
  await page.getByRole("button", { name: "列表", exact: true }).click();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "列表", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(canvas(page)).toHaveCount(0);
  await page.getByRole("button", { name: "画布", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("group", { name: "编排视图", exact: true }),
  ).toHaveCount(0);
  await expect(canvas(page)).toHaveCount(0);
  await expect(
    page.getByRole("complementary", { name: "节点列表" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 900, height: 960 });
  await expect(canvas(page)).toBeVisible();
  expect(
    (await configOf(request, app)).graph.nodes.find(
      (node) => node.id === "answer",
    )!.title,
  ).toBe("列表中的回答");
});

test("localStorage 不可读写时仍默认画布并能切换", async ({ page, request }) => {
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => {
      throw new DOMException("Unavailable", "SecurityError");
    };
    Storage.prototype.setItem = () => {
      throw new DOMException("Unavailable", "SecurityError");
    };
  });
  await create(request, page);
  await page.getByRole("button", { name: "列表", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "节点列表" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "画布", exact: true }).click();
  await expect(canvas(page)).toBeVisible();
});

test("分支设置同步出口，允许拖到节点卡片并拒绝连到开始", async ({
  page,
  request,
}) => {
  const app = await create(request, page, "intent-routing");
  await select(page, "route");
  const product = panel(page).getByRole("group", {
    name: "类别 2",
    exact: true,
  });
  await product.getByLabel("名称", { exact: true }).fill("产品问题");
  await product.getByLabel("标识", { exact: true }).fill("products");
  await expect(outlet(page, "route", "products")).toHaveAttribute(
    "aria-label",
    "意图分类的产品问题出口",
  );
  await expect(
    canvas(page).getByRole("button", {
      name: "意图分类 → 回答产品问题，产品问题",
      exact: true,
    }),
  ).toBeAttached();
  await panel(page)
    .getByRole("group", { name: "出错时", exact: true })
    .getByLabel("重试后仍失败", { exact: true })
    .selectOption("branch");
  await expect(outlet(page, "route", "error")).toBeVisible();
  await fit(page);
  await canvas(page).scrollIntoViewIfNeeded();
  await drag(
    page,
    await middle(outlet(page, "route", "products")),
    await middle(node(page, "start")),
  );
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    canvas(page).getByRole("button", {
      name: "意图分类 → 回答产品问题，产品问题",
      exact: true,
    }),
  ).toBeAttached();
  await fit(page);
  await canvas(page).scrollIntoViewIfNeeded();
  await drag(
    page,
    await middle(outlet(page, "route", "error")),
    await middle(node(page, "handoff").locator(".canvas-node-heading")),
  );
  await expect(
    canvas(page).getByRole("button", {
      name: "意图分类 → 转人工客服，出错",
      exact: true,
    }),
  ).toBeAttached();
  await save(page);
  const config = await configOf(request, app);
  expect(config.graph.edges).toContainEqual({
    from: "route",
    to: "handoff",
    branch: "error",
  });
  expect(config.graph.edges.some((edge) => edge.to === "start")).toBe(false);
});

test("工具栏按分类添加节点，从视口中心落点避开已有节点", async ({ page, request }) => {
  const app = await create(request, page);
  await fit(page);
  const center = await canvas(page).evaluate((element) => {
    const viewport = element.querySelector(".react-flow__viewport")!;
    const matrix = new DOMMatrix(getComputedStyle(viewport).transform);
    const rect = element.getBoundingClientRect();
    return {
      x: (rect.width / 2 - matrix.e) / matrix.a,
      y: (rect.height / 2 - matrix.f) / matrix.a,
    };
  });
  await page
    .getByRole("toolbar", { name: "画布工具" })
    .getByRole("button", { name: "添加节点", exact: true })
    .click();
  const menu = page.getByRole("dialog", { name: "添加节点", exact: true });
  await expect(
    menu.getByRole("region", { name: "模型", exact: true }),
  ).toBeVisible();
  await expect(
    menu.getByRole("button", { name: "开始", exact: true }),
  ).toBeDisabled();
  await menu
    .getByRole("region", { name: "逻辑", exact: true })
    .getByRole("button", { name: "文本拼接", exact: true })
    .click();
  await expect(panel(page)).toHaveAccessibleName("节点设置：文本拼接");
  await expectNoOverlap(page);
  await connect(page, "start", "next", "template");
  await connect(page, "template", "next", "answer");
  await save(page);
  const created = (await configOf(request, app)).graph.nodes.find(
    (node) => node.id === "template",
  )!;
  expect(created.ui!.x + 232 / 2).toBeCloseTo(center.x, 2);
  expect(created.ui!.y + 116 / 2).toBeGreaterThanOrEqual(center.y - 0.01);
});
