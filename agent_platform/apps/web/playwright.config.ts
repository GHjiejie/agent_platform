import { defineConfig } from "@playwright/test";

const baseURL = process.env.PLATFORM_E2E_BASE_URL ?? "http://127.0.0.1:5173";
if (!["127.0.0.1", "localhost", "::1"].includes(new URL(baseURL).hostname)) {
  throw new Error(
    "UI smoke tests must target an isolated local test deployment.",
  );
}

export default defineConfig({
  testDir: "./tests",
  timeout: 90_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    baseURL,
    browserName: "chromium",
    headless: true,
    actionTimeout: 10_000,
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
    trace: "off",
    video: "off",
  },
});
