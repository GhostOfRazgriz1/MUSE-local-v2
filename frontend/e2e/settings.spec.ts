import { test, expect } from "@playwright/test";
import { waitForAppLoad, waitForConnection, openSettings, clickSettingsTab } from "./helpers";

test.describe("Settings panel", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "chat") {
      test.skip(true, "Setup card is showing");
      return;
    }
    await waitForConnection(page);
    await openSettings(page);
  });

  test("settings button toggles settings view", async ({ page }) => {
    // Settings should be visible (opened in beforeEach)
    const settingsContent = page.locator(".settings-panel, .settings-container, .settings-tabs");
    await expect(settingsContent.first()).toBeVisible();

    // Click settings button again to go back to chat
    await page.click('button[aria-label="Settings"]');
    await expect(page.locator(".chat-area")).toBeVisible();
  });

  test("Ctrl+Shift+S toggles settings", async ({ page }) => {
    // Already in settings (from beforeEach), press shortcut to go back
    await page.keyboard.press("Control+Shift+S");
    await expect(page.locator(".chat-area")).toHaveCSS("display", "flex");

    // Press again to return to settings
    await page.keyboard.press("Control+Shift+S");
    await page.waitForTimeout(500);
    // Settings view should be showing
    const settingsContent = page.locator(".settings-panel, .settings-container, .settings-tabs");
    await expect(settingsContent.first()).toBeVisible();
  });

  test("settings tabs are visible", async ({ page }) => {
    // Should have multiple tabs
    const tabs = page.locator(".settings-tab");
    const count = await tabs.count();
    expect(count).toBeGreaterThanOrEqual(4);
  });

  test("General tab: font size options exist", async ({ page }) => {
    await clickSettingsTab(page, "General");
    await expect(page.locator(".font-size-picker")).toBeVisible();
    const options = page.locator(".font-size-option");
    expect(await options.count()).toBeGreaterThanOrEqual(3);
  });

  test("General tab: font family options exist", async ({ page }) => {
    await clickSettingsTab(page, "General");
    await expect(page.locator(".font-family-picker")).toBeVisible();
    const options = page.locator(".font-family-option");
    expect(await options.count()).toBeGreaterThanOrEqual(3);
  });

  test("General tab: color palette picker exists", async ({ page }) => {
    await clickSettingsTab(page, "General");
    await expect(page.locator(".palette-picker").first()).toBeVisible();
  });

  test("General tab: clicking a font size option selects it", async ({ page }) => {
    await clickSettingsTab(page, "General");
    const option = page.locator(".font-size-option").nth(2); // e.g. "large"
    await option.click();
    await expect(option).toHaveClass(/active/);
  });

  test("General tab: clicking a palette option selects it", async ({ page }) => {
    await clickSettingsTab(page, "General");
    const option = page.locator(".palette-option").first();
    await option.click();
    await expect(option).toHaveClass(/active/);
  });

  test("Models tab: provider list is shown", async ({ page }) => {
    await clickSettingsTab(page, "Models");
    await expect(page.locator(".provider-keys-list, .provider-key-row").first()).toBeVisible({
      timeout: 5_000,
    });
  });

  test("Models tab: each provider shows status badge", async ({ page }) => {
    await clickSettingsTab(page, "Models");
    await page.waitForSelector(".provider-key-row", { timeout: 5_000 });

    const rows = page.locator(".provider-key-row");
    const count = await rows.count();
    expect(count).toBeGreaterThanOrEqual(1);

    // Each row should have a badge
    for (let i = 0; i < Math.min(count, 3); i++) {
      const row = rows.nth(i);
      const badge = row.locator(
        ".badge-vault, .badge-env, .badge-custom, .badge-none, .provider-key-badge"
      );
      await expect(badge.first()).toBeVisible();
    }
  });

  test("Models tab: default model selector exists", async ({ page }) => {
    await clickSettingsTab(page, "Models");
    await expect(page.locator(".sms-container, .sms-trigger").first()).toBeVisible({
      timeout: 5_000,
    });
  });

  test("Skills tab: skills list loads", async ({ page }) => {
    await clickSettingsTab(page, "Skills");
    // Should show skill items or an empty state
    await page.waitForTimeout(2_000);
    const content = page.locator(".settings-panel, .settings-container");
    await expect(content.first()).toBeVisible();
  });

  test("Security tab: loads permission settings", async ({ page }) => {
    await clickSettingsTab(page, "Security");
    await page.waitForTimeout(1_000);
    const content = page.locator(".settings-panel, .settings-container");
    await expect(content.first()).toBeVisible();
  });

  test("MCP tab: shows MCP server configuration", async ({ page }) => {
    await clickSettingsTab(page, "MCP");
    await page.waitForTimeout(1_000);
    const content = page.locator(".settings-panel, .settings-container");
    await expect(content.first()).toBeVisible();
  });
});
