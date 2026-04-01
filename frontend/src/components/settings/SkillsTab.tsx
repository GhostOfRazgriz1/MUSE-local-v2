import { useState, useEffect, useCallback } from "react";
import {
  IconPuzzle,
  IconCheck,
  IconZap,
  IconChevronDown,
  IconLayoutGrid,
  IconLayoutList,
  IconPlus,
  IconKey,
  IconEye,
  IconEyeOff,
  IconTrash,
} from "../Icons";
import { apiFetch } from "../../hooks/useApiToken";
import {
  SettingsSection,
  SettingsLoader,
  SkillInfo,
  CredentialSpec,
} from "./shared";

/* ─── Constants & Types ─── */

const TIER_LABELS: Record<string, { label: string; color: string }> = {
  lightweight: { label: "Lightweight", color: "var(--success)" },
  standard:    { label: "Standard",    color: "var(--accent)" },
  hardened:    { label: "Hardened",     color: "var(--warning)" },
};

type SkillView = "grid" | "list";

/* ─── Skills Tab ─── */

function SkillsTab() {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<SkillView>(() => {
    try {
      const v = localStorage.getItem("muse-skill-view");
      return v === "list" ? "list" : "grid";
    } catch { return "grid"; }
  });
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  const [defaults, setDefaults] = useState<Record<string, string>>({});
  const [categories, setCategories] = useState<Record<string, { skill_id: string; name: string }[]>>({});

  useEffect(() => {
    Promise.all([
      apiFetch("/api/skills").then((r) => r.json()),
      apiFetch("/api/skills/defaults").then((r) => r.json()),
    ])
      .then(([skillsRes, defaultsRes]) => {
        setSkills(skillsRes.skills || []);
        setDefaults(defaultsRes.defaults || {});
        setCategories(defaultsRes.categories || {});
      })
      .catch(() => setSkills([]))
      .finally(() => setLoading(false));
  }, []);

  const setDefault = useCallback(async (category: string, skillId: string) => {
    setDefaults((prev) => ({ ...prev, [category]: skillId }));
    await apiFetch(`/api/skills/defaults/${category}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill_id: skillId }),
    });
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

  const filtered = categoryFilter === "all"
    ? skills
    : skills.filter((s) => (s.category || "") === categoryFilter);
  const firstParty = filtered.filter((s) => s.is_first_party);
  const thirdParty = filtered.filter((s) => !s.is_first_party);

  // Collect unique categories from all skills (not filtered)
  const allCategories = [...new Set(skills.map((s) => s.category).filter(Boolean))].sort();

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

      {/* Category filter */}
      {allCategories.length > 1 && (
        <div className="skill-category-bar">
          <button
            className={`skill-category-chip ${categoryFilter === "all" ? "active" : ""}`}
            onClick={() => setCategoryFilter("all")}
          >
            All
          </button>
          {allCategories.map((cat) => (
            <button
              key={cat}
              className={`skill-category-chip ${categoryFilter === cat ? "active" : ""}`}
              onClick={() => setCategoryFilter(cat)}
            >
              {cat.charAt(0).toUpperCase() + cat.slice(1)}
            </button>
          ))}
        </div>
      )}

      {/* Skill defaults per category */}
      {Object.keys(categories).length > 0 && categoryFilter === "all" && (
        <SettingsSection
          title="Defaults"
          description="When multiple skills can handle the same task, the default is used."
        >
          {Object.entries(categories).map(([cat, catSkills]) => (
            catSkills.length > 1 ? (
              <div key={cat} className="skill-default-row">
                <span className="skill-default-label">
                  {cat.charAt(0).toUpperCase() + cat.slice(1)}
                </span>
                <select
                  className="settings-input"
                  style={{ width: "auto", minWidth: 160 }}
                  value={defaults[cat] || ""}
                  onChange={(e) => setDefault(cat, e.target.value)}
                >
                  <option value="">Auto (LLM decides)</option>
                  {catSkills.map((s) => (
                    <option key={s.skill_id} value={s.skill_id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : null
          ))}
        </SettingsSection>
      )}

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

export default SkillsTab;

/* ─── Skill Card (grid view) ─── */

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

/* ─── Skill List Item (list view) ─── */

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

/* ─── OAuth Setup Form (inline in credential row) ─── */

function OAuthSetupForm({
  providerName,
  onComplete: _onComplete,
  onCancel,
}: {
  providerName: string;
  onComplete: () => void;
  onCancel: () => void;
}) {
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const handleConnect = async () => {
    if (!clientId.trim() || !clientSecret.trim()) {
      setError("Both client ID and client secret are required.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      // Save client_id and client_secret to user_settings
      await apiFetch(`/api/settings/oauth.${providerName}.client_id`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: clientId.trim() }),
      });
      await apiFetch(`/api/settings/oauth.${providerName}.client_secret`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: clientSecret.trim() }),
      });
      // Redirect to OAuth start -- the backend will load credentials and redirect to provider
      window.location.href = `/api/oauth/start?provider=${providerName}`;
    } catch {
      setError("Failed to save credentials.");
      setSaving(false);
    }
  };

  const redirectUri = `${window.location.protocol}//${window.location.hostname}:${window.location.port || (window.location.protocol === "https:" ? "443" : "80")}/api/oauth/callback`;

  return (
    <div className="oauth-setup-form">
      <div className="oauth-setup-hint">
        In your provider's developer console, add this as an authorized redirect URI:
        <code className="oauth-redirect-uri" onClick={() => navigator.clipboard.writeText(redirectUri)} title="Click to copy">
          {redirectUri}
        </code>
      </div>
      <div className="oauth-setup-fields">
        <input
          type="text"
          className="settings-input"
          placeholder="Client ID"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          autoFocus
        />
        <input
          type="password"
          className="settings-input"
          placeholder="Client Secret"
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
        />
      </div>
      {error && <div className="oauth-setup-error">{error}</div>}
      <div className="oauth-setup-actions">
        <button className="btn btn-ghost btn-sm" onClick={onCancel}>Cancel</button>
        <button
          className="btn btn-primary btn-sm"
          onClick={handleConnect}
          disabled={saving}
        >
          {saving ? "Saving..." : "Authorize"}
        </button>
      </div>
    </div>
  );
}

/* ─── Single Credential Row ─── */

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
    // Derive provider name from credential id (e.g. "google_oauth" -> "google")
    const providerName = spec.id.replace(/_oauth$/, "");

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
            <>
              {!editing ? (
                <button
                  className="btn btn-primary btn-sm"
                  onClick={() => setEditing(true)}
                >
                  Connect
                </button>
              ) : (
                <OAuthSetupForm
                  providerName={providerName}
                  onComplete={() => { onUpdate(true); setEditing(false); }}
                  onCancel={() => setEditing(false)}
                />
              )}
            </>
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
