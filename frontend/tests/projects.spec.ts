import { test, expect, type APIRequestContext } from "@playwright/test";
import type { FlowConfig } from "../lib/apps";

// Projects (docs/api/projects.md): the pages, choosing one for a new run, and what the
// run page shows. The mock keeps its state for the whole dev server, so names are unique.
test.describe.configure({ timeout: 60_000 });

const unique = (prefix: string) =>
  `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;

async function boundApp(
  request: APIRequestContext,
  project: "required" | "optional",
) {
  const app = unique("bound");
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title: "绑定项目的应用", from: { blank: true } },
      })
    ).status(),
  ).toBe(201);
  const config: FlowConfig = (
    await (await request.get(`/api/apps/${app}`)).json()
  ).current.config;
  const start = config.graph.nodes.find((node) => node.type === "start")!;
  if (start.type === "start") start.project = project;
  const answer = config.graph.nodes.find((node) => node.type === "llm")!;
  if (answer.type === "llm")
    answer.prompt =
      "统计月 {{param.update_month}}，写到 {{project.catalog}}.{{project.schema}}\n{{start.question}}";
  const saved = await request.put(`/api/apps/${app}/draft`, {
    data: { config },
  });
  expect(saved.status(), await saved.text()).toBe(200);
  expect(
    (
      await request.post(`/api/apps/${app}/draft/publish`, {
        data: { note: "用项目" },
      })
    ).status(),
  ).toBe(200);
  return app;
}

test("项目页面：新建时先在页面上查出问题，保存后能查看、编辑和删除", async ({
  page,
}) => {
  await page.goto("/projects");
  await expect(
    page.getByRole("link", { name: "项目", exact: true }),
  ).toHaveClass(/active/);
  const list = page.getByRole("table", { name: "项目" });
  await expect(list.getByRole("link", { name: "销售预测" })).toBeVisible();

  await page.getByRole("link", { name: "新建项目" }).click();
  await expect(page).toHaveURL(/\/projects\/new$/);
  const form = page.getByRole("form", { name: "新建项目" });
  const id = unique("pt");
  await form.getByLabel("项目标识").fill(id);
  await form.getByLabel("名称", { exact: true }).fill(`页面测试 ${id}`);
  await form.getByLabel("表名前缀").fill("pt_");

  // The environments start as dev and prod without a place to write: saving says so.
  await form.getByRole("button", { name: "建立项目" }).click();
  await expect(form.getByRole("alert")).toContainText("还有没填好的地方");
  const dev = form.getByRole("group", { name: "环境 1" });
  const prod = form.getByRole("group", { name: "环境 2" });
  await expect(dev).toContainText("请填写 catalog");
  for (const [group, schema] of [
    [dev, "pt_dev"],
    [prod, "pt"],
  ] as const) {
    await group.getByLabel("catalog").fill("main");
    await group.getByLabel("schema").fill(schema);
  }
  await expect(dev).not.toContainText("请填写 catalog");

  await form.getByRole("button", { name: "添加引用表" }).click();
  const ref = form.getByRole("group", { name: "引用表 1" });
  await ref
    .getByLabel("表名（catalog.schema.table）")
    .fill("prev.sales.forecast");
  await ref.getByLabel("读取筛选（选填）").fill("Type = 'Vol.'; drop");
  await expect(ref).toContainText("读取筛选不能有分号");
  await ref.getByLabel("读取筛选（选填）").fill("Type = 'Vol.'");
  await expect(ref).not.toContainText("读取筛选不能有分号");
  await form.getByLabel(/归本项目的表/).fill("other_table");
  await expect(form).toContainText("other_table 要以表名前缀 pt_ 开头");
  await form.getByLabel(/归本项目的表/).fill("pt_history");

  await form.getByRole("button", { name: "添加运行参数" }).click();
  const param = form.getByRole("group", { name: "运行参数 1" });
  await param.getByLabel("标识").fill("update_month");
  await param.getByLabel("显示名").fill("统计月");
  await param.getByLabel("类型").selectOption({ label: "月份" });
  await param.getByLabel("必填").check();
  // A text parameter may have a format; its default must fit it.
  await form.getByRole("button", { name: "添加运行参数" }).click();
  const version = form.getByRole("group", { name: "运行参数 2" });
  await version.getByLabel("标识").fill("version_id");
  await version.getByLabel("显示名").fill("版本");
  await version.getByLabel("格式（选填）").fill("FY\\d{2}_\\d{2}_M");
  await version.getByLabel("默认值（选填）").fill("FY26-06");
  await expect(version).toContainText(
    "默认值不对：参数「版本」要符合格式 FY\\d{2}_\\d{2}_M",
  );
  await version.getByLabel("默认值（选填）").fill("FY26_06_M");
  await expect(version).not.toContainText("默认值不对");
  // How the scheduled runs of a released job fill it in (jadeflow retest I-19).
  await version
    .getByLabel("上线后定时运行时")
    .selectOption({ label: "按运行那天推算日期" });
  await expect(version.getByLabel("推算到哪个月")).toHaveValue("-1");
  await expect(version).toContainText(
    "定时运行时的取法不对：文本参数要写日期格式，如 FY%y_%m_M",
  );
  await version.getByLabel("日期格式").fill("FY%y_%B");
  await expect(version).toContainText("日期格式只能用 %Y（四位年份）");
  await version.getByLabel("日期格式").fill("FY%y_%m_M");
  await expect(version).not.toContainText("定时运行时的取法不对");
  await form.getByLabel("发给模型的数据").selectOption({ label: "只发统计" });
  await expect(form).toContainText("不发样本行和取值分布");

  // Acceptance checks are written as JSON; repeated ids show before saving.
  const check = {
    id: "rows_this_month",
    label: "本月有结果",
    kind: "aggregate",
    table: "pt_history",
    expr: "count(*)",
    op: "gt",
    value: 0,
  };
  const checks = form.getByLabel("检查（JSON）");
  await checks.fill(JSON.stringify([check, check]));
  await expect(form).toContainText("验收检查 id rows_this_month 重复");
  await checks.fill(JSON.stringify([check]));
  await expect(form).not.toContainText("验收检查 id rows_this_month 重复");

  await form.getByRole("button", { name: "建立项目" }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${id}$`));
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    `页面测试 ${id}`,
  );
  await expect(page.getByRole("region", { name: "环境" })).toContainText(
    "main.pt_dev",
  );
  await expect(page.getByRole("region", { name: "表登记" })).toContainText(
    "Type = 'Vol.'",
  );
  await expect(page.getByRole("region", { name: "表登记" })).toContainText(
    "pt_history",
  );
  await expect(page.getByRole("region", { name: "运行参数" })).toContainText(
    "统计月",
  );
  await expect(page.getByRole("region", { name: "运行参数" })).toContainText(
    "文字，格式 FY\\d{2}_\\d{2}_M",
  );
  await expect(page.getByRole("region", { name: "运行参数" })).toContainText(
    "按运行那天推算上个月，写成 FY{yy}_{MM}_M",
  );
  // 统计月 is required with no default and no rule: a release could not fill it in.
  await expect(page.getByRole("region", { name: "运行参数" })).toContainText(
    "没写（上线时取不到，不能上线）",
  );
  await expect(
    page.getByRole("region", { name: "数据外发策略" }),
  ).toContainText("只发统计");
  await expect(page.getByRole("region", { name: "验收检查" })).toContainText(
    "本月有结果",
  );
  await expect(page.getByRole("region", { name: "验收检查" })).toContainText(
    "聚合值",
  );

  // The same id cannot be used twice.
  const {
    created_at: _made,
    updated_at: _changed,
    ...saved
  } = await (await page.request.get(`/api/projects/${id}`)).json();
  void [_made, _changed];
  const again = await page.request.post("/api/projects", {
    data: { ...saved, name: "别的" },
  });
  expect(again.status()).toBe(409);
  expect((await again.json()).detail).toBe(`项目 ${id} 已经存在`);

  await page.getByRole("button", { name: "编辑", exact: true }).click();
  const edit = page.getByRole("form", { name: "编辑项目" });
  await expect(edit.getByRole("button", { name: "保存" })).toBeDisabled();
  await edit.getByLabel("名称", { exact: true }).fill(`改过的 ${id}`);
  await edit.getByRole("button", { name: "保存" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "已保存" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    `改过的 ${id}`,
  );

  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(list.getByRole("link", { name: `改过的 ${id}` })).toHaveCount(0);
});

