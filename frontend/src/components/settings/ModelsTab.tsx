import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  IconChevronDown,
  IconChevronRight,
  IconX,
  IconCheck,
  IconAlertCircle,
} from "../Icons";
import { apiFetch } from "../../hooks/useApiToken";
import { SettingsSection, SettingsLoader, ModelInfo } from "./shared";

/* ─── Models Tab (Local Only) ─── */

function ModelsTab() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [skills, setSkills] = useState<{ id: string; name: string }[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [gallerySearch, setGallerySearch] = useState("");
  const [serverStatus, setServerStatus] = useState<"checking" | "online" | "offline">("checking");
  const [connRuntime, setConnRuntime] = useState("ollama");
  const [connAddress, setConnAddress] = useState("localhost");
  const [connPort, setConnPort] = useState("11434");
  const [connEditing, setConnEditing] = useState(false);
  const [connBusy, setConnBusy] = useState(false);
  const [connError, setConnError] = useState("");
  const [connSuccess, setConnSuccess] = useState(false);
  const [maxWorkers, setMaxWorkers] = useState(2);

  const fetchModels = useCallback(() => {
    apiFetch("/api/settings/models")
      .then((r) => r.json())
      .then((res) => {
        const m = res.models || [];
        setModels(m);
        setServerStatus(m.length > 0 ? "online" : "offline");
      })
      .catch(() => setServerStatus("offline"));
  }, []);

  useEffect(() => {
    Promise.all([
      apiFetch("/api/settings/models").then((r) => r.json()),
      apiFetch("/api/settings/models/overrides").then((r) => r.json()),
      apiFetch("/api/skills").then((r) => r.json()),
      apiFetch("/api/settings").then((r) => r.json()),
      apiFetch("/api/settings/local").then((r) => r.json()).catch(() => ({})),
    ])
      .then(([modelsRes, overridesRes, skillsRes, settingsRes, localRes]) => {
        const m = modelsRes.models || [];
        setModels(m);
        setServerStatus(m.length > 0 ? "online" : "offline");
        setOverrides(overridesRes.overrides || {});
        const skillsList = Array.isArray(skillsRes) ? skillsRes : skillsRes.skills || [];
        setSkills(skillsList);
        setDefaultModel(settingsRes.settings?.default_model || "");
        if (localRes.runtime) setConnRuntime(localRes.runtime);
        if (localRes.address) setConnAddress(localRes.address);
        if (localRes.port) setConnPort(String(localRes.port));
        if (localRes.max_workers) setMaxWorkers(localRes.max_workers);
      })
      .catch(() => setLoadError("Failed to load settings. Check your connection."))
      .finally(() => setLoading(false));
  }, []);

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

  // Group models by provider
  const grouped = useMemo(() => {
    return models.reduce<Record<string, ModelInfo[]>>((acc, m) => {
      const key = m.provider || "local";
      (acc[key] ??= []).push(m);
      return acc;
    }, {});
  }, [models]);

  // Search for the model gallery
  const filteredGalleryModels = useMemo(() => {
    if (!gallerySearch.trim()) return grouped;
    const q = gallerySearch.toLowerCase();
    const result: Record<string, ModelInfo[]> = {};
    for (const [prov, ms] of Object.entries(grouped)) {
      const filtered = ms.filter(
        (m) => m.name.toLowerCase().includes(q) || m.id.toLowerCase().includes(q),
      );
      if (filtered.length > 0) result[prov] = filtered;
    }
    return result;
  }, [grouped, gallerySearch]);

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

  const filteredProviderNames = Object.keys(filteredGalleryModels);

  return (
    <div className="settings-tab">
      <div className="settings-tab-header">
        <h2>Models</h2>
        <p>MUSE runs on local models via Ollama or vLLM. No API keys needed.</p>
      </div>

      {/* Local server connection */}
      <SettingsSection
        title="Local Server"
        description="Your local LLM server connection."
      >
        <div className="provider-keys-list">
          <div className="provider-key-row">
            <div className="provider-key-info">
              <span className="provider-key-name">
                {connRuntime.charAt(0).toUpperCase() + connRuntime.slice(1)} — {connAddress}:{connPort}
              </span>
              {serverStatus === "checking" && (
                <span className="provider-key-badge badge-none">checking...</span>
              )}
              {serverStatus === "online" && (
                <span className="provider-key-badge badge-vault">
                  <IconCheck size={10} /> {models.length} model{models.length !== 1 ? "s" : ""}
                </span>
              )}
              {serverStatus === "offline" && (
                <span className="provider-key-badge badge-none">not connected</span>
              )}
            </div>
            <div className="provider-key-actions">
              <button className="btn btn-sm btn-ghost" onClick={fetchModels}>Refresh</button>
              <button className="btn btn-sm btn-ghost" onClick={() => setConnEditing(!connEditing)}>
                {connEditing ? "Cancel" : "Configure"}
              </button>
            </div>
          </div>
          {connEditing && (
            <div className="custom-provider-form">
              <div className="custom-provider-field">
                <label className="custom-provider-label">Runtime</label>
                <div className="settings-select-wrapper">
                  <select className="settings-select" value={connRuntime} onChange={(e) => setConnRuntime(e.target.value)}>
                    <option value="ollama">Ollama</option>
                    <option value="vllm">vLLM</option>
                    <option value="llamacpp">llama.cpp</option>
                    <option value="other">Other (OpenAI-compatible)</option>
                  </select>
                  <IconChevronDown size={14} className="settings-select-icon" />
                </div>
              </div>
              <div className="custom-provider-field">
                <label className="custom-provider-label">Address</label>
                <input className="settings-input" type="text" placeholder="localhost" value={connAddress}
                  onChange={(e) => { setConnAddress(e.target.value); setConnError(""); }} />
              </div>
              <div className="custom-provider-field">
                <label className="custom-provider-label">Port</label>
                <input className="settings-input" type="text" placeholder="11434" value={connPort}
                  onChange={(e) => { setConnPort(e.target.value); setConnError(""); }} />
              </div>
              {connError && <span className="provider-key-error"><IconAlertCircle size={12} /> {connError}</span>}
              {connSuccess && <span className="provider-key-error" style={{ color: "var(--success)" }}><IconCheck size={12} /> Connection saved and models refreshed.</span>}
              <div className="custom-provider-actions">
                <button className="btn btn-sm btn-primary" disabled={connBusy || !connAddress.trim()} onClick={async () => {
                  setConnBusy(true); setConnError(""); setConnSuccess(false);
                  try {
                    const testRes = await apiFetch("/api/settings/local/test", {
                      method: "POST", headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ address: connAddress.trim(), port: parseInt(connPort) || 11434 }),
                    });
                    const testData = await testRes.json();
                    if (!testRes.ok || testData.error) { setConnError(testData.error || testData.detail || "Connection failed"); setConnBusy(false); return; }
                    const saveRes = await apiFetch("/api/settings/local", {
                      method: "PUT", headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ runtime: connRuntime, address: connAddress.trim(), port: parseInt(connPort) || 11434, models: (testData.models || []).map((m: { id: string }) => m.id), max_workers: maxWorkers }),
                    });
                    if (!saveRes.ok) { const d = await saveRes.json().catch(() => ({})); setConnError(d.detail || "Failed to save"); setConnBusy(false); return; }
                    setConnSuccess(true); fetchModels();
                    setTimeout(() => { setConnEditing(false); setConnSuccess(false); }, 1500);
                  } catch { setConnError("Could not reach the server. Check address and port."); }
                  setConnBusy(false);
                }}>{connBusy ? "Testing..." : "Test & Save"}</button>
                <button className="btn btn-sm btn-ghost" onClick={() => { setConnEditing(false); setConnError(""); }}>Cancel</button>
              </div>
            </div>
          )}
        </div>
        <div className="settings-field" style={{ marginTop: 12 }}>
          <label className="settings-label">Max Concurrent Tasks</label>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input
              className="settings-input"
              type="number"
              min={1}
              max={16}
              value={maxWorkers}
              style={{ width: 80 }}
              onChange={(e) => {
                const v = Math.max(1, Math.min(16, parseInt(e.target.value) || 1));
                setMaxWorkers(v);
              }}
              onBlur={async () => {
                try {
                  await apiFetch("/api/settings/local", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ runtime: connRuntime, address: connAddress.trim(), port: parseInt(connPort) || 11434, models: models.map((m) => m.id), max_workers: maxWorkers }),
                  });
                } catch {}
              }}
            />
          </div>
          <div className="settings-hint">
            How many skills can run in parallel. Lower values are more reliable on smaller models. Default: 2.
          </div>
        </div>
      </SettingsSection>

      {/* Default model */}
      <SettingsSection title="Model" description="The AI model used for chat and tasks.">
        {models.length === 0 ? (
          <div className="settings-empty-state">
            No models available. Install Ollama and run: <code>ollama pull llama3.2</code>
          </div>
        ) : (
          <div className="settings-field">
            <SearchableModelSelect
              models={models}
              grouped={grouped}
              value={defaultModel}
              onChange={saveDefaultModel}
              placeholder="Auto (first available)"
            />
            {saving && <span className="settings-saving">Saving...</span>}
          </div>
        )}
      </SettingsSection>

      {/* Advanced */}
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
          {skills.length > 0 && models.length > 0 && (
            <SettingsSection
              title="Per-Skill Model Overrides"
              description="Assign a specific model to individual skills."
            >
              <div className="override-list">
                {skills.map((skill) => (
                  <div key={skill.id} className="override-row">
                    <div className="override-skill">{skill.name || skill.id}</div>
                    <div className="override-select">
                      <SearchableModelSelect
                        models={models}
                        grouped={grouped}
                        value={overrides[skill.id] || ""}
                        onChange={(v) => saveOverride(skill.id, v)}
                        placeholder="Default"
                      />
                    </div>
                  </div>
                ))}
              </div>
            </SettingsSection>
          )}

          {models.length > 0 && (
            <SettingsSection title="Installed Models" description="Models available on your local server.">
              {models.length > 10 && (
                <input
                  className="settings-input model-search-input"
                  type="text"
                  placeholder="Search models..."
                  value={gallerySearch}
                  onChange={(e) => setGallerySearch(e.target.value)}
                />
              )}
              {filteredProviderNames.length === 0 ? (
                <div className="settings-empty-state">No models match your search.</div>
              ) : (
                filteredProviderNames.map((prov) => (
                  <div key={prov} className="provider-group">
                    <div className="model-grid">
                      {filteredGalleryModels[prov].map((m) => (
                        <ModelCard key={m.id} model={m} />
                      ))}
                    </div>
                  </div>
                ))
              )}
            </SettingsSection>
          )}
        </div>
      )}
    </div>
  );
}

