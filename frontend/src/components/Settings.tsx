import React, { useState, useEffect, useCallback } from "react";
import {
  IconSliders,
  IconCpu,
  IconKey,
  IconShield,
  IconFileText,
  IconDollarSign,
  IconEye,
  IconEyeOff,
  IconTrash,
  IconPlus,
  IconChevronDown,
  IconPuzzle,
  IconCheck,
  IconZap,
  IconLayoutGrid,
  IconLayoutList,
} from "./Icons";
import { apiFetch } from "../hooks/useApiToken";
import type { AuditEntry, ApprovalMode } from "../types/events";

/* ─── Types ─── */

interface SettingsProps {
  onBack: () => void;
}

interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  context_window: number;
  input_price: number;
  output_price: number;
}

type Tab = "general" | "skills" | "models" | "security" | "proactivity";

const TABS: { id: Tab; label: string; icon: React.ReactNode }[] = [
  { id: "general", label: "General", icon: <IconSliders size={16} /> },
  { id: "skills", label: "Skills", icon: <IconPuzzle size={16} /> },
  { id: "models", label: "Models", icon: <IconCpu size={16} /> },
  { id: "security", label: "Security", icon: <IconShield size={16} /> },
  { id: "proactivity", label: "Proactivity", icon: <IconZap size={16} /> },
];

/* ─── Main Component ─── */