test("新建运行时选项目、环境并填参数，运行页显示项目和外发策略", async ({
  page,
  request,
}) => {
  const app = await boundApp(request, "required");
  // The app needs a project; the API says which.
  const missing = await request.post("/api/runs", {
    data: { app, input: { question: "多少？" } },
  });
  expect(missing.status()).toBe(422);
  expect((await missing.json()).detail).toBe(
    "应用「绑定项目的应用」需要选择一个项目",
  );
  const unknown = await request.post("/api/runs", {
    data: {
      app,
      input: { question: "多少？" },
      project: {
        project_id: "sales-forecast",
        params: { update_month: "2026-09", owner: "x" },
      },
    },
  });
  expect((await unknown.json()).detail).toBe("项目「销售预测」没有参数 owner");

  await page.goto(`/runs/new?app=${app}`);
  const form = page.getByRole("form", { name: "新建运行" });
  const picker = form.getByLabel("项目", { exact: true });
  await expect(picker).toBeEnabled();
  await form.getByLabel("客户问题").fill("这个月的预测是多少？");
  await form.getByRole("button", { name: "开始运行" }).click();
  await expect(form).toContainText("这个应用需要选择一个项目");

  await picker.selectOption({ label: "销售预测" });
  await expect(form.getByLabel("环境")).toHaveValue("dev");
  await expect(form).toContainText("写到 main.sales_dev，表名以 sf_ 开头。");
  await form.getByLabel("环境").selectOption({ label: "生产（prod）" });
  await expect(form.getByLabel("版本号（选填）")).toHaveValue("1");
  await form.getByRole("button", { name: "开始运行" }).click();
  await expect(form).toContainText("请填写参数「统计月」");
  await form.getByLabel("统计月").fill("2026-09");
  await form.getByRole("button", { name: "开始运行" }).click();

  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  const header = page.getByRole("main");
  await expect(header.getByRole("link", { name: "销售预测" })).toBeVisible();
  await expect(header).toContainText("环境：生产");
  await expect(header).toContainText("统计月：2026-09");
  await expect(header).toContainText("版本号：1");
  await expect(header).toContainText("只发统计（项目设置）");
});

