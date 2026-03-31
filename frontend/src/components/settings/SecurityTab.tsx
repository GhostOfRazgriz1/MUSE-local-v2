import { useState, useEffect } from "react";
import { IconShield, IconFileText } from "../Icons";
import { apiFetch } from "../../hooks/useApiToken";
import { SettingsSection, SettingsLoader } from "./shared";
import type { AuditEntry, ApprovalMode } from "../../types/events";

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

export default SecurityTab;
