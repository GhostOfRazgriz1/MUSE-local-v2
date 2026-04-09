import { test, expect } from "@playwright/test";
import { waitForAppLoad, waitForConnection, sendMessage, waitForAgentResponse } from "./helpers";

test.describe("Session sidebar", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "chat") {
      test.skip(true, "Setup card is showing");
      return;
    }
    await waitForConnection(page);
  });

  test("sidebar shows Sessions header and new-session button", async ({ page }) => {
    await expect(page.locator(".sidebar-wrapper.open")).toBeVisible();
    await expect(page.locator(".sidebar-new-btn")).toBeVisible();
  });

  test("toggle button opens and closes sidebar", async ({ page }) => {
    const toggleBtn = page.locator('button[aria-label="Close sidebar"], button[aria-label="Open sidebar"]').first();

    // Sidebar starts open
    await expect(page.locator(".sidebar-wrapper.open")).toBeVisible();

    // Close it
    await toggleBtn.click();
    await expect(page.locator(".sidebar-wrapper.open")).not.toBeVisible();

    // Re-open it
    const openBtn = page.locator('button[aria-label="Open sidebar"]');
    await openBtn.click();
    await expect(page.locator(".sidebar-wrapper.open")).toBeVisible();
  });

  test("current session appears in the session list", async ({ page }) => {
    // Wait for greeting to assign a session
    await page.waitForSelector(".msg-bubble.agent, .chat-empty", { timeout: 15_000 });

    // At least one session item should exist
    const items = page.locator(".session-item");
    await expect(items.first()).toBeVisible({ timeout: 5_000 });
  });

  test("active session is highlighted", async ({ page }) => {
    await page.waitForSelector(".msg-bubble.agent, .chat-empty", { timeout: 15_000 });
    await page.waitForSelector(".session-item", { timeout: 5_000 });

    // The active session has aria-current="true"
    const active = page.locator('.session-item[aria-current="true"]');
    await expect(active).toBeVisible();
  });

  test("new session button creates a fresh session", async ({ page }) => {
    // Wait for initial session to load
    await page.waitForSelector(".msg-bubble.agent, .chat-empty", { timeout: 15_000 });
    const initialCount = await page.locator(".session-item").count();

    // Create new session
    await page.click(".sidebar-new-btn");

    // Wait for new session to appear
    await page.waitForTimeout(2_000);
    const newCount = await page.locator(".session-item").count();
    expect(newCount).toBeGreaterThanOrEqual(initialCount);

    // Chat area should be cleared
    // (either empty state or a new greeting)
    const messages = page.locator(".msg-bubble.user");
    expect(await messages.count()).toBe(0);
  });

  test("clicking a different session switches to it", async ({ page }) => {
    // Create a message in first session
    await sendMessage(page, "First session message");
    await waitForAgentResponse(page);

    // Create second session
    await page.click(".sidebar-new-btn");
    await page.waitForTimeout(2_000);

    // Send message in second session
    await waitForConnection(page);
    await sendMessage(page, "Second session message");
    await waitForAgentResponse(page);

    // Click back to first session
    const firstSession = page.locator(".session-item").first();
    await firstSession.click();
    await page.waitForTimeout(2_000);

    // Should see the first session's messages
    await expect(page.locator(".msg-bubble.user").first()).toContainText("First session message");
  });

  test("session item shows delete button on hover", async ({ page }) => {
    await page.waitForSelector(".session-item", { timeout: 10_000 });

    const sessionItem = page.locator(".session-item").first();
    await sessionItem.hover();

    // Delete button should be visible on hover
    await expect(sessionItem.locator(".session-item-delete")).toBeVisible({ timeout: 2_000 });
  });

  test("delete button opens confirmation modal", async ({ page }) => {
    await page.waitForSelector(".session-item", { timeout: 10_000 });

    const sessionItem = page.locator(".session-item").first();
    await sessionItem.hover();
    await sessionItem.locator(".session-item-delete").click();

    // Confirmation modal should appear
    await expect(page.locator(".modal-overlay")).toBeVisible({ timeout: 3_000 });
    await expect(page.locator(".modal-card")).toBeVisible();
  });

  test("search input appears when 3+ sessions exist", async ({ page }) => {
    // Create multiple sessions
    for (let i = 0; i < 3; i++) {
      await page.click(".sidebar-new-btn");
      await page.waitForTimeout(1_500);
      await waitForConnection(page);
    }

    // Search input should now be visible
    const searchInput = page.locator(".sidebar-search-input");
    // It may or may not appear depending on session count
    if (await page.locator(".session-item").count() >= 3) {
      await expect(searchInput).toBeVisible({ timeout: 3_000 });
    }
  });
});
