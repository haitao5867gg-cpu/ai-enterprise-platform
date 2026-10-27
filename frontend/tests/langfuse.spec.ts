import { expect, test } from "@playwright/test";

for (const traceId of ["trace/with space", null]) {
  test(`Langfuse runtime configuration with ${traceId ? "a trace" : "no trace"}`, async ({
    page,
  }, testInfo) => {
    const id = "langfuse-config";
    await page.route(`**/api/runs/${id}`, (route) =>
      route.fulfill({
        json: {
          run_id: id,
          app: "hello",
          status: "done",
          messages: [],
          pending: null,
          result: "Langfuse 配置测试",
          error: null,
          trace_id: traceId,
        },
      }),
    );
    await page.goto(`/runs/${id}`);
    const baseUrl = testInfo.project.metadata.langfuseTraceBaseUrl;
    const link = page.getByRole("link", { name: "在 Langfuse 中查看" });
    // Wait for the run to load before checking that a link is absent.
    await expect(page.getByRole("heading", { name: "运行结果" })).toBeVisible();
    if (baseUrl && traceId) {
      await expect(link).toHaveAttribute(
        "href",
        `${baseUrl}trace%2Fwith%20space`,
      );
      await expect(link).toHaveAttribute("target", "_blank");
      await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    } else {
      await expect(link).toHaveCount(0);
    }
  });
}
