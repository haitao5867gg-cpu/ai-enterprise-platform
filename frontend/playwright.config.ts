import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? "github" : "list",
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1360, height: 960 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "workbench",
      testMatch: [
        "workbench.spec.ts",
        "etl.spec.ts",
        "flow.spec.ts",
        "langfuse.spec.ts",
        "uploads.spec.ts",
        "apps-skills.spec.ts",
        "flow-apps.spec.ts",
        "flow-editor.spec.ts",
      ],
      use: { baseURL: "http://127.0.0.1:3100" },
      metadata: {
        langfuseTraceBaseUrl: "http://localhost:3100/project/aep-local/traces/",
      },
    },
    {
      name: "production-proxy",
      testMatch: ["proxy.spec.ts", "langfuse.spec.ts"],
      use: { baseURL: "http://127.0.0.1:3101" },
      metadata: {
        langfuseTraceBaseUrl:
          "https://traces.example.com/langfuse/project/azure%20project%2F%E6%B5%8B%E8%AF%95/traces/",
      },
    },
    {
      name: "langfuse-disabled",
      testMatch: "langfuse.spec.ts",
      use: { baseURL: "http://127.0.0.1:3103" },
      metadata: { langfuseTraceBaseUrl: null },
    },
  ],
  webServer: [
    {
      command: "node tests/fixtures/backend.mjs",
      url: "http://127.0.0.1:3102/internal/v1/readyz",
      reuseExistingServer: false,
    },
    {
      command: "npm run dev -- --hostname 127.0.0.1 --port 3100",
      url: "http://127.0.0.1:3100",
      env: {
        NODE_ENV: "development",
        AEP_MOCK_RUNS: "true",
        AEP_API_INTERNAL_URL: "http://127.0.0.1:3102/internal/v1/",
        NEXT_TELEMETRY_DISABLED: "1",
      },
      reuseExistingServer: false,
    },
    // Deliberately enable the flag: production must still forward to the fixture.
    {
      command: "node tests/fixtures/start-production.mjs",
      url: "http://127.0.0.1:3101",
      env: {
        NODE_ENV: "production",
        HOSTNAME: "127.0.0.1",
        PORT: "3101",
        AEP_MOCK_RUNS: "true",
        AEP_API_INTERNAL_URL: "http://127.0.0.1:3102/internal/v1/",
        AEP_LANGFUSE_UI_URL: "https://traces.example.com/langfuse/",
        AEP_LANGFUSE_PROJECT_ID: "azure project/测试",
        NEXT_TELEMETRY_DISABLED: "1",
      },
      reuseExistingServer: false,
    },
    // Reuse the same standalone build with a different runtime configuration.
    {
      command: "node tests/fixtures/start-production.mjs",
      url: "http://127.0.0.1:3103",
      env: {
        NODE_ENV: "production",
        HOSTNAME: "127.0.0.1",
        PORT: "3103",
        AEP_API_INTERNAL_URL: "http://127.0.0.1:3102/internal/v1/",
        AEP_LANGFUSE_UI_URL: "",
        AEP_LANGFUSE_PROJECT_ID: "aep-local",
        NEXT_TELEMETRY_DISABLED: "1",
      },
      reuseExistingServer: false,
    },
  ],
});
