import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E config for MUSE.
 *
 * Assumes:
 *   - Backend running on http://localhost:8080
 *   - Frontend dev server on http://localhost:3000 (proxies /api → :8080)
 *
 * Run:
 *   npx playwright test              # all tests
 *   npx playwright test --headed     # watch in browser
 *   npx playwright test --ui         # interactive UI mode
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // sequential — tests share server state
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  // Start both backend and frontend before tests
  webServer: [
    {
      command: "cd .. && python -m muse.main",
      port: 8080,
      reuseExistingServer: true,
      timeout: 60_000,
    },
    {
      command: "npm run dev",
      port: 3000,
      reuseExistingServer: true,
      timeout: 30_000,
    },
  ],
});
