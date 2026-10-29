import { expect, test } from "@playwright/test";

test("knowledge proxy preserves multipart bytes, chunk pagination and original download", async ({
  request,
}) => {
  const path = "/api/knowledge-bases/policies/documents";
  const bytes = [0x25, 0x50, 0x44, 0x46, 0xff, 0, 0xfe];
  const upload = await request.post(path, {
    multipart: {
      files: {
        name: "policy.pdf",
        mimeType: "application/pdf",
        buffer: Buffer.from(bytes),
      },
    },
  });
  expect(upload.status()).toBe(202);
  expect(await upload.json()).toEqual([{ name: "policy.pdf", bytes }]);
  const chunks = await request.get(`${path}/policy/chunks?offset=50&limit=50`);
  expect(await chunks.json()).toEqual({ offset: "50", limit: "50" });
  const file = await request.get(`${path}/policy/file`);
  expect(file.headers()["content-disposition"]).toBe(
    "attachment; filename=policy.pdf",
  );
  expect([...(await file.body())]).toEqual(bytes);
});

test("production ignores mock flag and forwards health and configured API base path", async ({
  request,
  page,
}) => {
  const response = await request.get("/api/runs/fixture");
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual({ source: "contract-fixture" });
  expect(response.headers()["cache-control"]).toBe("no-store");
  const flow = await request.get("/api/runs/fixture/flow");
  expect(await flow.json()).toEqual({ source: "contract-fixture-flow" });
  const health = await request.get("/api/readyz");
  expect(await health.json()).toEqual({
    status: "ok",
    source: "contract-fixture",
  });
  await page.goto("/");
  await expect(page.getByText("服务状态：正常")).toBeVisible();
  await expect(page.getByText("本地模拟模式")).toHaveCount(0);
});

for (const decision of ["approve", "reject"] as const) {
  test(`production browser flow through real route handlers: ${decision}`, async ({
    page,
  }) => {
    await page.goto("/runs/new");
    await page.getByLabel("你想完成什么？").fill(`proxy ${decision}`);
    await page.getByRole("button", { name: "开始运行" }).click();
    await expect(
      page.getByRole("heading", { name: "需要你的确认" }),
    ).toBeVisible();
    if (decision === "reject")
      await page.getByLabel("驳回意见").fill("请调整输入");
    await page
      .getByRole("button", {
        name: decision === "approve" ? "确认" : "驳回",
        exact: true,
      })
      .click();
    if (decision === "reject") {
      await expect(page.getByRole("list", { name: "消息记录" })).toContainText(
        "已根据意见重新生成：请调整输入",
      );
      await page.getByRole("button", { name: "确认", exact: true }).click();
    }
    await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
    await page.getByText("开发者视图", { exact: true }).click();
    await expect(page.locator(".result-json")).toContainText(
      "contract-fixture",
    );
    await expect(page.locator(".result-json")).toContainText("已确认并完成");
  });
}

test("preserves API validation, missing-run and conflict responses", async ({
  request,
}) => {
  expect(
    (
      await request.post("/api/runs", { data: { app: "hello", input: {} } })
    ).status(),
  ).toBe(422);
  expect((await request.get("/api/runs/missing-run")).status()).toBe(404);
  const created = await request.post("/api/runs", {
    data: { app: "hello", input: { text: "API contract" } },
  });
  expect(created.status()).toBe(201);
  const { run_id: id } = await created.json();
  const resumed = await request.post(`/api/runs/${id}/resume`, {
    data: { decision: "reject", comment: "test comment" },
  });
  expect(resumed.status()).toBe(202);
  const rejected = await request.get(`/api/runs/${id}`);
  expect((await rejected.json()).messages).toContainEqual({
    role: "user",
    content: "reject: test comment",
  });
  expect((await rejected.json()).status).toBe("waiting");
  expect(
    (
      await request.post(`/api/runs/${id}/resume`, {
        data: { decision: "approve", comment: "" },
      })
    ).status(),
  ).toBe(202);
  expect(
    (
      await request.post(`/api/runs/${id}/resume`, {
        data: { decision: "approve", comment: "" },
      })
    ).status(),
  ).toBe(409);
  const detail = await request.get(`/api/runs/${id}`);
  expect((await detail.json()).result).toEqual({
    answer: "已确认并完成。",
    source: "contract-fixture",
  });
});

