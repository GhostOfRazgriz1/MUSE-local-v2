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
import { useLocale } from "../i18n";

interface SetupCardProps {
  onComplete: () => void;
}

const PROVIDERS = [
  { id: "openrouter", label: "OpenRouter", hint: "openrouter.ai/keys — works with all models", recommended: true },
  { id: "anthropic",  label: "Anthropic",  hint: "console.anthropic.com — Claude models" },
  { id: "openai",     label: "OpenAI",     hint: "platform.openai.com — GPT models" },
  { id: "gemini",     label: "Google Gemini", hint: "aistudio.google.com — Gemini models" },
  { id: "deepseek",   label: "DeepSeek",   hint: "platform.deepseek.com — DeepSeek models" },
  { id: "alibaba",    label: "Alibaba (DashScope)", hint: "dashscope.console.aliyun.com — Qwen models" },
  { id: "bytedance",  label: "ByteDance (Volcengine)", hint: "console.volcengine.com — Doubao models" },
  { id: "minimax",    label: "MiniMax",    hint: "platform.minimaxi.com — MiniMax models" },
];

export const SetupCard: React.FC<SetupCardProps> = ({ onComplete }) => {
  const { t } = useLocale();
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
      setError(t("setup_connection_failed"));
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
        <h2 className="setup-card-title">{t("setup_welcome")}</h2>
        <p className="setup-card-desc">
          {t("setup_desc")}
        </p>

        {/* Provider selector */}
        <div className="setup-provider-select-wrapper">
          <select
            className="setup-provider-select"
            value={selectedProvider}
            onChange={(e) => setSelectedProvider(e.target.value)}
          >
            {PROVIDERS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}{p.recommended ? ` (${t("setup_recommended")})` : ""}
              </option>
            ))}
          </select>
        </div>

        {/* Key input */}
        <div className="setup-key-section">
          <label className="setup-key-label">
            <IconKey size={14} />
            {provider.label} {t("setup_api_key")}
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
                placeholder={t("setup_paste_key", { provider: provider.label })}
                value={keyValue}
                onChange={(e) => setKeyValue(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSave()}
                autoFocus
                disabled={done}
              />
              <button
                className="setup-key-vis-btn"
                onClick={() => setVisible((v) => !v)}
                title={visible ? t("hide") : t("show")}
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
              <><IconCheck size={16} /> {t("setup_connected")}</>
            ) : saving ? (
              t("setup_connecting")
            ) : (
              t("setup_connect")
            )}
          </button>
        </div>

        <p className="setup-footer">
          {t("setup_footer")}
        </p>
      </div>
    </div>
  );
};
