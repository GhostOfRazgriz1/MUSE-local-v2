import { useState, useEffect, useCallback } from "react";
import { IconCheck, IconCpu } from "../Icons";
import { apiFetch } from "../../hooks/useApiToken";
import { SettingsSection, SettingsLoader } from "./shared";
import { useLocale, LANGUAGES } from "../../i18n";

/* ─── General Tab ─── */

/* ─── Font Size ─── */

const FONT_SIZE_OPTIONS = [
  { value: "compact",    label: "Compact",    letterSize: 16, preview: "14px" },
  { value: "comfortable",label: "Comfortable",letterSize: 20, preview: "15px — default" },
  { value: "large",      label: "Large",      letterSize: 24, preview: "16px" },
  { value: "extra-large",label: "Extra Large",letterSize: 28, preview: "18px" },
] as const;

type FontSizePreset = (typeof FONT_SIZE_OPTIONS)[number]["value"];

export function getStoredFontSize(): FontSizePreset {
  try {
    const v = localStorage.getItem("muse-font-size");
    if (v && FONT_SIZE_OPTIONS.some((o) => o.value === v)) return v as FontSizePreset;
  } catch {}
  return "comfortable";
}

export function applyFontSize(preset: FontSizePreset) {
  const html = document.documentElement;
  if (preset === "comfortable") {
    html.removeAttribute("data-font-size");
  } else {
    html.setAttribute("data-font-size", preset);
  }
  try {
    localStorage.setItem("muse-font-size", preset);
  } catch {}
}

/* ─── Font Family ─── */

const FONT_FAMILY_OPTIONS = [
  { value: "inter",    label: "Inter",          desc: "Clean & modern",   preview: "Aa" },
  { value: "system",   label: "System",         desc: "Native OS font",   preview: "Aa" },
  { value: "dm-sans",  label: "DM Sans",        desc: "Geometric",        preview: "Aa" },
  { value: "jakarta",  label: "Jakarta",         desc: "Rounded & warm",   preview: "Aa" },
  { value: "nunito",   label: "Nunito",          desc: "Soft & friendly",  preview: "Aa" },
  { value: "mono",     label: "JetBrains Mono",  desc: "Monospace",        preview: "Aa" },
] as const;

type FontFamilyPreset = (typeof FONT_FAMILY_OPTIONS)[number]["value"];

const FONT_FAMILY_CSS: Record<FontFamilyPreset, string> = {
  inter:   "'Inter', system-ui, sans-serif",
  system:  "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
  "dm-sans": "'DM Sans', system-ui, sans-serif",
  jakarta: "'Plus Jakarta Sans', system-ui, sans-serif",
  nunito:  "'Nunito', system-ui, sans-serif",
  mono:    "'JetBrains Mono', 'Fira Code', Consolas, monospace",
};

export function getStoredFontFamily(): FontFamilyPreset {
  try {
    const v = localStorage.getItem("muse-font-family");
    if (v && FONT_FAMILY_OPTIONS.some((o) => o.value === v)) return v as FontFamilyPreset;
  } catch {}
  return "inter";
}

export function applyFontFamily(preset: FontFamilyPreset) {
  const html = document.documentElement;
  if (preset === "inter") {
    html.removeAttribute("data-font-family");
  } else {
    html.setAttribute("data-font-family", preset);
  }
  try {
    localStorage.setItem("muse-font-family", preset);
  } catch {}
}

/* ─── Color Palette ─── */

type PaletteGroup = "dark" | "light" | "comfort";

interface PaletteOption {
  value: string;
  label: string;
  desc: string;
  group: PaletteGroup;
  swatches: { bg: string; surface: string; elevated: string; accent: string };
}

const PALETTE_GROUPS: { key: PaletteGroup; label: string }[] = [
  { key: "dark", label: "Dark" },
  { key: "comfort", label: "Easy on Eyes" },
  { key: "light", label: "Light" },
];

