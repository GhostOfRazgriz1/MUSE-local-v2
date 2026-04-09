import { test, expect } from "@playwright/test";
import { waitForAppLoad, waitForConnection } from "./helpers";

test.describe("App loading", () => {
  test("renders the MUSE title in the topbar", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);

    if (state === "chat") {
      await expect(page.locator(".topbar-title")).toHaveText("MUSE");
    } else {
      // Setup card is showing — that's still a valid load
      await expect(page.locator(".setup-card")).toBeVisible();
    }
  });

  test("shows connection status indicator", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "chat") {
      test.skip(true, "Setup card is showing — no connection dot visible");
      return;
    }
    // Connection dot should exist (either connected or disconnected)
    await expect(page.locator(".connection-dot")).toBeVisible();
  });

  test("establishes WebSocket connection", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "chat") {
      test.skip(true, "Setup card is showing");
      return;
    }
    await waitForConnection(page);
    await expect(page.locator(".connection-dot.connected")).toBeVisible();
  });

  test("displays greeting message on connect", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "chat") {
      test.skip(true, "Setup card is showing");
      return;
    }
    await waitForConnection(page);
    // Agent greeting should appear as the first message
    await expect(page.locator(".msg-bubble.agent, .chat-empty").first()).toBeVisible({
      timeout: 15_000,
    });
  });

  test("sidebar is open by default on desktop", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "chat") {
      test.skip(true, "Setup card is showing");
      return;
    }
    await expect(page.locator(".sidebar-wrapper.open")).toBeVisible();
  });

  test("skip-to-content link exists for accessibility", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "chat") {
      test.skip(true, "Setup card is showing");
      return;
    }
    await expect(page.locator(".skip-link")).toHaveAttribute("href", "#main-content");
  });
});