function ModelCard({ model }: { model: ModelInfo }) {
  return (
    <div className="model-card">
      <div className="model-card-header">
        <div className="model-card-name">{model.name}</div>
      </div>
      <div className="model-card-id">{model.id}</div>
      <div className="model-card-stats">
        <span>Context: {(model.context_window / 1000).toFixed(0)}K</span>
      </div>
    </div>
  );
}

/* ─── Searchable Model Select ─── */

function SearchableModelSelect({
  models,
  grouped,
  value,
  onChange,
  placeholder = "Select a model",
}: {
  models: ModelInfo[];
  grouped: Record<string, ModelInfo[]>;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
        setSearch("");
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open]);

  const selectedModel = models.find((m) => m.id === value);
  const displayLabel = selectedModel ? selectedModel.name : placeholder;

  const filteredGrouped = useMemo(() => {
    if (!search.trim()) return grouped;
    const q = search.toLowerCase();
    const result: Record<string, ModelInfo[]> = {};
    for (const [prov, ms] of Object.entries(grouped)) {
      const filtered = ms.filter(
        (m) => m.name.toLowerCase().includes(q) || m.id.toLowerCase().includes(q),
      );
      if (filtered.length > 0) result[prov] = filtered;
    }
    return result;
  }, [grouped, search]);

  const filteredProviders = Object.keys(filteredGrouped);
  const totalFiltered = filteredProviders.reduce((n, p) => n + filteredGrouped[p].length, 0);

  const select = (modelId: string) => {
    onChange(modelId);
    setOpen(false);
    setSearch("");
  };

  return (
    <div className="sms-container" ref={containerRef}>
      <button
        className="sms-trigger"
        onClick={() => setOpen(!open)}
        type="button"
      >
        <span className={value ? "sms-trigger-label" : "sms-trigger-placeholder"}>
          {displayLabel}
        </span>
        <IconChevronDown size={14} className="sms-trigger-icon" />
      </button>

      {open && (
        <div className="sms-dropdown">
          <div className="sms-search-wrapper">
            <input
              ref={inputRef}
              className="sms-search"
              type="text"
              placeholder="Search models..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") { setOpen(false); setSearch(""); }
              }}
            />
            {search && (
              <button
                className="sms-search-clear"
                onClick={() => setSearch("")}
                type="button"
              >
                <IconX size={12} />
              </button>
            )}
          </div>

          <div className="sms-options">
            <button
              className={`sms-option ${!value ? "sms-option-active" : ""}`}
              onClick={() => select("")}
              type="button"
            >
              {placeholder}
            </button>

            {totalFiltered === 0 ? (
              <div className="sms-empty">No models match "{search}"</div>
            ) : (
              filteredProviders.map((prov) => (
                <div key={prov} className="sms-group">
                  {filteredGrouped[prov].map((m) => (
                    <button
                      key={m.id}
                      className={`sms-option ${m.id === value ? "sms-option-active" : ""}`}
                      onClick={() => select(m.id)}
                      type="button"
                    >
                      <span className="sms-option-name">{m.name}</span>
                      <span className="sms-option-id">{m.id}</span>
                    </button>
                  ))}
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default ModelsTab;