export const Settings: React.FC<SettingsProps> = ({ onBack }) => {
  const [activeTab, setActiveTab] = useState<Tab>("general");

  return (
    <div className="settings">
      <div className="settings-nav">
        <div className="settings-nav-header">Settings</div>
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`settings-nav-item ${activeTab === tab.id ? "active" : ""}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.icon}
            <span>{tab.label}</span>
          </button>
        ))}
        <div className="settings-nav-footer">
          <button className="settings-nav-back" onClick={onBack}>
            Back to Chat
          </button>
        </div>
      </div>
      <div className="settings-content">
        {activeTab === "general" && <GeneralTab />}
        {activeTab === "skills" && <SkillsTab />}
        {activeTab === "models" && <ModelsTab />}
        {activeTab === "security" && <SecurityTab />}
        {activeTab === "proactivity" && <ProactivityTab />}
      </div>
    </div>
  );
};

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

function GeneralTab() {
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [fontSize, setFontSize] = useState<FontSizePreset>(getStoredFontSize);
  const [fontFamily, setFontFamily] = useState<FontFamilyPreset>(getStoredFontFamily);
  const [palette, setPalette] = useState<PalettePreset>(getStoredPalette);

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

      {/* Notifications */}
      <SettingsSection
        title="Desktop Notifications"
        description="Get alerted about reminders and suggestions even when the tab is hidden."
      >
        {typeof window !== "undefined" && "Notification" in window ? (
          <div className="settings-notification-row">
            {Notification.permission === "granted" ? (
              <div className="settings-notification-status">
                <IconCheck size={14} />
                <span>Notifications enabled. You can toggle them off in your browser settings.</span>
              </div>
            ) : Notification.permission === "denied" ? (
              <div className="settings-notification-status denied">
                <span>Notifications blocked. Enable them in your browser's site settings.</span>
              </div>
            ) : (
              <button
                className="btn btn-primary btn-sm"
                onClick={() => Notification.requestPermission()}
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

      {/* Trust Budget */}
      <SettingsSection title="Trust Budget" description="Control how much your agent can spend per day.">
        <div className="settings-field">
          <label className="settings-label">Daily Budget (USD)</label>
          <div className="settings-input-row">
            <div className="settings-input-prefix">
              <IconDollarSign size={14} />
            </div>
            <input
              type="number"
              className="settings-input has-prefix"
              value={settings["daily_budget"] ?? "1.00"}
              min="0"
              step="0.25"
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
            The agent will pause and ask before exceeding this amount.
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

/* ─── Skills Tab ─── */

interface CredentialSpec {
  id: string;
  label: string;
  type: string;
  required: boolean;
  help_url: string;
  help_text: string;
  configured?: boolean;
}

interface ActionSpec {
  id: string;
  description: string;
}

interface SkillInfo {
  skill_id: string;
  name: string;
  description: string;
  version: string;
  author: string;
  permissions: string[];
  granted_permissions: string[];
  memory_namespaces: string[];
  allowed_domains: string[];
  actions: ActionSpec[];
  credentials: CredentialSpec[];
  isolation_tier: string;
  is_first_party: boolean;
  max_tokens: number;
  timeout_seconds: number;
  installed_at: string;
  updated_at: string;
}

const TIER_LABELS: Record<string, { label: string; color: string }> = {
  lightweight: { label: "Lightweight", color: "var(--success)" },
  standard:    { label: "Standard",    color: "var(--accent)" },
  hardened:    { label: "Hardened",     color: "var(--warning)" },
};

type SkillView = "grid" | "list";

function SkillsTab() {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<SkillView>(() => {
    try {
      const v = localStorage.getItem("muse-skill-view");
      return v === "list" ? "list" : "grid";
    } catch { return "grid"; }
  });

  useEffect(() => {
    apiFetch("/api/skills")
      .then((r) => r.json())
      .then((d) => setSkills(d.skills || []))
      .catch(() => setSkills([]))
      .finally(() => setLoading(false));
  }, []);

  const handleViewChange = (v: SkillView) => {
    setView(v);
    try { localStorage.setItem("muse-skill-view", v); } catch {}
  };

  const handleUninstall = useCallback(async (skillId: string) => {
    try {
      const res = await apiFetch(`/api/skills/${skillId}`, { method: "DELETE" });
      if (res.ok) {
        setSkills((prev) => prev.filter((s) => s.skill_id !== skillId));
      }
    } catch {}
  }, []);

  if (loading) return <SettingsLoader />;

  const firstParty = skills.filter((s) => s.is_first_party);
  const thirdParty = skills.filter((s) => !s.is_first_party);

  const viewToggle = skills.length > 0 ? (
    <div className="skill-view-toggle">
      <button
        className={`skill-view-btn ${view === "grid" ? "active" : ""}`}
        onClick={() => handleViewChange("grid")}
        title="Grid view"
      >
        <IconLayoutGrid size={15} />
      </button>
      <button
        className={`skill-view-btn ${view === "list" ? "active" : ""}`}
        onClick={() => handleViewChange("list")}
        title="List view"
      >
        <IconLayoutList size={15} />
      </button>
    </div>
  ) : null;

  return (
    <div className="settings-tab">
      <div className="settings-tab-header">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <h2>Skills</h2>
          {viewToggle}
        </div>
        <p>
          Skills extend what your agent can do. {skills.length} skill
          {skills.length !== 1 ? "s" : ""} installed.
        </p>
      </div>

      {skills.length === 0 ? (
        <SettingsSection title="Installed Skills" description="No skills are installed yet.">
          <div className="settings-empty-state">
            <IconPuzzle size={24} style={{ opacity: 0.3, marginBottom: 8 }} />
            <div>No skills installed.</div>
            <div className="settings-empty-hint">
              Skills will appear here once they are loaded by the agent.
            </div>
          </div>
        </SettingsSection>
      ) : (
        <>
          {firstParty.length > 0 && (
            <SettingsSection
              title="Built-in Skills"
              description="First-party skills that ship with MUSE."
            >
              {view === "grid" ? (
                <div className="skill-grid">
                  {firstParty.map((skill) => (
                    <SkillCard key={skill.skill_id} skill={skill} />
                  ))}
                </div>
              ) : (
                <div className="skill-list">
                  {firstParty.map((skill) => (
                    <SkillListItem key={skill.skill_id} skill={skill} />
                  ))}
                </div>
              )}
            </SettingsSection>
          )}

          {thirdParty.length > 0 && (
            <SettingsSection
              title="Third-Party Skills"
              description="Community or custom-installed skills."
            >
              {view === "grid" ? (
                <div className="skill-grid">
                  {thirdParty.map((skill) => (
                    <SkillCard key={skill.skill_id} skill={skill} onUninstall={handleUninstall} />
                  ))}
                </div>
              ) : (
                <div className="skill-list">
                  {thirdParty.map((skill) => (
                    <SkillListItem key={skill.skill_id} skill={skill} onUninstall={handleUninstall} />
                  ))}
                </div>
              )}
            </SettingsSection>
          )}
        </>
      )}
    </div>
  );
}

function SkillCard({ skill, onUninstall }: { skill: SkillInfo; onUninstall?: (id: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const tier = TIER_LABELS[skill.isolation_tier] || TIER_LABELS.standard;
  const hasCredentials = skill.credentials && skill.credentials.length > 0;

  return (
    <div className="skill-card">
      <div className="skill-card-header">
        <div className="skill-card-title-row">
          <div className="skill-card-icon">
            <IconZap size={16} />
          </div>
          <div className="skill-card-name">{skill.name}</div>
          <span className="skill-card-version">v{skill.version}</span>
        </div>
        {skill.is_first_party ? (
          <span className="skill-badge first-party">Built-in</span>
        ) : onUninstall && (
          <button
            className="skill-uninstall-btn"
            onClick={() => setConfirmDelete(true)}
            title="Uninstall skill"
          >
            <IconTrash size={13} />
          </button>
        )}
      </div>

      {skill.description && (
        <div className="skill-card-desc">{skill.description}</div>
      )}

      <div className="skill-card-meta">
        {skill.author && <span className="skill-meta-item">By {skill.author}</span>}
        <span
          className="skill-tier-badge"
          style={{ color: tier.color, borderColor: tier.color + "40" }}
        >
          {tier.label}
        </span>
      </div>

      {/* Permissions */}
      {skill.permissions.length > 0 && (
        <div className="skill-card-perms">
          {skill.permissions.map((perm) => {
            const granted = skill.granted_permissions.includes(perm);
            return (
              <span
                key={perm}
                className={`skill-perm-badge ${granted ? "granted" : "pending"}`}
                title={granted ? "Granted" : "Not yet granted"}
              >
                {granted && <IconCheck size={10} />}
                {perm}
              </span>
            );
          })}
        </div>
      )}

      {/* Settings / Details toggle */}
      <button
        className="skill-card-expand"
        onClick={() => setExpanded(!expanded)}
      >
        <IconChevronDown
          size={14}
          style={{
            transform: expanded ? "rotate(180deg)" : "rotate(0)",
            transition: "transform 0.2s",
          }}
        />
        {expanded ? "Less" : hasCredentials ? "Settings" : "Details"}
      </button>

      {expanded && (
        <div className="skill-card-details">
          {/* Credential settings */}
          {hasCredentials && (
            <SkillCredentials skillId={skill.skill_id} specs={skill.credentials} />
          )}

          {/* Actions */}
          {skill.actions && skill.actions.length > 0 && (
            <div className="skill-actions">
              <div className="skill-actions-title">Actions</div>
              <div className="skill-actions-list">
                {skill.actions.map((a) => (
                  <div key={a.id} className="skill-action-item">
                    <span className="skill-action-id">{a.id}</span>
                    <span className="skill-action-desc">{a.description}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="skill-detail-row">
            <span className="skill-detail-label">Max Tokens</span>
            <span className="skill-detail-value">
              {skill.max_tokens.toLocaleString()}
            </span>
          </div>
          <div className="skill-detail-row">
            <span className="skill-detail-label">Timeout</span>
            <span className="skill-detail-value">{skill.timeout_seconds}s</span>
          </div>
          {skill.memory_namespaces.length > 0 && (
            <div className="skill-detail-row">
              <span className="skill-detail-label">Memory</span>
              <span className="skill-detail-value">
                {skill.memory_namespaces.join(", ")}
              </span>
            </div>
          )}
          {skill.allowed_domains.length > 0 && (
            <div className="skill-detail-row">
              <span className="skill-detail-label">Domains</span>
              <span className="skill-detail-value">
                {skill.allowed_domains.join(", ")}
              </span>
            </div>
          )}
        </div>
      )}

      {confirmDelete && onUninstall && (
        <div className="skill-card-confirm">
          <span>Uninstall <strong>{skill.name}</strong>?</span>
          <div className="skill-card-confirm-actions">
            <button className="btn btn-sm btn-ghost" onClick={() => setConfirmDelete(false)}>Cancel</button>
            <button className="btn btn-sm btn-danger" onClick={() => onUninstall(skill.skill_id)}>Uninstall</button>
          </div>
        </div>
      )}
    </div>
  );
}

/** List-view skill row — full-width with dividers, details button on the right. */
function SkillListItem({ skill, onUninstall }: { skill: SkillInfo; onUninstall?: (id: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const tier = TIER_LABELS[skill.isolation_tier] || TIER_LABELS.standard;
  const hasCredentials = skill.credentials && skill.credentials.length > 0;

  return (
    <div className={`skill-list-item ${expanded ? "expanded" : ""}`}>
      <div className="skill-list-row">
        <div className="skill-list-main">
          <div className="skill-list-top">
            <span className="skill-list-icon"><IconZap size={14} /></span>
            <span className="skill-list-name">{skill.name}</span>
            <span className="skill-card-version">v{skill.version}</span>
            {skill.is_first_party && (
              <span className="skill-badge first-party">Built-in</span>
            )}
            <span
              className="skill-tier-badge"
              style={{ color: tier.color, borderColor: tier.color + "40" }}
            >
              {tier.label}
            </span>
            {skill.author && (
              <span className="skill-list-author">by {skill.author}</span>
            )}
          </div>
          {skill.description && (
            <div className="skill-list-desc">{skill.description}</div>
          )}
        </div>
        {!skill.is_first_party && onUninstall && (
          <button
            className="skill-uninstall-btn"
            onClick={() => {
              if (confirm(`Uninstall ${skill.name}?`)) onUninstall(skill.skill_id);
            }}
            title="Uninstall skill"
          >
            <IconTrash size={13} />
          </button>
        )}
        <button
          className="skill-list-toggle"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Hide" : hasCredentials ? "Settings" : "Details"}
          <IconChevronDown
            size={13}
            style={{
              transform: expanded ? "rotate(180deg)" : "rotate(0)",
              transition: "transform 0.2s",
            }}
          />
        </button>
      </div>

      {expanded && (
        <div className="skill-list-details">
          {hasCredentials && (
            <SkillCredentials skillId={skill.skill_id} specs={skill.credentials} />
          )}

          <div className="skill-list-detail-grid">
            <div className="skill-detail-row">
              <span className="skill-detail-label">Max Tokens</span>
              <span className="skill-detail-value">{skill.max_tokens.toLocaleString()}</span>
            </div>
            <div className="skill-detail-row">
              <span className="skill-detail-label">Timeout</span>
              <span className="skill-detail-value">{skill.timeout_seconds}s</span>
            </div>
            {skill.memory_namespaces.length > 0 && (
              <div className="skill-detail-row">
                <span className="skill-detail-label">Memory</span>
                <span className="skill-detail-value">{skill.memory_namespaces.join(", ")}</span>
              </div>
            )}
            {skill.allowed_domains.length > 0 && (
              <div className="skill-detail-row">
                <span className="skill-detail-label">Domains</span>
                <span className="skill-detail-value">{skill.allowed_domains.join(", ")}</span>
              </div>
            )}
          </div>

          {skill.permissions.length > 0 && (
            <div className="skill-list-perms">
              <span className="skill-detail-label" style={{ marginBottom: 4, display: "block" }}>Permissions</span>
              <div className="skill-card-perms">
                {skill.permissions.map((perm) => {
                  const granted = skill.granted_permissions.includes(perm);
                  return (
                    <span
                      key={perm}
                      className={`skill-perm-badge ${granted ? "granted" : "pending"}`}
                    >
                      {granted && <IconCheck size={10} />}
                      {perm}
                    </span>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ─── Per-Skill Credential Settings ─── */

function SkillCredentials({
  skillId,
  specs,
}: {
  skillId: string;
  specs: CredentialSpec[];
}) {
  const [statuses, setStatuses] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);

  // Fetch configured status for this skill's credentials
  useEffect(() => {
    apiFetch(`/api/skills/${skillId}/settings`)
      .then((r) => r.json())
      .then((d) => {
        const map: Record<string, boolean> = {};
        for (const c of d.credentials || []) {
          map[c.id] = c.configured;
        }
        setStatuses(map);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [skillId]);

  if (loading) return <div className="skill-creds-loading">Loading...</div>;

  return (
    <div className="skill-creds">
      <div className="skill-creds-title">
        <IconKey size={13} />
        Credentials
      </div>
      {specs.map((spec) => (
        <SkillCredentialRow
          key={spec.id}
          skillId={skillId}
          spec={spec}
          configured={statuses[spec.id] || false}
          onUpdate={(configured) =>
            setStatuses((prev) => ({ ...prev, [spec.id]: configured }))
          }
        />
      ))}
    </div>
  );
}

function SkillCredentialRow({
  skillId,
  spec,
  configured,
  onUpdate,
}: {
  skillId: string;
  spec: CredentialSpec;
  configured: boolean;
  onUpdate: (configured: boolean) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [secret, setSecret] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    if (!secret.trim()) return;
    setSaving(true);
    try {
      await apiFetch(`/api/skills/${skillId}/credentials`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: spec.id, secret: secret.trim(), type: spec.type }),
      });
      onUpdate(true);
      setEditing(false);
      setSecret("");
      setShowSecret(false);
    } catch {}
    setSaving(false);
  };

  const handleDelete = async () => {
    try {
      await apiFetch(`/api/skills/${skillId}/credentials/${spec.id}`, {
        method: "DELETE",
      });
      onUpdate(false);
    } catch {}
  };

  if (spec.type === "oauth") {
    return (
      <div className="skill-cred-row">
        <div className="skill-cred-info">
          <span className="skill-cred-label">{spec.label}</span>
          {spec.help_text && (
            <span className="skill-cred-help">{spec.help_text}</span>
          )}
        </div>
        <div className="skill-cred-status">
          {configured ? (
            <>
              <span className="skill-cred-badge connected">Connected</span>
              <button
                className="settings-icon-btn danger"
                onClick={handleDelete}
                title="Disconnect"
              >
                <IconTrash size={13} />
              </button>
            </>
          ) : (
            <a
              href={`/api/oauth/start?provider=${spec.id}`}
              className="btn btn-primary btn-sm"
            >
              Connect
            </a>
          )}
        </div>
      </div>
    );
  }

  // API key type
  return (
    <div className="skill-cred-row">
      <div className="skill-cred-info">
        <span className="skill-cred-label">{spec.label}</span>
        {!editing && spec.help_text && (
          <span className="skill-cred-help">
            {spec.help_text}
            {spec.help_url && (
              <>
                {" "}
                <a
                  href={spec.help_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="md-link"
                >
                  Get a key
                </a>
              </>
            )}
          </span>
        )}
      </div>

      {!editing ? (
        <div className="skill-cred-status">
          {configured ? (
            <>
              <span className="skill-cred-badge configured">
                <IconCheck size={10} />
                Configured
              </span>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => setEditing(true)}
              >
                Change
              </button>
              <button
                className="settings-icon-btn danger"
                onClick={handleDelete}
                title="Remove key"
              >
                <IconTrash size={13} />
              </button>
            </>
          ) : (
            <button
              className="btn btn-primary btn-sm"
              onClick={() => setEditing(true)}
            >
              <IconPlus size={12} />
              Add Key
            </button>
          )}
        </div>
      ) : (
        <div className="skill-cred-form">
          <div className="settings-input-row">
            <input
              className="settings-input"
              type={showSecret ? "text" : "password"}
              placeholder="Paste API key..."
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleSave();
              }}
              autoFocus
            />
            <button
              className="settings-input-action"
              onClick={() => setShowSecret(!showSecret)}
              type="button"
              title={showSecret ? "Hide" : "Show"}
            >
              {showSecret ? <IconEyeOff size={14} /> : <IconEye size={14} />}
            </button>
          </div>
          <div className="skill-cred-form-actions">
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setEditing(false);
                setSecret("");
                setShowSecret(false);
              }}
            >
              Cancel
            </button>
            <button
              className="btn btn-primary btn-sm"
              disabled={!secret.trim() || saving}
              onClick={handleSave}
            >
              {saving ? "Saving..." : "Save"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ─── Models Tab ─── */

interface ProviderStatus {
  id: string;
  name: string;
  env_var: string;
  source: "env" | "vault" | null;
}

function ModelsTab() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [skills, setSkills] = useState<{ id: string; name: string }[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [providers, setProviders] = useState<ProviderStatus[]>([]);

  const fetchModels = useCallback(() => {
    apiFetch("/api/settings/models")
      .then((r) => r.json())
      .then((res) => setModels(res.models || []))
      .catch(() => {});
  }, []);

  const fetchProviders = useCallback(() => {
    apiFetch("/api/settings/providers")
      .then((r) => r.json())
      .then((res) => setProviders(res.providers || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    Promise.all([
      apiFetch("/api/settings/models").then((r) => r.json()),
      apiFetch("/api/settings/models/overrides").then((r) => r.json()),
      apiFetch("/api/skills").then((r) => r.json()),
      apiFetch("/api/settings").then((r) => r.json()),
      apiFetch("/api/settings/providers").then((r) => r.json()),
    ])
      .then(([modelsRes, overridesRes, skillsRes, settingsRes, providersRes]) => {
        setModels(modelsRes.models || []);
        setOverrides(overridesRes.overrides || {});
        const skillsList = Array.isArray(skillsRes) ? skillsRes : skillsRes.skills || [];
        setSkills(skillsList);
        setDefaultModel(settingsRes.settings?.default_model || "");
        setProviders(providersRes.providers || []);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const onKeyChanged = () => {
    fetchProviders();
    fetchModels();
  };

  const saveDefaultModel = async (modelId: string) => {
    setDefaultModel(modelId);
    setSaving(true);
    try {
      await apiFetch("/api/settings/default_model", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: modelId }),
      });
    } catch {}
    setSaving(false);
  };

  const saveOverride = async (skillId: string, modelId: string) => {
    setOverrides((prev) => ({ ...prev, [skillId]: modelId }));
    try {
      await apiFetch(`/api/settings/models/overrides/${skillId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId }),
      });
    } catch {}
  };

  if (loading) return <SettingsLoader />;

  const formatPrice = (price: number) => {
    if (!price) return "Free";
    if (price < 0.000001) return `$${(price * 1_000_000).toFixed(2)}/M`;
    return `$${(price * 1_000_000).toFixed(2)}/M`;
  };

  // Group models by provider, preserving insertion order.
  const grouped = models.reduce<Record<string, ModelInfo[]>>((acc, m) => {
    const key = m.provider || "other";
    (acc[key] ??= []).push(m);
    return acc;
  }, {});
  const providerNames = Object.keys(grouped);

  const ModelOptgroups = () => (
    <>
      {providerNames.map((prov) => (
        <optgroup key={prov} label={formatProviderLabel(prov)}>
          {grouped[prov].map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
        </optgroup>
      ))}
    </>
  );

  return (
    <div className="settings-tab">
      <div className="settings-tab-header">
        <h2>Models</h2>
        <p>Choose which LLM models power your agent and individual skills.</p>
      </div>

      <ProviderKeysSection providers={providers} onKeyChanged={onKeyChanged} />

      <SettingsSection title="Default Model" description="The primary model used for chat and reasoning.">
        {models.length === 0 ? (
          <div className="settings-empty-state">
            No models available. Check your API key configuration.
          </div>
        ) : (
          <div className="settings-field">
            <div className="settings-select-wrapper">
              <select
                className="settings-select"
                value={defaultModel}
                onChange={(e) => saveDefaultModel(e.target.value)}
              >
                <option value="">Auto (provider default)</option>
                <ModelOptgroups />
              </select>
              <IconChevronDown size={14} className="settings-select-icon" />
            </div>
            {defaultModel && models.find((m) => m.id === defaultModel) && (
              <div className="model-details">
                <ModelCard model={models.find((m) => m.id === defaultModel)!} />
              </div>
            )}
            {saving && <span className="settings-saving">Saving...</span>}
          </div>
        )}
      </SettingsSection>

      {skills.length > 0 && models.length > 0 && (
        <SettingsSection
          title="Per-Skill Overrides"
          description="Assign a specific model to individual skills. Leave empty to use the default."
        >
          <div className="override-list">
            {skills.map((skill) => (
              <div key={skill.id} className="override-row">
                <div className="override-skill">{skill.name || skill.id}</div>
                <div className="settings-select-wrapper override-select">
                  <select
                    className="settings-select"
                    value={overrides[skill.id] || ""}
                    onChange={(e) => saveOverride(skill.id, e.target.value)}
                  >
                    <option value="">Default</option>
                    <ModelOptgroups />
                  </select>
                  <IconChevronDown size={14} className="settings-select-icon" />
                </div>
              </div>
            ))}
          </div>
        </SettingsSection>
      )}

      {models.length > 0 && (
        <SettingsSection title="Available Models" description="All models available across your configured providers.">
          {providerNames.map((prov) => (
            <div key={prov} className="provider-group">
              <h3 className="provider-group-label">{formatProviderLabel(prov)}</h3>
              <div className="model-grid">
                {grouped[prov].map((m) => (
                  <ModelCard key={m.id} model={m} formatPrice={formatPrice} />
                ))}
              </div>
            </div>
          ))}
        </SettingsSection>
      )}
    </div>
  );
}

