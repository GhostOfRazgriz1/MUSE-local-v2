/**
 * First-run setup card — shown when no LLM provider is configured.
 *
 * Guides the user to paste an API key so the agent can start working.
 * Once a key is saved, calls onComplete() so the parent can switch to
 * the normal chat view.
 */

import React, { useState } from "react";
import { IconBot, IconKey, IconEye, IconEyeOff, IconCheck } from "./Icons";
import { apiFetch } from "../hooks/useApiToken";

interface SetupCardProps {
  onComplete: () => void;
}

const PROVIDERS = [
  { id: "openrouter", label: "OpenRouter", hint: "openrouter.ai/keys — works with all models", recommended: true },
  { id: "anthropic",  label: "Anthropic",  hint: "console.anthropic.com — Claude models" },
  { id: "openai",     label: "OpenAI",     hint: "platform.openai.com — GPT models" },
  { id: "gemini",     label: "Google Gemini", hint: "aistudio.google.com — Gemini models" },
];

export const SetupCard: React.FC<SetupCardProps> = ({ onComplete }) => {
  const [selectedProvider, setSelectedProvider] = useState("openrouter");
  const [keyValue, setKeyValue] = useState("");
  const [visible, setVisible] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const handleSave = async () => {
    if (!keyValue.trim()) return;
    setSaving(true);
    setError("");

    try {
      const res = await apiFetch(`/api/settings/providers/${selectedProvider}/key`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: keyValue.trim() }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.error || "Failed to save key");
        setSaving(false);
        return;
      }

      setDone(true);
      // Brief pause to show success, then transition to chat
      setTimeout(() => onComplete(), 800);
    } catch {
      setError("Connection failed — is the server running?");
      setSaving(false);
    }
  };

  const provider = PROVIDERS.find((p) => p.id === selectedProvider)!;

  return (
    <div className="setup-card-overlay">
      <div className="setup-card">
        <div className="setup-card-icon">
          <IconBot size={32} />
        </div>
        <h2 className="setup-card-title">Welcome to MUSE</h2>
        <p className="setup-card-desc">
          Add an API key to connect to an LLM provider. You can change this
          anytime in Settings.
        </p>

        {/* Provider selector */}
        <div className="setup-provider-list">
          {PROVIDERS.map((p) => (
            <button
              key={p.id}
              className={`setup-provider-btn ${selectedProvider === p.id ? "active" : ""}`}
              onClick={() => setSelectedProvider(p.id)}
            >
              {p.label}
              {p.recommended && <span className="setup-recommended">Recommended</span>}
            </button>
          ))}
        </div>

        {/* Key input */}
        <div className="setup-key-section">
          <label className="setup-key-label">
            <IconKey size={14} />
            {provider.label} API Key
          </label>
          <p className="setup-key-hint">
            Get your key at <strong>{provider.hint.split(" — ")[0]}</strong>
            {provider.hint.includes(" — ") && ` — ${provider.hint.split(" — ")[1]}`}
          </p>
          <div className="setup-key-input-row">
            <div className="setup-key-input-wrapper">
              <input
                className="setup-key-input"
                type={visible ? "text" : "password"}
                placeholder={`Paste your ${provider.label} key here`}
                value={keyValue}
                onChange={(e) => setKeyValue(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSave()}
                autoFocus
                disabled={done}
              />
              <button
                className="setup-key-vis-btn"
                onClick={() => setVisible((v) => !v)}
                title={visible ? "Hide" : "Show"}
              >
                {visible ? <IconEyeOff size={14} /> : <IconEye size={14} />}
              </button>
            </div>
          </div>

          {error && <div className="setup-error">{error}</div>}

          <button
            className={`btn btn-primary setup-save-btn ${done ? "done" : ""}`}
            onClick={handleSave}
            disabled={saving || !keyValue.trim() || done}
          >
            {done ? (
              <><IconCheck size={16} /> Connected</>
            ) : saving ? (
              "Connecting..."
            ) : (
              "Connect"
            )}
          </button>
        </div>

        <p className="setup-footer">
          Your key is stored securely in your OS keychain — never sent anywhere except the provider.
        </p>
      </div>
    </div>
  );
};
