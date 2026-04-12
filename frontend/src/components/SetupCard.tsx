/**
 * First-run setup card — configure local LLM server.
 *
 * Lets the user select a runtime (Ollama/vLLM/llama.cpp/Other),
 * set address and port, add model names, and configure max workers.
 */

import React, { useState } from "react";
import { IconBot, IconCheck, IconAlertCircle, IconX, IconPlus } from "./Icons";
import { apiFetch } from "../hooks/useApiToken";

interface SetupCardProps {
  onComplete: () => void;
}

const RUNTIMES = [
  { id: "ollama", label: "Ollama", defaultPort: 11434 },
  { id: "vllm", label: "vLLM", defaultPort: 8000 },
  { id: "llama.cpp", label: "llama.cpp", defaultPort: 8080 },
  { id: "other", label: "Other (OpenAI Compatible)", defaultPort: 8000 },
];

export const SetupCard: React.FC<SetupCardProps> = ({ onComplete }) => {
  const [runtime, setRuntime] = useState("ollama");
  const [address, setAddress] = useState("localhost");
  const [port, setPort] = useState(11434);
  const [modelInput, setModelInput] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [maxWorkers, setMaxWorkers] = useState(2);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<{ status: string; message?: string; models?: string[] } | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const handleRuntimeChange = (newRuntime: string) => {
    setRuntime(newRuntime);
    const rt = RUNTIMES.find((r) => r.id === newRuntime);
    if (rt) setPort(rt.defaultPort);
    setTestResult(null);
  };

  const addModel = () => {
    const name = modelInput.trim();
    if (name && !models.includes(name)) {
      setModels([...models, name]);
      setModelInput("");
    }
  };

  const removeModel = (name: string) => {
    setModels(models.filter((m) => m !== name));
  };

  const testConnection = async () => {
    setTesting(true);
    setTestResult(null);
    setError("");
    try {
      const res = await apiFetch("/api/settings/local/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address, port }),
      });
      const data = await res.json();
      setTestResult(data);
      if (data.status === "ok" && data.models?.length > 0 && models.length === 0) {
        setModels(data.models);
      }
    } catch {
      setTestResult({ status: "error", message: "Cannot reach MUSE server." });
    }
    setTesting(false);
  };

  const handleSave = async () => {
    if (models.length === 0) {
      setError("Add at least one model name.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const res = await apiFetch("/api/settings/local", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          runtime,
          address,
          port,
          models,
          max_workers: maxWorkers,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail || "Failed to save configuration");
        setSaving(false);
        return;
      }
      setDone(true);
      setTimeout(() => onComplete(), 800);
    } catch {
      setError("Connection failed — is the MUSE server running?");
      setSaving(false);
    }
  };

  return (
    <div className="setup-card-overlay">
      <div className="setup-card">
        <div className="setup-card-icon">
          <IconBot size={32} />
        </div>
        <h2 className="setup-card-title">Welcome to MUSE</h2>
        <p className="setup-card-desc">
          Configure your local LLM server. Everything runs on your machine.
        </p>

        <div className="setup-key-section">
          {/* Runtime selector */}
          <div className="setup-field">
            <label className="setup-key-label">Runtime</label>
            <div className="setup-provider-select-wrapper">
              <select
                className="setup-provider-select"
                value={runtime}
                onChange={(e) => handleRuntimeChange(e.target.value)}
                disabled={done}
              >
                {RUNTIMES.map((r) => (
                  <option key={r.id} value={r.id}>{r.label}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Address and Port */}
          <div className="setup-field-row">
            <div className="setup-field">
              <label className="setup-key-label">Address</label>
              <input
                className="setup-input"
                type="text"
                value={address}
                onChange={(e) => { setAddress(e.target.value); setTestResult(null); }}
                placeholder="localhost"
                disabled={done}
              />
            </div>
            <div className="setup-field setup-field-port">
              <label className="setup-key-label">Port</label>
              <input
                className="setup-input"
                type="number"
                value={port}
                onChange={(e) => { setPort(parseInt(e.target.value) || 0); setTestResult(null); }}
                disabled={done}
              />
            </div>
          </div>

          {/* Test connection */}
          <button
            className="btn btn-sm btn-ghost setup-test-btn"
            onClick={testConnection}
            disabled={testing || done}
          >
            {testing ? "Testing..." : "Test Connection"}
          </button>

          {testResult && (
            <div className={`setup-status ${testResult.status === "ok" ? "setup-status-ok" : "setup-status-err"}`}>
              {testResult.status === "ok" ? (
                <>
                  <IconCheck size={14} />
                  <span>Connected — {testResult.models?.length || 0} model(s) found</span>
                </>
              ) : (
                <>
                  <IconAlertCircle size={14} />
                  <span>{testResult.message}</span>
                </>
              )}
            </div>
          )}

          {/* Models */}
          <div className="setup-field">
            <label className="setup-key-label">Models</label>
            <p className="setup-key-hint">
              Add the model names you want to use (e.g. <strong>llama3.2</strong>, <strong>gemma2</strong>).
              {runtime === "ollama" && " Make sure they're pulled with `ollama pull <name>`."}
            </p>

            <div className="setup-model-row">
              <input
                className="setup-input"
                type="text"
                placeholder="Model name"
                value={modelInput}
                onChange={(e) => setModelInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addModel(); } }}
                disabled={done}
              />
              <button
                className="btn btn-sm btn-primary"
                onClick={addModel}
                disabled={!modelInput.trim() || done}
                title="Add model"
              >
                <IconPlus size={14} />
              </button>
            </div>

            {models.length > 0 && (
              <div className="setup-model-tags">
                {models.map((m) => (
                  <span key={m} className="setup-model-tag">
                    {m}
                    {!done && (
                      <button onClick={() => removeModel(m)} title="Remove">
                        <IconX size={10} />
                      </button>
                    )}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* Max workers */}
          <div className="setup-field">
            <label className="setup-key-label">Max Concurrent Workers</label>
            <p className="setup-key-hint">
              How many tasks can run at once. Lower values use less memory.
            </p>
            <input
              className="setup-input setup-input-sm"
              type="number"
              min={1}
              max={16}
              value={maxWorkers}
              onChange={(e) => setMaxWorkers(Math.max(1, Math.min(16, parseInt(e.target.value) || 1)))}
              disabled={done}
            />
          </div>

          {error && <div className="setup-error">{error}</div>}

          <button
            className={`btn btn-primary setup-save-btn ${done ? "done" : ""}`}
            onClick={handleSave}
            disabled={saving || models.length === 0 || done}
          >
            {done ? (
              <><IconCheck size={16} /> Configured</>
            ) : saving ? (
              "Saving..."
            ) : (
              "Save & Start"
            )}
          </button>
        </div>

        <p className="setup-footer">
          All data stays on your machine. No cloud, no API keys, no tracking.
        </p>
      </div>
    </div>
  );
};
