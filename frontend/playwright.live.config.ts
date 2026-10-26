import { defineConfig, devices } from "@playwright/test";

const target = process.env.AEP_LIVE_BASE_URL;
if (!target)
  throw new Error("Set AEP_LIVE_BASE_URL to the local Compose web URL.");
const url = new URL(target);
if (
  url.protocol !== "http:" ||
  !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
) {
  throw new Error(
    "Live acceptance tests only create synthetic runs on a local HTTP server.",
  );
}

export default defineConfig({
  testDir: "./tests/live",
  workers: 1,
  forbidOnly: true,
  timeout: 60_000,
  expect: { timeout: 20_000 },
  use: {
    ...devices["Desktop Chrome"],
    baseURL: url.origin,
    viewport: { width: 1360, height: 960 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
