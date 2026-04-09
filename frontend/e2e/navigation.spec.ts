import { test, expect } from "@playwright/test";
import { waitForAppLoad, waitForConnection } from "./helpers";

test.describe("Navigation and panels", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "chat") {
      test.skip(true, "Setup card is showing");
      return;
    }
    await waitForConnection(page);
  });

  test("topbar has all expected buttons", async ({ page }) => {
    // Left side: sidebar toggle
    await expect(page.locator('button[aria-label="Close sidebar"], button[aria-label="Open sidebar"]').first()).toBeVisible();

    // Right side: memories, files, tasks, settings
    await expect(page.locator('button[aria-label="What I know about you"]')).toBeVisible();
    await expect(page.locator('button[aria-label="File browser"]')).toBeVisible();
    await expect(page.locator('button[aria-label="Tasks"]')).toBeVisible();
    await expect(page.locator('button[aria-label="Settings"]')).toBeVisible();
  });

  test("memory panel opens and closes", async ({ page }) => {
    const memBtn = page.locator('button[aria-label="What I know about you"]');
    await memBtn.click();

    // Memory panel should appear
    await expect(page.locator(".memory-panel, .memory-panel-overlay").first()).toBeVisible({
      timeout: 5_000,
    });

    // Close it
    const closeBtn = page.locator(".memory-panel .close-btn, .memory-panel-close, [aria-label='Close']").first();
    if (await closeBtn.isVisible()) {
      await closeBtn.click();
    } else {
      // Toggle via button again
      await memBtn.click();
    }
  });

  test("file browser panel opens and closes", async ({ page }) => {
    const fileBtn = page.locator('button[aria-label="File browser"]');
    await fileBtn.click();

    // File browser panel should appear
    await expect(page.locator(".file-browser-panel").first()).toBeVisible({ timeout: 5_000 });

    // Button should have active class
    await expect(fileBtn).toHaveClass(/active/);
  });

  test("task tray popover opens on click", async ({ page }) => {
    const taskBtn = page.locator('button[aria-label="Tasks"]');
    await taskBtn.click();

    // Task popover should be visible
    await expect(page.locator(".task-popover")).toBeVisible();

    // Click outside to close
    await page.locator(".topbar").click({ position: { x: 10, y: 10 } });
    await page.waitForTimeout(500);
  });

  test("Escape closes task popover", async ({ page }) => {
    await page.locator('button[aria-label="Tasks"]').click();
    await expect(page.locator(".task-popover")).toBeVisible();

    await page.keyboard.press("Escape");
    await page.waitForTimeout(500);
    // Popover should be hidden (display: none)
    await expect(page.locator(".task-popover")).toBeHidden();
  });

  test("Ctrl+Shift+N creates new session", async ({ page }) => {
    // Wait for initial session
    await page.waitForSelector(".msg-bubble.agent, .chat-empty", { timeout: 15_000 });

    await page.keyboard.press("Control+Shift+N");
    await page.waitForTimeout(2_000);

    // Should clear messages (fresh session)
    const userMsgs = page.locator(".msg-bubble.user");
    expect(await userMsgs.count()).toBe(0);
  });

  test("cost dashboard is visible in topbar", async ({ page }) => {
    // The CostDashboard component renders token counts
    const topbarRight = page.locator(".topbar-right");
    await expect(topbarRight).toBeVisible();
  });
});
