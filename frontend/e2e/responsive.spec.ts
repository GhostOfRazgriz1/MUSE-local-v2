import { test, expect } from "@playwright/test";
import { waitForAppLoad, waitForConnection } from "./helpers";

test.describe("Responsive / mobile behavior", () => {
  test.use({ viewport: { width: 375, height: 812 } }); // iPhone viewport

  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "chat") {
      test.skip(true, "Setup card is showing");
      return;
    }
    await waitForConnection(page);
  });

  test("sidebar can be toggled on mobile", async ({ page }) => {
    // On mobile, sidebar may start closed or be overlaid
    const toggleBtn = page.locator('button[aria-label="Close sidebar"], button[aria-label="Open sidebar"]').first();
    await toggleBtn.click();
    await page.waitForTimeout(500);

    // Check sidebar wrapper state changed
    const wrapper = page.locator(".sidebar-wrapper");
    const isOpen = await wrapper.evaluate((el) => el.classList.contains("open"));

    // Toggle back
    if (isOpen) {
      // Should have a scrim overlay on mobile
      const scrim = page.locator(".sidebar-scrim");
      if (await scrim.isVisible()) {
        await scrim.click(); // clicking scrim closes sidebar
        await page.waitForTimeout(500);
        await expect(page.locator(".sidebar-wrapper.open")).not.toBeVisible();
      }
    }
  });

  test("chat input is accessible on mobile", async ({ page }) => {
    const textarea = page.locator(".input-textarea");
    await expect(textarea).toBeVisible();
    await textarea.focus();
    await expect(textarea).toBeFocused();
  });

  test("sidebar closes after selecting a session on mobile", async ({ page }) => {
    // Open sidebar
    const openBtn = page.locator('button[aria-label="Open sidebar"]');
    if (await openBtn.isVisible()) {
      await openBtn.click();
    }
    await page.waitForTimeout(500);

    // If there are sessions, clicking one should close the sidebar
    const sessions = page.locator(".session-item");
    if (await sessions.count() > 0) {
      await sessions.first().click();
      await page.waitForTimeout(1_000);
      // On mobile (<768px), sidebar should auto-close
      await expect(page.locator(".sidebar-wrapper.open")).not.toBeVisible();
    }
  });
});