const COLOR_PALETTE_OPTIONS: readonly PaletteOption[] = [
  { value: "midnight",   label: "Midnight",    desc: "Default blue-tinted",  group: "dark",    swatches: { bg: "#0b0d13", surface: "#151820", elevated: "#1b1e2b", accent: "#5b8def" } },
  { value: "slate",      label: "Slate",       desc: "Neutral zinc gray",    group: "dark",    swatches: { bg: "#09090b", surface: "#27272a", elevated: "#3f3f46", accent: "#3b82f6" } },
  { value: "terminal",   label: "Terminal",     desc: "Dev tools green",      group: "dark",    swatches: { bg: "#020617", surface: "#0F172A", elevated: "#1E293B", accent: "#22C55E" } },
  { value: "aurora",     label: "Aurora",       desc: "Indigo & orange",      group: "dark",    swatches: { bg: "#0F0F23", surface: "#1E1B4B", elevated: "#27273B", accent: "#F97316" } },
  { value: "cherry",     label: "Cherry",       desc: "Cinema dark & rose",   group: "dark",    swatches: { bg: "#030305", surface: "#0F0F23", elevated: "#181818", accent: "#E11D48" } },
  { value: "nebula",     label: "Nebula",       desc: "Deep purple",          group: "dark",    swatches: { bg: "#0F0F23", surface: "#1E1B4B", elevated: "#27273B", accent: "#A78BFA" } },
  { value: "nord",       label: "Nord",         desc: "Classic arctic",       group: "dark",    swatches: { bg: "#2e3440", surface: "#3b4252", elevated: "#434c5e", accent: "#88c0d0" } },
  { value: "catppuccin", label: "Catppuccin",   desc: "Pastel mocha",         group: "dark",    swatches: { bg: "#1e1e2e", surface: "#313244", elevated: "#45475a", accent: "#89b4fa" } },
  { value: "dracula",    label: "Dracula",      desc: "The classic editor",   group: "dark",    swatches: { bg: "#282a36", surface: "#44475a", elevated: "#4d5066", accent: "#bd93f9" } },
  { value: "ember",      label: "Ember",        desc: "Warm dark, low blue",  group: "comfort", swatches: { bg: "#1a1614", surface: "#282320", elevated: "#312b27", accent: "#d4a574" } },
  { value: "dusk",       label: "Dusk",         desc: "Soft gray, gentle",    group: "comfort", swatches: { bg: "#1e2028", surface: "#2c2e38", elevated: "#363842", accent: "#8a98cc" } },
  { value: "sepia",      label: "Sepia",        desc: "Warm cream paper",     group: "comfort", swatches: { bg: "#FAF6EE", surface: "#FFFFFF", elevated: "#F4EFDF", accent: "#A07030" } },
  { value: "sage",       label: "Sage",         desc: "Calm nature green",    group: "comfort", swatches: { bg: "#F3F5F0", surface: "#FFFFFF", elevated: "#EAEDE6", accent: "#4B7A5C" } },
  { value: "daylight",   label: "Daylight",     desc: "Clean white & blue",   group: "light",   swatches: { bg: "#F8FAFC", surface: "#FFFFFF", elevated: "#F1F5F9", accent: "#2563EB" } },
  { value: "paper",      label: "Paper",        desc: "Warm & minimal",       group: "light",   swatches: { bg: "#FAFAF9", surface: "#FFFFFF", elevated: "#F5F5F4", accent: "#1C1917" } },
  { value: "lavender",   label: "Lavender",     desc: "Soft indigo tint",     group: "light",   swatches: { bg: "#F5F3FF", surface: "#FFFFFF", elevated: "#EDE9FE", accent: "#6366F1" } },
] as const;

type PalettePreset = (typeof COLOR_PALETTE_OPTIONS)[number]["value"];

export function getStoredPalette(): PalettePreset {
  try {
    const v = localStorage.getItem("muse-palette");
    if (v && COLOR_PALETTE_OPTIONS.some((o) => o.value === v)) return v as PalettePreset;
  } catch {}
  return "midnight";
}

export function applyPalette(preset: PalettePreset) {
  const html = document.documentElement;
  if (preset === "midnight") {
    html.removeAttribute("data-palette");
  } else {
    html.setAttribute("data-palette", preset);
  }
  try {
    localStorage.setItem("muse-palette", preset);
  } catch {}
}

