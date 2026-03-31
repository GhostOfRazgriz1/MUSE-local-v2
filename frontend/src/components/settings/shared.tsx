import React from "react";
import type { ApprovalMode } from "../../types/events";

/* ─── Shared Types ─── */

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  context_window: number;
  input_price: number;
  output_price: number;
}

export interface CredentialSpec {
  id: string;
  label: string;
  type: string;
  required: boolean;
  help_url: string;
  help_text: string;
  configured?: boolean;
}

export interface ActionSpec {
  id: string;
  description: string;
}

export interface SkillInfo {
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
  category: string;
  isolation_tier: string;
  is_first_party: boolean;
  max_tokens: number;
  timeout_seconds: number;
  installed_at: string;
  updated_at: string;
}

export type { ApprovalMode };

/* ─── Shared Components ─── */

export function SettingsSection({
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

export function SettingsLoader() {
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
