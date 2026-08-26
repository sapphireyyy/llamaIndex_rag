import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 150_000,
  expect: { timeout: 15_000 },
  workers: 1,
  fullyParallel: false,
  reporter: [["line"]],
  use: {
    baseURL: process.env.E2E_WEB_URL ?? "http://localhost:3000",
    channel: process.env.E2E_BROWSER_CHANNEL ?? "chrome",
    headless: true,
    trace: "off",
    screenshot: "off",
    video: "off",
    acceptDownloads: true,
  },
  outputDir: ".playwright-output",
});