/* ─── Provider API Keys ─── */

function ProviderKeysSection({
  providers,
  onKeyChanged,
}: {
  providers: ProviderStatus[];
  onKeyChanged: () => void;
}) {
  if (providers.length === 0) return null;

  return (
    <SettingsSection
      title="API Keys"
      description="Add API keys to connect directly to each provider. Keys are stored in your OS keychain."
    >
      <div className="provider-keys-list">
        {providers.map((p) => (
          <ProviderKeyRow key={p.id} provider={p} onKeyChanged={onKeyChanged} />
        ))}
      </div>
    </SettingsSection>
  );
}

function ProviderKeyRow({
  provider,
  onKeyChanged,
}: {
  provider: ProviderStatus;
  onKeyChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [keyValue, setKeyValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [visible, setVisible] = useState(false);

  const save = async () => {
    if (!keyValue.trim()) return;
    setBusy(true);
    try {
      await apiFetch(`/api/settings/providers/${provider.id}/key`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: keyValue }),
      });
      setEditing(false);
      setKeyValue("");
      onKeyChanged();
    } catch {}
    setBusy(false);
  };

  const remove = async () => {
    setBusy(true);
    try {
      await apiFetch(`/api/settings/providers/${provider.id}/key`, { method: "DELETE" });
      onKeyChanged();
    } catch {}
    setBusy(false);
  };

  const label = formatProviderLabel(provider.id);

  return (
    <div className="provider-key-row">
      <div className="provider-key-info">
        <span className="provider-key-name">{label}</span>
        {provider.source === "env" && (
          <span className="provider-key-badge badge-env">env</span>
        )}
        {provider.source === "vault" && (
          <span className="provider-key-badge badge-vault">configured</span>
        )}
        {!provider.source && (
          <span className="provider-key-badge badge-none">not set</span>
        )}
      </div>

      <div className="provider-key-actions">
        {editing ? (
          <div className="provider-key-input-row">
            <div className="provider-key-input-wrapper">
              <input
                className="provider-key-input"
                type={visible ? "text" : "password"}
                placeholder={provider.env_var}
                value={keyValue}
                onChange={(e) => setKeyValue(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && save()}
                autoFocus
              />
              <button
                className="provider-key-vis-btn"
                onClick={() => setVisible((v) => !v)}
                title={visible ? "Hide" : "Show"}
              >
                {visible ? <IconEyeOff size={14} /> : <IconEye size={14} />}
              </button>
            </div>
            <button className="btn btn-sm btn-primary" onClick={save} disabled={busy || !keyValue.trim()}>
              {busy ? "Saving..." : "Save"}
            </button>
            <button className="btn btn-sm btn-ghost" onClick={() => { setEditing(false); setKeyValue(""); }}>
              Cancel
            </button>
          </div>
        ) : (
          <div className="provider-key-btn-group">
            {provider.source !== "env" && (
              <button className="btn btn-sm btn-ghost" onClick={() => setEditing(true)}>
                {provider.source ? "Update" : "Add key"}
              </button>
            )}
            {provider.source === "vault" && (
              <button className="provider-key-delete" onClick={remove} disabled={busy}>
                <IconTrash size={13} />
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  gemini: "Gemini",
  alibaba: "Alibaba / Qwen",
  deepseek: "DeepSeek",
  bytedance: "ByteDance / Doubao",
  minimax: "MiniMax",
  openrouter: "OpenRouter",
};

function formatProviderLabel(provider: string): string {
  return PROVIDER_LABELS[provider] || provider.charAt(0).toUpperCase() + provider.slice(1);
}

function ModelCard({
  model,
  formatPrice,
}: {
  model: ModelInfo;
  formatPrice?: (p: number) => string;
}) {
  const fmt = formatPrice || ((p: number) => `$${(p * 1_000_000).toFixed(2)}/M`);
  return (
    <div className="model-card">
      <div className="model-card-header">
        <div className="model-card-name">{model.name}</div>
        <span className="model-card-provider">{formatProviderLabel(model.provider)}</span>
      </div>
      <div className="model-card-id">{model.id}</div>
      <div className="model-card-stats">
        <span>Context: {(model.context_window / 1000).toFixed(0)}K</span>
        <span>In: {fmt(model.input_price)}</span>
        <span>Out: {fmt(model.output_price)}</span>
      </div>
    </div>
  );
}

/* ─── Security Tab (unified permissions + directories + audit) ─── */

interface PermGrant {
  id: number;
  skill_id: string;
  permission: string;
  risk_tier: string;
  approval_mode: ApprovalMode;
  granted_at: string;
  granted_by: string;
  session_id: string | null;
}

const MODE_LABELS: Record<ApprovalMode, string> = {
  always: "Always",
  session: "Session",
  once: "Once",
};

const MODE_COLORS: Record<ApprovalMode, string> = {
  always: "var(--accent)",
  session: "var(--warning)",
  once: "var(--text-muted)",
};

function SecurityTab() {
  const [grants, setGrants] = useState<PermGrant[]>([]);
  const [directories, setDirectories] = useState<string[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [auditFilter, setAuditFilter] = useState<"all" | "allowed" | "denied">("all");
  const [autoGrantFirstParty, setAutoGrantFirstParty] = useState(false);

  useEffect(() => {
    Promise.all([
      apiFetch("/api/permissions").then((r) => r.json()),
      apiFetch("/api/permissions/directories").then((r) => r.json()),
      apiFetch("/api/permissions/audit").then((r) => r.json()),
      apiFetch("/api/settings").then((r) => r.json()),
    ])
      .then(([permsRes, dirsRes, auditRes, settingsRes]) => {
        setGrants(permsRes.grants ?? []);
        setDirectories(dirsRes.directories ?? []);
        setAudit(Array.isArray(auditRes) ? auditRes : auditRes.entries ?? []);
        const settings = settingsRes.settings ?? {};
        setAutoGrantFirstParty(settings.auto_grant_first_party === "true");
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const handleRevokePermission = async (skillId: string, permission: string) => {
    try {
      await apiFetch(`/api/permissions/${skillId}/${permission}`, { method: "DELETE" });
      setGrants((prev) =>
        prev.filter((p) => !(p.skill_id === skillId && p.permission === permission))
      );
    } catch {}
  };

  const handleRevokeDirectory = async (path: string) => {
    try {
      await apiFetch("/api/permissions/directories", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      setDirectories((prev) => prev.filter((d) => d !== path));
    } catch {}
  };

  if (loading) return <SettingsLoader />;

  const filteredAudit =
    auditFilter === "all" ? audit : audit.filter((e) => e.result === auditFilter);

  return (
    <div className="settings-tab">
      <div className="settings-tab-header">
        <h2>Security</h2>
        <p>Manage skill permissions, file access, and review the audit trail.</p>
      </div>

      {/* ── Permission Policy ── */}
      <SettingsSection
        title="Permission Policy"
        description="Control how built-in skills receive permissions."
      >
        <label className="settings-toggle-row">
          <span>
            <strong>Auto-approve built-in skills</strong>
            <br />
            <span className="settings-toggle-hint">
              When enabled, first-party skills (Notes, Search, Files, etc.) get
              their permissions granted automatically without prompting.
              When disabled, you'll be asked to approve each permission on first use.
            </span>
          </span>
          <input
            type="checkbox"
            className="settings-toggle"
            checked={autoGrantFirstParty}
            onChange={async (e) => {
              const val = e.target.checked;
              setAutoGrantFirstParty(val);
              await apiFetch("/api/settings/auto_grant_first_party", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ value: val ? "true" : "false" }),
              });
            }}
          />
        </label>
      </SettingsSection>

      {/* ── Skill Permissions ── */}
      <SettingsSection
        title="Skill Permissions"
        description="Action permissions granted to skills at runtime."
      >
        {grants.length === 0 ? (
          <div className="settings-empty-state">
            <IconShield size={24} style={{ opacity: 0.3, marginBottom: 8 }} />
            <div>No permissions granted yet.</div>
            <div className="settings-empty-hint">
              Permissions will appear here as skills request access.
            </div>
          </div>
        ) : (
          <div className="perm-list">
            {grants.map((grant) => (
              <div key={grant.id} className="perm-list-row">
                <div className="perm-list-icon">
                  <IconShield size={14} />
                </div>
                <div className="perm-list-info">
                  <div className="perm-list-skill">
                    {grant.skill_id}
                    <span
                      className="perm-mode-badge"
                      style={{ background: MODE_COLORS[grant.approval_mode] + "1a", color: MODE_COLORS[grant.approval_mode] }}
                    >
                      {MODE_LABELS[grant.approval_mode] ?? grant.approval_mode}
                    </span>
                  </div>
                  <div className="perm-list-detail">
                    {grant.permission} &middot; {grant.risk_tier} risk &middot; Granted{" "}
                    {new Date(grant.granted_at).toLocaleDateString()}
                  </div>
                </div>
                <button
                  className="btn btn-danger btn-sm"
                  onClick={() => handleRevokePermission(grant.skill_id, grant.permission)}
                >
                  Revoke
                </button>
              </div>
            ))}
          </div>
        )}
      </SettingsSection>

      {/* ── Directory Access ── */}
      <SettingsSection
        title="Directory Access"
        description="Folders the agent can read and write files in."
      >
        {directories.length === 0 ? (
          <div className="settings-empty-state">
            <IconFileText size={24} style={{ opacity: 0.3, marginBottom: 8 }} />
            <div>No directories approved yet.</div>
            <div className="settings-empty-hint">
              The agent will ask for access when it needs to work with files.
            </div>
          </div>
        ) : (
          <div className="perm-list">
            {directories.map((dir) => (
              <div key={dir} className="perm-list-row">
                <div className="perm-list-icon">
                  <IconFileText size={14} />
                </div>
                <div className="perm-list-info">
                  <div className="perm-list-skill" style={{ fontFamily: "var(--font-mono, monospace)", fontSize: "0.85em" }}>
                    {dir}
                  </div>
                </div>
                <button
                  className="btn btn-danger btn-sm"
                  onClick={() => handleRevokeDirectory(dir)}
                >
                  Revoke
                </button>
              </div>
            ))}
          </div>
        )}
      </SettingsSection>

      {/* ── Audit Log ── */}
      <SettingsSection
        title="Audit Log"
        description="A record of permission checks performed by the system."
        action={
          <div className="audit-filters">
            {(["all", "allowed", "denied"] as const).map((f) => (
              <button
                key={f}
                className={`audit-filter-btn ${auditFilter === f ? "active" : ""}`}
                onClick={() => setAuditFilter(f)}
              >
                {f === "all" ? "All" : f === "allowed" ? "Allowed" : "Denied"}
                {f !== "all" && (
                  <span className="audit-filter-count">
                    {audit.filter((e) => e.result === f).length}
                  </span>
                )}
              </button>
            ))}
          </div>
        }
      >
        {filteredAudit.length === 0 ? (
          <div className="settings-empty-state">
            <IconFileText size={24} style={{ opacity: 0.3, marginBottom: 8 }} />
            <div>
              {auditFilter === "all"
                ? "No audit entries yet."
                : `No ${auditFilter} entries.`}
            </div>
          </div>
        ) : (
          <div className="audit-list">
            {filteredAudit.map((entry, i) => (
              <div key={i} className="audit-row">
                <div
                  className={`audit-dot ${
                    entry.result === "allowed" ? "allowed" : "denied"
                  }`}
                />
                <div className="audit-row-info">
                  <div className="audit-row-main">
                    <span className="audit-row-skill">{entry.skill_id}</span>
                    <span className="audit-row-action">{entry.action}</span>
                    <span className="audit-row-perm">{entry.permission}</span>
                  </div>
                  <div className="audit-row-time">
                    {new Date(entry.timestamp).toLocaleString()}
                  </div>
                </div>
                <span
                  className={`audit-badge ${
                    entry.result === "allowed" ? "allowed" : "denied"
                  }`}
                >
                  {entry.result}
                </span>
              </div>
            ))}
          </div>
        )}
      </SettingsSection>
    </div>
  );
}

/* ─── Shared Components ─── */

function SettingsSection({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="settings-section">
      <div className="settings-section-header">
        <div>
          <div className="settings-section-title">{title}</div>
          {description && (
            <div className="settings-section-desc">{description}</div>
          )}
        </div>
        {action}
      </div>
      <div className="settings-section-body">{children}</div>
    </div>
  );
}

function SettingsLoader() {
  return (
    <div className="settings-tab">
      <div className="settings-loader">
        <div className="settings-loader-dots">
          <span /><span /><span />
        </div>
        Loading...
      </div>
    </div>
  );
}


/* ─── Proactivity Tab ─── */

function ProactivityTab() {
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [skills, setSkills] = useState<{ skill_id: string; name: string }[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      apiFetch("/api/settings").then((r) => r.json()),
      apiFetch("/api/skills").then((r) => r.json()),
    ])
      .then(([settingsRes, skillsRes]) => {
        setSettings(settingsRes.settings || {});
        setSkills((skillsRes.skills || []).map((s: any) => ({
          skill_id: s.skill_id,
          name: s.name,
        })));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const getSetting = (key: string, def: string) => settings[key] ?? def;
  const setSetting = async (key: string, value: string) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
    await apiFetch(`/api/settings/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
  };

  const level3Skills: string[] = (() => {
    try { return JSON.parse(getSetting("proactivity.level3_skills", "[]")); }
    catch { return []; }
  })();

  const toggleLevel3Skill = async (skillId: string) => {
    const updated = level3Skills.includes(skillId)
      ? level3Skills.filter((s) => s !== skillId)
      : [...level3Skills, skillId];
    await setSetting("proactivity.level3_skills", JSON.stringify(updated));
  };

  if (loading) return <SettingsLoader />;

  return (
    <div className="settings-tab">
      <div className="settings-tab-header">
        <h2>Proactivity</h2>
        <p>Control how proactively the agent suggests actions and acts on your behalf.</p>
      </div>

      <SettingsSection
        title="Suggestion Levels"
        description="Choose what kinds of proactive behavior the agent can perform."
      >
        <label className="settings-toggle-row">
          <span>
            <strong>Post-task suggestions</strong><br />
            <span className="settings-toggle-hint">
              After completing a task, suggest a natural follow-up action.
            </span>
          </span>
          <input
            type="checkbox"
            className="settings-toggle"
            checked={getSetting("proactivity.level1", "true") === "true"}
            onChange={(e) => setSetting("proactivity.level1", e.target.checked ? "true" : "false")}
          />
        </label>

        <label className="settings-toggle-row">
          <span>
            <strong>Idle nudges</strong><br />
            <span className="settings-toggle-hint">
              When you're quiet for a while, suggest helpful actions based on context.
            </span>
          </span>
          <input
            type="checkbox"
            className="settings-toggle"
            checked={getSetting("proactivity.level2", "true") === "true"}
            onChange={(e) => setSetting("proactivity.level2", e.target.checked ? "true" : "false")}
          />
        </label>

        <label className="settings-toggle-row">
          <span>
            <strong>Autonomous actions</strong><br />
            <span className="settings-toggle-hint">
              Allow the agent to run skills in the background without asking. Only
              enabled skills (below) can be used autonomously.
            </span>
          </span>
          <input
            type="checkbox"
            className="settings-toggle"
            checked={getSetting("proactivity.level3", "false") === "true"}
            onChange={(e) => setSetting("proactivity.level3", e.target.checked ? "true" : "false")}
          />
        </label>
      </SettingsSection>

      {getSetting("proactivity.level3", "false") === "true" && (
        <SettingsSection
          title="Autonomous Skills"
          description="Select which skills the agent can run on its own."
        >
          {skills.map((s) => (
            <label key={s.skill_id} className="settings-toggle-row">
              <span>{s.name}</span>
              <input
                type="checkbox"
                className="settings-toggle"
                checked={level3Skills.includes(s.skill_id)}
                onChange={() => toggleLevel3Skill(s.skill_id)}
              />
            </label>
          ))}
        </SettingsSection>
      )}

      <SettingsSection
        title="Daily Budgets"
        description="Limit how many proactive actions the agent takes per day."
      >
        <div className="settings-budget-row">
          <span>Suggestions per day (Levels 1 &amp; 2)</span>
          <input
            type="number"
            className="settings-budget-input"
            min="0"
            max="50"
            value={getSetting("proactivity.suggestion_budget", "10")}
            onChange={(e) => setSetting("proactivity.suggestion_budget", e.target.value)}
          />
        </div>
        <div className="settings-budget-row">
          <span>Autonomous actions per day (Level 3)</span>
          <input
            type="number"
            className="settings-budget-input"
            min="0"
            max="20"
            value={getSetting("proactivity.action_budget", "3")}
            onChange={(e) => setSetting("proactivity.action_budget", e.target.value)}
          />
        </div>
      </SettingsSection>
    </div>
  );
}