test("passes cancel, retry and rerun through with their responses", async ({
  request,
}) => {
  const created = await request.post("/api/runs", {
    data: { app: "hello", input: { text: "取消测试" } },
  });
  const { run_id: id } = await created.json();
  const cancelled = await request.post(`/api/runs/${id}/cancel`, {
    data: { actor: "李雷" },
  });
  expect(cancelled.status()).toBe(202);
  expect(await cancelled.json()).toEqual({ run_id: id, status: "cancelled" });
  expect(
    (await request.post(`/api/runs/${id}/cancel`, { data: {} })).status(),
  ).toBe(409);
  const retried = await request.post(`/api/runs/${id}/retry`, { data: {} });
  expect(retried.status()).toBe(409);
  expect((await retried.json()).detail).toBe(
    "这次运行已经不在失败状态，可能有人已经重试过",
  );
  const rerun = await request.post(`/api/runs/${id}/rerun`, {
    data: { input: { text: "再来一次" } },
  });
  expect(rerun.status()).toBe(201);
  const copy = await (
    await request.get(`/api/runs/${(await rerun.json()).run_id}`)
  ).json();
  expect(copy.rerun_of).toBe(id);
  expect(copy.messages[0]).toEqual({ role: "user", content: "再来一次" });
  expect((await request.get(`/api/runs/${id}/cancel`)).status()).toBe(405);
});

test("does not expose arbitrary backend routes or incorrect methods", async ({
  request,
}) => {
  expect((await request.get("/api/llm/chat")).status()).toBe(404);
  expect((await request.get("/api/runs/id/resume")).status()).toBe(405);
  expect((await request.post("/api/readyz", { data: {} })).status()).toBe(405);
  expect((await request.post("/api/runs/id", { data: {} })).status()).toBe(405);
  expect((await request.post("/api/datasources", { data: {} })).status()).toBe(
    405,
  );
  expect(
    (await request.post("/api/runs/id/files.zip", { data: {} })).status(),
  ).toBe(405);
  expect((await request.get("/api/datasources/id")).status()).toBe(404);
  expect((await request.get("/api/runs/id/files.zip/extra")).status()).toBe(
    404,
  );
});

test("forwards data sources and preserves ZIP bytes, download headers and errors", async ({
  request,
}) => {
  const sources = await request.get("/api/datasources");
  expect(sources.status()).toBe(200);
  expect(await sources.json()).toEqual([
    {
      datasource_id: "fixture-postgres",
      name: "契约示例库",
      kind: "postgres",
      description: "合成测试数据源",
    },
  ]);
  expect(sources.headers()["cache-control"]).toBe("no-store");
  const zip = await request.get("/api/runs/zip-fixture/files.zip");
  expect(zip.status()).toBe(200);
  expect(zip.headers()["content-type"]).toBe("application/zip");
  expect(zip.headers()["content-disposition"]).toBe(
    "attachment; filename=\"fixture.zip\"; filename*=UTF-8''%E6%B5%8B%E8%AF%95.zip",
  );
  expect(zip.headers()["cache-control"]).toBe("no-store");
  expect(await zip.body()).toEqual(
    Buffer.from([0x50, 0x4b, 3, 4, 0, 0xff, 0xfe, 0x80, 0xc3, 0x28, 0]),
  );
  const missing = await request.get("/api/runs/missing-run/files.zip");
  expect(missing.status()).toBe(404);
  expect(await missing.json()).toEqual({ detail: "Run not found" });
});

test("network and timeout errors are bounded and hide internal connection details", async ({
  request,
}) => {
  const unavailable = await request.get("/api/runs/unavailable-run");
  expect(unavailable.status()).toBe(502);
  expect(await unavailable.text()).not.toContain("127.0.0.1");
  const timeout = await request.get("/api/runs/timeout-run");
  expect(timeout.status()).toBe(504);
});

test("forwards application and Skill methods, JSON bodies, 204 and contract errors in production", async ({
  request,
}) => {
  for (const path of [
    "/apps/etl",
    "/apps/etl/versions",
    "/skills",
    "/skills/etl-plan",
    "/skills/etl-plan/versions/1",
  ]) {
    const response = await request.get(`/api${path}`);
    expect(await response.json()).toMatchObject({
      source: "assets-contract-fixture",
      method: "GET",
      path,
    });
  }
  const mutations = [
    { method: "POST", path: "/skills" },
    { method: "PUT", path: "/skills/etl-plan/draft" },
    { method: "POST", path: "/skills/etl-plan/publish" },
    { method: "PUT", path: "/apps/etl/draft" },
    { method: "POST", path: "/apps/etl/draft/eval" },
    { method: "POST", path: "/apps/etl/draft/publish" },
    { method: "POST", path: "/apps/etl/versions/1/activate" },
  ];
  for (const { method, path } of mutations) {
    const payload = {
      note: "保留中文和换行\n发布",
      actor: "张三",
      config: { nodes: {} },
    };
    const response = await request.fetch(`/api${path}`, {
      method,
      data: payload,
    });
    expect(response.ok()).toBe(true);
    expect(await response.json()).toMatchObject({
      source: "assets-contract-fixture",
      method,
      path,
      payload,
    });
    expect(response.headers()["cache-control"]).toBe("no-store");
  }
  for (const path of ["/apps/etl/draft", "/skills/etl-plan/draft"]) {
    const response = await request.delete(`/api${path}`);
    expect(response.status()).toBe(204);
    expect(await response.text()).toBe("");
  }
  const conflict = await request.post("/api/apps/conflict/draft/publish", {
    data: {},
  });
  expect(conflict.status()).toBe(409);
  expect(await conflict.json()).toEqual({ detail: "请先通过评测" });
  const invalid = await request.put("/api/skills/invalid/draft", { data: {} });
  expect(invalid.status()).toBe(422);
  expect(await invalid.json()).toEqual({
    detail: [{ loc: ["body", "description"], msg: "说明不能为空" }],
  });
  expect((await request.put("/api/runs/fixture", { data: {} })).status()).toBe(
    405,
  );
  expect((await request.post("/api/apps/etl", { data: {} })).status()).toBe(
    405,
  );
  expect((await request.delete("/api/skills/etl-plan")).status()).toBe(405);
  expect((await request.get("/api/apps/etl/secrets")).status()).toBe(404);
});

