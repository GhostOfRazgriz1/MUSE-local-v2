import { test, expect } from "@playwright/test";
import { waitForAppLoad } from "./helpers";

test.describe("Setup flow", () => {
  // These tests only run if the app shows the setup card.
  // If a provider is already configured, they are skipped.

  test("setup card shows provider selector and API key input", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "setup") {
      test.skip(true, "Provider already configured — setup card not shown");
      return;
    }

    await expect(page.locator(".setup-card")).toBeVisible();
    await expect(page.locator(".setup-provider-select")).toBeVisible();
    await expect(page.locator(".setup-key-input")).toBeVisible();
    await expect(page.locator(".setup-save-btn")).toBeVisible();
  });

  test("provider dropdown contains expected options", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "setup") {
      test.skip(true, "Provider already configured");
      return;
    }

    const select = page.locator(".setup-provider-select");
    const options = select.locator("option");
    const values = await options.evaluateAll((els) =>
      els.map((el) => (el as HTMLOptionElement).value)
    );

    // Should include at least the main providers
    expect(values).toContain("openrouter");
    expect(values).toContain("anthropic");
    expect(values).toContain("openai");
    expect(values).toContain("gemini");
  });

  test("API key field is password type by default", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "setup") {
      test.skip(true, "Provider already configured");
      return;
    }

    const input = page.locator(".setup-key-input");
    await expect(input).toHaveAttribute("type", "password");
  });

  test("visibility toggle reveals and hides API key", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "setup") {
      test.skip(true, "Provider already configured");
      return;
    }

    const input = page.locator(".setup-key-input");
    const toggle = page.locator(".setup-key-vis-btn");

    // Initially password
    await expect(input).toHaveAttribute("type", "password");

    // Click to reveal
    await toggle.click();
    await expect(input).toHaveAttribute("type", "text");

    // Click again to hide
    await toggle.click();
    await expect(input).toHaveAttribute("type", "password");
  });

  test("save button is disabled when API key is empty", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "setup") {
      test.skip(true, "Provider already configured");
      return;
    }

    const saveBtn = page.locator(".setup-save-btn");
    // Clear any existing text
    await page.locator(".setup-key-input").fill("");
    await expect(saveBtn).toBeDisabled();
  });

  test("shows error on invalid API key submission", async ({ page }) => {
    await page.goto("/");
    const state = await waitForAppLoad(page);
    if (state !== "setup") {
      test.skip(true, "Provider already configured");
      return;
    }

    // Select a provider and enter a bogus key
    await page.locator(".setup-provider-select").selectOption("openrouter");
    await page.locator(".setup-key-input").fill("invalid-key-12345");
    await page.locator(".setup-save-btn").click();

    // Should show error message (server rejects invalid key)
    await expect(page.locator(".setup-error")).toBeVisible({ timeout: 10_000 });
  });
});
