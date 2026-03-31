import { useState, useEffect, useCallback } from "react";
import {
  IconChevronDown,
  IconEye,
  IconEyeOff,
  IconTrash,
} from "../Icons";
import { apiFetch } from "../../hooks/useApiToken";
import { SettingsSection, SettingsLoader, ModelInfo } from "./shared";

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

export default ModelsTab;
