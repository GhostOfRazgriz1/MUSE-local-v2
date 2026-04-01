import { useState, useEffect, useCallback } from "react";
import {
  IconChevronDown,
  IconChevronRight,
  IconEye,
  IconEyeOff,
  IconTrash,
  IconCheck,
  IconAlertCircle,
} from "../Icons";
import { apiFetch } from "../../hooks/useApiToken";
import { SettingsSection, SettingsLoader, ModelInfo } from "./shared";

/* ─── Models Tab ─── */

interface ProviderStatus {
  id: string;
  name: string;
  env_var: string;
  source: "env" | "vault" | null;
  is_custom?: boolean;
  base_url?: string;
  api_style?: string;
}

function ModelsTab() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [skills, setSkills] = useState<{ id: string; name: string }[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [advancedOpen, setAdvancedOpen] = useState(false);

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
      .catch(() => setLoadError("Failed to load settings. Check your connection."))
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
  if (loadError) {
    return (
      <div className="settings-tab">
        <div className="settings-error-state">
          <IconAlertCircle size={20} />
          <span>{loadError}</span>
          <button className="btn btn-sm btn-primary" onClick={() => window.location.reload()}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  const formatPrice = (price: number) => {
    if (!price) return "Free";
    return `$${(price * 1_000_000).toFixed(2)}/M`;
  };

  // Group models by provider
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

  // Find the active provider (the one with a key configured)
  const activeProviders = providers.filter((p) => p.source !== null);
  const inactiveProviders = providers.filter((p) => p.source === null);

  // Models from active providers only (for simple view dropdown)
  const activeProviderIds = new Set(activeProviders.map((p) => p.id));
  const activeModels = models.filter((m) => activeProviderIds.has(m.provider));

  return (
    <div className="settings-tab">
      <div className="settings-tab-header">
        <h2>Models</h2>
        <p>Configure your AI provider and choose which model powers your agent.</p>
      </div>

      {/* ═══ SIMPLE VIEW ═══ */}

      {/* All providers */}
      <SettingsSection
        title="Providers"
        description="Connect your AI providers. Keys are stored securely in your OS keychain."
      >
        <div className="provider-keys-list">
          {[...activeProviders, ...inactiveProviders].map((p) => (
            <ProviderKeyRow
              key={p.id}
              provider={p}
              onKeyChanged={onKeyChanged}
              onDeleted={p.is_custom ? onKeyChanged : undefined}
            />
          ))}
        </div>
        <AddCustomProviderForm onAdded={onKeyChanged} />
      </SettingsSection>

      {/* Default model — only from active providers */}
      <SettingsSection title="Model" description="The AI model used for chat and tasks.">
        {activeModels.length === 0 ? (
          <div className="settings-empty-state">
            No models available. Connect a provider above first.
          </div>
        ) : (
          <div className="settings-field">
            <div className="settings-select-wrapper">
              <select
                className="settings-select"
                value={defaultModel}
                onChange={(e) => saveDefaultModel(e.target.value)}
              >
                <option value="">Auto (recommended)</option>
                {providerNames
                  .filter((prov) => activeProviderIds.has(prov))
                  .map((prov) => (
                    <optgroup key={prov} label={formatProviderLabel(prov)}>
                      {grouped[prov].map((m) => (
                        <option key={m.id} value={m.id}>{m.name}</option>
                      ))}
                    </optgroup>
                  ))}
              </select>
              <IconChevronDown size={14} className="settings-select-icon" />
            </div>
            {defaultModel && models.find((m) => m.id === defaultModel) && (
              <div className="model-details">
                <ModelCard model={models.find((m) => m.id === defaultModel)!} formatPrice={formatPrice} />
              </div>
            )}
            {saving && <span className="settings-saving">Saving...</span>}
          </div>
        )}
      </SettingsSection>

      {/* ═══ ADVANCED TOGGLE ═══ */}
      <div className="settings-advanced-toggle">
        <button
          className="settings-advanced-btn"
          onClick={() => setAdvancedOpen(!advancedOpen)}
        >
          {advancedOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
          <span>Advanced settings</span>
        </button>
      </div>

      {advancedOpen && (
        <div className="settings-advanced-panel">
          {/* Per-skill overrides */}
          {skills.length > 0 && models.length > 0 && (
            <SettingsSection
              title="Per-Skill Model Overrides"
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

          {/* Model gallery */}
          {models.length > 0 && (
            <SettingsSection title="All Available Models" description="Models available across your configured providers.">
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
      )}
    </div>
  );
}

/* ─── Provider API Key Row ─── */

function ProviderKeyRow({
  provider,
  onKeyChanged,
  onDeleted,
}: {
  provider: ProviderStatus;
  onKeyChanged: () => void;
  onDeleted?: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [keyValue, setKeyValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [visible, setVisible] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);

  const save = async () => {
    if (!keyValue.trim()) return;
    setBusy(true);
    setError("");
    setSuccess(false);
    try {
      const res = await apiFetch(`/api/settings/providers/${provider.id}/key`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: keyValue }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail || "Failed to save key");
        setBusy(false);
        return;
      }
      setSuccess(true);
      setTimeout(() => {
        setEditing(false);
        setKeyValue("");
        setSuccess(false);
        onKeyChanged();
      }, 600);
    } catch {
      setError("Connection failed");
    }
    setBusy(false);
  };

  const remove = async () => {
    setBusy(true);
    try {
      if (provider.is_custom) {
        // Delete the custom provider definition entirely
        await apiFetch(`/api/settings/providers/custom/${provider.id}`, { method: "DELETE" });
      } else {
        await apiFetch(`/api/settings/providers/${provider.id}/key`, { method: "DELETE" });
      }
      onKeyChanged();
      if (onDeleted) onDeleted();
    } catch {}
    setBusy(false);
  };

  const label = provider.is_custom ? provider.name : formatProviderLabel(provider.id);

  return (
    <div className="provider-key-row">
      <div className="provider-key-info">
        <span className="provider-key-name">{label}</span>
        {provider.is_custom && (
          <span className="provider-key-badge badge-custom">custom</span>
        )}
        {provider.source === "env" && (
          <span className="provider-key-badge badge-env">env</span>
        )}
        {provider.source === "vault" && (
          <span className="provider-key-badge badge-vault">
            <IconCheck size={10} /> connected
          </span>
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
                className={`provider-key-input ${error ? "input-error" : ""}`}
                type={visible ? "text" : "password"}
                placeholder={provider.env_var}
                value={keyValue}
                onChange={(e) => { setKeyValue(e.target.value); setError(""); }}
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
            <button
              className="btn btn-sm btn-primary"
              onClick={save}
              disabled={busy || !keyValue.trim()}
            >
              {busy ? "Saving..." : success ? "Connected!" : "Save"}
            </button>
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => { setEditing(false); setKeyValue(""); setError(""); }}
            >
              Cancel
            </button>
            {error && (
              <span className="provider-key-error">
                <IconAlertCircle size={12} /> {error}
              </span>
            )}
          </div>
        ) : (
          <div className="provider-key-btn-group">
            {provider.source !== "env" && (
              <button className="btn btn-sm btn-ghost" onClick={() => setEditing(true)}>
                {provider.source ? "Update" : "Add key"}
              </button>
            )}
            {(provider.source === "vault" || provider.is_custom) && (
              <button className="provider-key-delete" onClick={remove} disabled={busy} title={provider.is_custom ? "Remove provider" : "Remove key"}>
                <IconTrash size={13} />
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── Add Custom Provider Form ─── */

function AddCustomProviderForm({ onAdded }: { onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiStyle, setApiStyle] = useState<"openai" | "anthropic">("openai");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const reset = () => {
    setName("");
    setBaseUrl("");
    setApiKey("");
    setApiStyle("openai");
    setError("");
    setOpen(false);
  };

  const submit = async () => {
    if (!name.trim() || !baseUrl.trim()) {
      setError("Name and Base URL are required");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await apiFetch("/api/settings/providers/custom", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          base_url: baseUrl.trim(),
          api_key: apiKey.trim(),
          api_style: apiStyle,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail || "Failed to add provider");
        setBusy(false);
        return;
      }
      reset();
      onAdded();
    } catch {
      setError("Connection failed");
    }
    setBusy(false);
  };

  if (!open) {
    return (
      <button className="btn btn-sm btn-ghost custom-provider-add-btn" onClick={() => setOpen(true)}>
        + Add custom provider
      </button>
    );
  }

  return (
    <div className="custom-provider-form">
      <div className="custom-provider-field">
        <label className="custom-provider-label">Name</label>
        <input
          className="settings-input"
          type="text"
          placeholder="e.g. My Ollama, Together AI"
          value={name}
          onChange={(e) => { setName(e.target.value); setError(""); }}
        />
      </div>
      <div className="custom-provider-field">
        <label className="custom-provider-label">Base URL</label>
        <input
          className="settings-input"
          type="text"
          placeholder="e.g. http://localhost:11434/v1"
          value={baseUrl}
          onChange={(e) => { setBaseUrl(e.target.value); setError(""); }}
        />
      </div>
      <div className="custom-provider-field">
        <label className="custom-provider-label">API Key <span style={{ opacity: 0.5 }}>(optional for local models)</span></label>
        <input
          className="settings-input"
          type="password"
          placeholder="sk-..."
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
      </div>
      <div className="custom-provider-field">
        <label className="custom-provider-label">API Format</label>
        <div className="settings-select-wrapper">
          <select
            className="settings-select"
            value={apiStyle}
            onChange={(e) => setApiStyle(e.target.value as "openai" | "anthropic")}
          >
            <option value="openai">OpenAI-compatible (most providers)</option>
            <option value="anthropic">Anthropic</option>
          </select>
          <IconChevronDown size={14} className="settings-select-icon" />
        </div>
      </div>
      {error && (
        <span className="provider-key-error">
          <IconAlertCircle size={12} /> {error}
        </span>
      )}
      <div className="custom-provider-actions">
        <button className="btn btn-sm btn-primary" onClick={submit} disabled={busy || !name.trim() || !baseUrl.trim()}>
          {busy ? "Adding..." : "Add provider"}
        </button>
        <button className="btn btn-sm btn-ghost" onClick={reset}>Cancel</button>
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