test("multipart proxy preserves names, boundaries, binary bytes and validation errors", async ({
  request,
}) => {
  const bytes = Buffer.from([0x50, 0x4b, 0, 0xff, 0xfe, 0x80, 0xc3, 0x28, 0]);
  const response = await request.post("/api/datasources/files", {
    multipart: {
      name: "中文数据集",
      files: {
        name: "测试.xlsx",
        mimeType:
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        buffer: bytes,
      },
    },
  });
  expect(response.status()).toBe(201);
  expect(await response.json()).toEqual({
    name: "中文数据集",
    files: [{ name: "测试.xlsx", bytes: [...bytes] }],
  });
  const invalid = await request.post("/api/datasources/files", {
    multipart: {
      name: "invalid",
      files: {
        name: "bad.csv",
        mimeType: "text/csv",
        buffer: Buffer.from("id,id\n1,2"),
      },
    },
  });
  expect(invalid.status()).toBe(422);
  expect((await invalid.json()).detail).toContain("bad.csv");
  expect((await request.get("/api/datasources/files")).status()).toBe(405);
  expect(
    (await request.post("/api/datasources/files", { data: {} })).status(),
  ).toBe(415);
});

test("forwards only the new flow contract endpoints and preserves YAML download bytes", async ({
  request,
}) => {
  expect(await (await request.get("/api/apps")).json()).toEqual([
    expect.objectContaining({ app: "hello", kind: "flow" }),
  ]);
  for (const path of ["/node-types", "/app-templates"]) {
    expect(await (await request.get(`/api${path}`)).json()).toMatchObject({
      method: "GET",
      path,
    });
    expect((await request.post(`/api${path}`, { data: {} })).status()).toBe(
      405,
    );
  }
  const payload = {
    title: "中文名称",
    yaml: "format: aep-app/1\n",
    values: { "start.question": "订单" },
  };
  for (const [method, path] of [
    ["POST", "/apps"],
    ["PATCH", "/apps/hello"],
    ["POST", "/apps/import"],
    ["POST", "/apps/hello/draft/import"],
    ["POST", "/apps/hello/nodes/ask_order/test"],
    ["PUT", "/apps/hello/secrets/crm_token"],
  ]) {
    const response = await request.fetch(`/api${path}`, {
      method,
      data: payload,
    });
    expect(response.ok()).toBe(true);
    expect(await response.json()).toMatchObject({ method, path, payload });
  }
  for (const path of ["/apps/hello", "/apps/hello/secrets/crm_token"])
    expect((await request.delete(`/api${path}`)).status()).toBe(204);
  const exported = await request.get("/api/apps/hello/versions/1/export");
  expect(exported.headers()["content-type"]).toBe("text/yaml; charset=utf-8");
  expect(exported.headers()["content-disposition"]).toContain(
    "filename*=UTF-8''%E9%97%AE%E7%AD%94.yaml",
  );
  expect(await exported.body()).toEqual(
    Buffer.from("format: aep-app/1\ntitle: 问答\n"),
  );
  for (const path of [
    "/apps/hello/nodes/Invalid/test",
    "/apps/hello/nodes/a-b/test",
    "/apps/hello/nodes/" + "a".repeat(33) + "/test",
    "/apps/hello/secrets/Invalid",
    "/apps/hello/secrets/a-b",
    "/apps/hello/secrets/" + "s".repeat(33),
    "/apps/hello/nodes/ask_order/delete",
    "/apps/hello/versions/0/export",
    "/apps/hello/versions/1/export/extra",
  ])
    expect((await request.post(`/api${path}`, { data: {} })).status()).toBe(
      404,
    );
  expect(
    (
      await request.put("/api/apps/hello/nodes/ask_order/test", { data: {} })
    ).status(),
  ).toBe(405);
  expect(
    (await request.get("/api/apps/hello/secrets/crm_token")).status(),
  ).toBe(405);
});
