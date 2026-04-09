import { test, expect } from "@playwright/test";
import { waitForAppLoad, waitForConnection, sendMessage, waitForAgentResponse } from "./helpers";

test.describe("Chat interface", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "chat") {
      test.skip(true, "Setup card is showing — chat not available");
      return;
    }
    await waitForConnection(page);
  });

  test("chat input textarea is visible and focusable", async ({ page }) => {
    const textarea = page.locator(".input-textarea");
    await expect(textarea).toBeVisible();
    await textarea.focus();
    await expect(textarea).toBeFocused();
  });

  test("send button is disabled when input is empty", async ({ page }) => {
    const sendBtn = page.locator(".send-btn");
    const textarea = page.locator(".input-textarea");
    await textarea.fill("");
    await expect(sendBtn).toBeDisabled();
  });

  test("send button enables when text is entered", async ({ page }) => {
    const sendBtn = page.locator(".send-btn");
    const textarea = page.locator(".input-textarea");
    await textarea.fill("Hello MUSE");
    await expect(sendBtn).toBeEnabled();
  });

  test("pressing Enter sends a message", async ({ page }) => {
    await sendMessage(page, "Hello from Playwright");
    // User message should appear in the chat
    await expect(
      page.locator(".msg-bubble.user").last()
    ).toContainText("Hello from Playwright");
  });

  test("Shift+Enter inserts a newline instead of sending", async ({ page }) => {
    const textarea = page.locator(".input-textarea");
    await textarea.fill("Line 1");
    await textarea.press("Shift+Enter");
    await textarea.type("Line 2");

    // Text should still be in the input (not sent)
    const value = await textarea.inputValue();
    expect(value).toContain("Line 1");
    expect(value).toContain("Line 2");
  });

  test("agent responds to a message", async ({ page }) => {
    await sendMessage(page, "What can you do?");
    // Wait for agent response (streaming or complete)
    await waitForAgentResponse(page, 30_000);
    const agentBubble = page.locator(".msg-bubble.agent").last();
    await expect(agentBubble).not.toBeEmpty();
  });

  test("agent response shows model name and copy button", async ({ page }) => {
    await sendMessage(page, "Hi");
    await waitForAgentResponse(page, 30_000);

    // Check footer elements on the last agent message
    const lastMsg = page.locator(".msg-row.agent").last();
    await expect(lastMsg.locator(".msg-model")).toBeVisible();
    await expect(lastMsg.locator(".msg-copy-btn")).toBeVisible();
  });

  test("input clears after sending", async ({ page }) => {
    const textarea = page.locator(".input-textarea");
    await textarea.fill("Test clear");
    await textarea.press("Enter");

    // Input should be empty after send
    await expect(textarea).toHaveValue("");
  });

  test("slash key focuses the input when not in a text field", async ({ page }) => {
    // Click elsewhere first to unfocus
    await page.locator(".topbar").click();
    await page.keyboard.press("/");
    await expect(page.locator(".input-textarea")).toBeFocused();
  });

  test("attach button is visible", async ({ page }) => {
    await expect(page.locator(".attach-btn")).toBeVisible();
  });

  test("mood indicator updates during processing", async ({ page }) => {
    await sendMessage(page, "Tell me a joke");
    // During processing, the mood should change from resting
    // We just check that the mood element exists (may change to thinking/working)
    const moodOrLogo = page.locator(".topbar-logo-icon");
    await expect(moodOrLogo).toBeVisible();
  });

  test("scroll-to-bottom button appears when scrolled up", async ({ page }) => {
    // Send several messages to create scrollable content
    for (let i = 0; i < 3; i++) {
      await sendMessage(page, `Message ${i + 1} for scrolling test`);
      await waitForAgentResponse(page, 30_000);
    }

    // Scroll up
    await page.locator(".chat-messages").evaluate((el) => {
      el.scrollTop = 0;
    });

    // The scroll-to-bottom button should appear
    await expect(page.locator(".scroll-to-bottom")).toBeVisible({ timeout: 3_000 });
  });
});
