import { type Page, expect } from "@playwright/test";

/**
 * Wait for the MUSE app to fully load (past the setup check).
 * Returns true if the app loaded into the main chat view,
 * false if the setup card appeared instead.
 */
export async function waitForAppLoad(page: Page): Promise<"chat" | "setup"> {
  // The app renders <div class="app" /> (empty) while checking providers,
  // then either shows SetupCard or the full chat UI.
  await page.waitForSelector(".app", { timeout: 10_000 });

  // Wait for either the setup card or the topbar (main UI) to appear
  const result = await Promise.race([
    page.waitForSelector(".setup-card", { timeout: 15_000 }).then(() => "setup" as const),
    page.waitForSelector(".topbar", { timeout: 15_000 }).then(() => "chat" as const),
  ]);
  return result;
}

/** Wait for WebSocket connection indicator to show "connected". */
export async function waitForConnection(page: Page) {
  await page.waitForSelector(".connection-dot.connected", { timeout: 15_000 });
}

/** Send a chat message and wait for it to appear as a user bubble. */
export async function sendMessage(page: Page, text: string) {
  const textarea = page.locator(".input-textarea");
  await textarea.fill(text);
  await textarea.press("Enter");
  // Wait for the user message bubble to appear with this text
  await expect(page.locator(".msg-bubble.user").last()).toContainText(text, {
    timeout: 5_000,
  });
}

/** Wait for an agent response bubble to appear (streaming or complete). */
export async function waitForAgentResponse(page: Page, timeout = 30_000) {
  await page.waitForSelector(".msg-bubble.agent", { timeout });
}

/** Navigate to settings view. */
export async function openSettings(page: Page) {
  await page.click('button[aria-label="Settings"]');
  await page.waitForSelector(".settings-panel, .settings-container", { timeout: 5_000 });
}

/** Navigate back to chat view. */
export async function openChat(page: Page) {
  // If we're in settings, click the settings button again to toggle back
  await page.click('button[aria-label="Settings"]');
  await page.waitForSelector(".chat-area", { timeout: 5_000 });
}

/** Click a settings tab by its text label. */
export async function clickSettingsTab(page: Page, label: string) {
  await page.click(`.settings-tab:has-text("${label}")`);
}

/** Create a new session via sidebar. */
export async function createNewSession(page: Page) {
  await page.click(".sidebar-new-btn");
}
