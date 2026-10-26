import { expect, test } from "@playwright/test";

test("production ignores mock flag and forwards health and configured API base path", async ({
  request,
  page,
}) => {
  const response = await request.get("/api/runs/fixture");
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual({ source: "contract-fixture" });
  expect(response.headers()["cache-control"]).toBe("no-store");
  const health = await request.get("/api/readyz");
  expect(await health.json()).toEqual({
    status: "ok",
    source: "contract-fixture",
  });
  await page.goto("/");
  await expect(page.getByText("后端状态：正常")).toBeVisible();
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
    await page.getByText("查看完整结果").click();
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

test("does not expose arbitrary backend routes or incorrect methods", async ({
  request,
}) => {
  expect((await request.get("/api/llm/chat")).status()).toBe(404);
  expect((await request.get("/api/runs/id/resume")).status()).toBe(405);
  expect((await request.post("/api/readyz", { data: {} })).status()).toBe(405);
  expect((await request.post("/api/runs/id", { data: {} })).status()).toBe(405);
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