test("文本参数可以限定格式，新建运行时填的值不符合就开始不了", async ({
  page,
  request,
}) => {
  const id = unique("fmt");
  const project = {
    project_id: id,
    name: `格式 ${id}`,
    table_prefix: "fm_",
    environments: [
      { key: "dev", label: "开发", catalog: "main", schema: "fm_dev" },
    ],
    params: [
      {
        key: "update_month",
        label: "统计月",
        type: "month",
        default: "2026-09",
      },
      {
        key: "version_id",
        label: "版本",
        type: "text",
        pattern: "FY\\d{2}_\\d{2}_M",
      },
    ],
  };
  const notText = await request.post("/api/projects", {
    data: {
      ...project,
      params: [{ key: "m", label: "月", type: "month", pattern: "x" }],
    },
  });
  expect(notText.status()).toBe(422);
  expect((await notText.json()).detail).toContain(
    "参数「月」不是文本，不能写格式",
  );
  const made = await request.post("/api/projects", { data: project });
  expect(made.status(), await made.text()).toBe(201);

  const app = await boundApp(request, "required");
  const wrong = await request.post("/api/runs", {
    data: {
      app,
      input: { question: "多少？" },
      project: { project_id: id, params: { version_id: "FY26-06" } },
    },
  });
  expect(wrong.status()).toBe(422);
  expect((await wrong.json()).detail).toBe(
    "参数「版本」要符合格式 FY\\d{2}_\\d{2}_M",
  );

  // The run form says the format and checks it before sending.
  await page.goto(`/runs/new?app=${app}`);
  const form = page.getByRole("form", { name: "新建运行" });
  await form.getByLabel("客户问题").fill("这个月的预测是多少？");
  await form
    .getByLabel("项目", { exact: true })
    .selectOption({ label: `格式 ${id}` });
  const field = form.getByLabel("版本（选填）");
  await expect(form).toContainText("格式：FY\\d{2}_\\d{2}_M");
  await field.fill("FY26-06");
  await form.getByRole("button", { name: "开始运行" }).click();
  await expect(form).toContainText("参数「版本」要符合格式 FY\\d{2}_\\d{2}_M");
  await field.fill("FY26_06_M");
  await form.getByRole("button", { name: "开始运行" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("main")).toContainText("版本：FY26_06_M");
});

test("开始节点可以设使用项目；没设时引用项目和参数保存不了", async ({
  request,
}) => {
  const app = unique("refs");
  expect(
    (
      await request.post("/api/apps", {
        data: { app, title: "引用项目", from: { blank: true } },
      })
    ).status(),
  ).toBe(201);
  const config: FlowConfig = (
    await (await request.get(`/api/apps/${app}`)).json()
  ).current.config;
  const answer = config.graph.nodes.find((node) => node.type === "llm")!;
  if (answer.type === "llm")
    answer.prompt = "{{param.update_month}} {{project.owner}}";
  const unbound = await request.put(`/api/apps/${app}/draft`, {
    data: { config },
  });
  expect(unbound.status()).toBe(422);
  expect(await unbound.text()).toContain(
    "开始节点没有设置使用项目，不能引用项目和运行参数",
  );
  const start = config.graph.nodes.find((node) => node.type === "start")!;
  if (start.type === "start") start.project = "optional";
  const wrong = await request.put(`/api/apps/${app}/draft`, {
    data: { config },
  });
  expect(await wrong.text()).toContain("项目没有 owner 这一项");
  if (answer.type === "llm")
    answer.prompt = "{{param.update_month}} {{project.brief}}";
  expect(
    (
      await request.put(`/api/apps/${app}/draft`, { data: { config } })
    ).status(),
  ).toBe(200);
  expect((await (await request.get(`/api/apps/${app}`)).json()).project).toBe(
    null,
  );
});