export default function GeneralTab() {
  const { locale, setLocale, t } = useLocale();
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [fontSize, setFontSize] = useState<FontSizePreset>(getStoredFontSize);
  const [fontFamily, setFontFamily] = useState<FontFamilyPreset>(getStoredFontFamily);
  const [palette, setPalette] = useState<PalettePreset>(getStoredPalette);
  const [notifPermission, setNotifPermission] = useState<NotificationPermission>(
    typeof window !== "undefined" && "Notification" in window ? Notification.permission : "default"
  );

  useEffect(() => {
    apiFetch("/api/settings")
      .then((r) => r.json())
      .then((d) => setSettings(d.settings || {}))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const saveSetting = useCallback(
    async (key: string, value: string) => {
      setSaving(key);
      try {
        await apiFetch(`/api/settings/${key}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value }),
        });
        setSettings((prev) => ({ ...prev, [key]: value }));
      } catch {
        // silent
      }
      setSaving(null);
    },
    []
  );

  const handleFontSizeChange = (preset: FontSizePreset) => {
    setFontSize(preset);
    applyFontSize(preset);
  };

  const handleFontFamilyChange = (preset: FontFamilyPreset) => {
    setFontFamily(preset);
    applyFontFamily(preset);
  };

  const handlePaletteChange = (preset: PalettePreset) => {
    setPalette(preset);
    applyPalette(preset);
  };

  if (loading) return <SettingsLoader />;

  return (
    <div className="settings-tab">
      <div className="settings-tab-header">
        <h2>General</h2>
        <p>Core preferences and budget controls for your agent.</p>
      </div>

      {/* Appearance */}
      <SettingsSection title="Appearance" description="Adjust the interface to your preference.">
        <div className="settings-field">
          <label className="settings-label">Font Size</label>
          <div className="font-size-picker">
            {FONT_SIZE_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                className={`font-size-option ${fontSize === opt.value ? "active" : ""}`}
                onClick={() => handleFontSizeChange(opt.value)}
              >
                <span className="font-size-option-letter" style={{ fontSize: opt.letterSize }}>Aa</span>
                <span className="font-size-option-label">{opt.label}</span>
                <span className="font-size-option-preview">{opt.preview}</span>
              </button>
            ))}
          </div>
          <div className="settings-hint">
            Scales all text, buttons, cards, and dialogs proportionally.
          </div>
        </div>
        <div className="settings-field">
          <label className="settings-label">Font Family</label>
          <div className="font-family-picker">
            {FONT_FAMILY_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                className={`font-family-option ${fontFamily === opt.value ? "active" : ""}`}
                onClick={() => handleFontFamilyChange(opt.value)}
              >
                <span className="font-family-preview" style={{ fontFamily: FONT_FAMILY_CSS[opt.value] }}>{opt.preview}</span>
                <span className="font-family-name">{opt.label}</span>
                <span className="font-family-desc">{opt.desc}</span>
              </button>
            ))}
          </div>
          <div className="settings-hint">
            Changes apply immediately and are saved to this browser.
          </div>
        </div>
        <div className="settings-field">
          <label className="settings-label">Color Palette</label>
          {PALETTE_GROUPS.map((group) => {
            const palettes = COLOR_PALETTE_OPTIONS.filter((o) => o.group === group.key);
            if (palettes.length === 0) return null;
            return (
              <div key={group.key} className="palette-group">
                <div className="palette-group-label">{group.label}</div>
                <div className="palette-picker">
                  {palettes.map((opt) => (
                    <button
                      key={opt.value}
                      className={`palette-option ${palette === opt.value ? "active" : ""}`}
                      onClick={() => handlePaletteChange(opt.value as PalettePreset)}
                    >
                      <div className="palette-swatches">
                        <span className="palette-swatch" style={{ background: opt.swatches.bg }} />
                        <span className="palette-swatch" style={{ background: opt.swatches.surface }} />
                        <span className="palette-swatch" style={{ background: opt.swatches.elevated }} />
                        <span className="palette-swatch-accent" style={{ background: opt.swatches.accent }} />
                      </div>
                      <span className="palette-name">{opt.label}</span>
                      <span className="palette-desc">{opt.desc}</span>
                    </button>
                  ))}
                </div>
              </div>
            );
          })}
          <div className="settings-hint">
            Sets the overall color scheme. Each palette includes matching backgrounds, accent, and border colors.
          </div>
        </div>
      </SettingsSection>

      {/* Workspace */}
      <SettingsSection title="Workspace" description="Default folder for files the agent creates.">
        <div className="settings-field">
          <label className="settings-label">Workspace Directory</label>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input
              className="settings-input"
              style={{ flex: 1 }}
              value={settings["workspace.directory"] ?? ""}
              placeholder={
                (typeof window !== "undefined" && navigator.platform?.startsWith("Win")
                  ? "C:\\Users\\...\\Documents\\MUSE"
                  : "~/Documents/MUSE") + " (default)"
              }
              onChange={(e) =>
                setSettings((prev) => ({ ...prev, "workspace.directory": e.target.value }))
              }
            />
            <button
              className="btn btn-sm btn-ghost"
              onClick={async () => {
                try {
                  const res = await apiFetch("/api/files/pick-folder", { method: "POST" });
                  if (res.ok) {
                    const data = await res.json();
                    if (data.path) {
                      setSettings((prev) => ({ ...prev, "workspace.directory": data.path }));
                      saveSetting("workspace.directory", data.path);
                    }
                  }
                } catch {}
              }}
            >
              Browse
            </button>
            <button
              className="btn btn-primary btn-sm"
              onClick={() => saveSetting("workspace.directory", settings["workspace.directory"] || "")}
              disabled={saving === "workspace.directory"}
            >
              {saving === "workspace.directory" ? "Saving..." : "Save"}
            </button>
          </div>
          <div className="settings-hint">
            Leave blank to use the default (Documents/MUSE). The agent can read and write here without asking.
          </div>
        </div>
      </SettingsSection>

      {/* Language */}
      <SettingsSection title={t("settings_language")} description={t("settings_language_desc")}>
        <div className="settings-field">
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              className="settings-select"
              value={locale}
              onChange={(e) => setLocale(e.target.value)}
            >
              {LANGUAGES.map((l) => (
                <option key={l.code} value={l.code}>{l.label}</option>
              ))}
            </select>
          </div>
          <div className="settings-hint">
            {t("settings_language_hint")}
          </div>
        </div>
      </SettingsSection>

      {/* Notifications */}
      <SettingsSection
        title="Desktop Notifications"
        description="Get alerted about reminders and suggestions even when the tab is hidden."
      >
        {typeof window !== "undefined" && "Notification" in window ? (
          <div className="settings-notification-row">
            {notifPermission === "granted" ? (
              <div className="settings-notification-status">
                <IconCheck size={14} />
                <span>Notifications enabled. You can toggle them off in your browser settings.</span>
              </div>
            ) : notifPermission === "denied" ? (
              <div className="settings-notification-status denied">
                <span>Notifications blocked. Enable them in your browser's site settings.</span>
              </div>
            ) : (
              <button
                className="btn btn-primary btn-sm"
                onClick={async () => {
                  const result = await Notification.requestPermission();
                  setNotifPermission(result);
                }}
              >
                Enable desktop notifications
              </button>
            )}
          </div>
        ) : (
          <div className="settings-notification-status">
            <span>Desktop notifications are not supported in this browser.</span>
          </div>
        )}
      </SettingsSection>

      {/* Daily Token Budget */}
      <SettingsSection title="Daily Token Limit" description="Limit how many AI tokens the agent can use per day.">
        <div className="settings-field">
          <label className="settings-label">Maximum tokens per day</label>
          <div className="settings-input-row">
            <div className="settings-input-prefix">
              <IconCpu size={14} />
            </div>
            <input
              type="number"
              className="settings-input has-prefix"
              value={settings["daily_budget"] ?? "50000"}
              min="0"
              step="10000"
              onChange={(e) =>
                setSettings((p) => ({ ...p, daily_budget: e.target.value }))
              }
              onBlur={(e) => saveSetting("daily_budget", e.target.value)}
            />
            {saving === "daily_budget" && (
              <span className="settings-saving">Saving...</span>
            )}
          </div>
          <div className="settings-hint">
            The agent will pause and ask before exceeding this limit. A typical message uses 500–2,000 tokens.
          </div>
        </div>
      </SettingsSection>

      {/* Agent Behavior */}
      <SettingsSection title="Agent Behavior" description="Tune how the agent interacts with you.">
        <div className="settings-field">
          <label className="settings-label">Autonomy Level</label>
          <select
            className="settings-select"
            value={settings["autonomy_level"] ?? "supervised"}
            onChange={(e) => {
              setSettings((p) => ({ ...p, autonomy_level: e.target.value }));
              saveSetting("autonomy_level", e.target.value);
            }}
          >
            <option value="ask_always">Ask Always</option>
            <option value="supervised">Supervised</option>
            <option value="autonomous">Autonomous</option>
          </select>
          <div className="settings-hint">
            {(settings["autonomy_level"] ?? "supervised") === "ask_always" &&
              "The agent will ask permission before every action."}
            {(settings["autonomy_level"] ?? "supervised") === "supervised" &&
              "The agent asks for high-risk actions but handles low-risk ones automatically."}
            {settings["autonomy_level"] === "autonomous" &&
              "The agent acts independently within its trust budget. Use with caution."}
          </div>
        </div>

        <div className="settings-field">
          <label className="settings-label">Response Style</label>
          <select
            className="settings-select"
            value={settings["response_style"] ?? "balanced"}
            onChange={(e) => {
              setSettings((p) => ({ ...p, response_style: e.target.value }));
              saveSetting("response_style", e.target.value);
            }}
          >
            <option value="concise">Concise</option>
            <option value="balanced">Balanced</option>
            <option value="detailed">Detailed</option>
          </select>
          <div className="settings-hint">
            How verbose the agent's responses should be.
          </div>
        </div>
      </SettingsSection>
    </div>
  );
}
