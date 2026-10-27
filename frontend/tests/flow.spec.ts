import { expect, test } from "@playwright/test";

test("ETL flow view: design, path, current node and each step's input and output", async ({
  page,
  request,
}) => {
  const created = await request.post("/api/runs", {
    data: {
      app: "etl",
      input: { datasource_id: "sample-postgres", request: "整理订单" },
    },
  });
  const { run_id: id } = await created.json();
  await page.goto(`/runs/${id}`);

  const flow = page.getByRole("region", { name: "工作流程" });
  await expect(flow).toContainText("固定流程的工作流");
  await expect(flow).toContainText("没有接知识库");
  const path = flow.getByRole("list", { name: "本次运行的步骤" });
  await expect(path.getByRole("button")).toHaveText([
    "1. 探查数据",
    "2. 出方案",
    "3. 确认方案",
  ]);
  const chart = flow.getByRole("group", { name: "流程图" });
  // The run waits at the review node, which opens first.
  await expect(chart.locator(".flow-node.current.waiting")).toHaveAttribute(
    "aria-label",
    /确认方案/,
  );
  const detail = flow.getByRole("region", { name: "节点 确认方案" });
  await expect(detail).toContainText("人工确认");
  await expect(detail).toContainText("向人提问：请检查 ETL 方案");
  await expect(detail).toContainText("等人做出决定后才有输出");

  await chart.getByRole("button", { name: /^出方案/ }).click();
  const plan = flow.getByRole("region", { name: "节点 出方案" });
  await expect(plan).toContainText("模型生成");
  await expect(plan).toContainText("mock");
  await expect(plan).toContainText("知识来源");
  await plan.getByText("查看提示词全文").click();
  await expect(plan).toContainText("本地模拟提示词");
  await expect(plan.locator(".flow-step .flow-json").first()).toContainText(
    "orders_clean",
  );

  await page.getByLabel("驳回意见").fill("改成全量");
  await page.getByRole("button", { name: "驳回", exact: true }).click();
  await expect(path.getByRole("button")).toHaveCount(5);
  await expect(
    chart.getByRole("button", { name: /^出方案.*执行 2 次/ }),
  ).toBeVisible();
  await expect(chart.locator(".flow-edge.on")).toHaveCount(4);

  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(path.getByRole("button")).toHaveText([
    "1. 探查数据",
    "2. 出方案",
    "3. 确认方案",
    "4. 出方案",
    "5. 确认方案",
    "6. 写代码",
    "7. 打包",
  ]);
  await expect(chart.locator(".flow-node.current")).toHaveCount(0);
  // The first review's decision is its output.
  await path.getByRole("button", { name: "3. 确认方案" }).click();
  const review = flow.getByRole("region", { name: "节点 确认方案" });
  await expect(review.getByRole("button", { name: /第 1 次/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(review.locator(".flow-step .flow-json").first()).toContainText(
    "改成全量",
  );
  await chart.getByRole("button", { name: /^上线审批/ }).click();
  await expect(
    flow.getByRole("region", { name: "节点 上线审批" }),
  ).toContainText("这次运行没有走到这个节点");
});
