import React, { useState, useEffect, useCallback } from "react";

type ScreenMode = "off" | "passive" | "active";

interface ScreenStatus {
  capture_available: boolean;
  vision_model_available: boolean;
  vision_model: string | null;
  mode: ScreenMode;
  is_streaming: boolean;
  fps: number;
}

interface ScreenControlsProps {
  apiBase: string;
  token: string;
}

/**
 * Desktop vision controls — toggle passive/active screen streaming,
 * configure capture settings, and manage safety features.
 */
const ScreenControls: React.FC<ScreenControlsProps> = ({ apiBase, token }) => {
  const [status, setStatus] = useState<ScreenStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };

  const fetchStatus = useCallback(async () => {
    try {
      const resp = await fetch(`${apiBase}/api/screen/status`, { headers });
      const data = await resp.json();
      setStatus(data);
      setError(null);
    } catch {
      setStatus(null);
    }
  }, [apiBase, token]);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  const toggleMode = async (mode: ScreenMode) => {
    setLoading(true);
    setError(null);
    try {
      if (mode === "off") {
        await fetch(`${apiBase}/api/screen/stop`, {
          method: "POST",
          headers,
        });
      } else {
        await fetch(`${apiBase}/api/screen/start`, {
          method: "POST",
          headers,
          body: JSON.stringify({ mode }),
        });
      }
      await fetchStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to change mode");
    } finally {
      setLoading(false);
    }
  };

  const killSwitch = async () => {
    try {
      await fetch(`${apiBase}/api/screen/kill`, {
        method: "POST",
        headers,
      });
      await fetchStatus();
    } catch {
      setError("Failed to activate kill switch");
    }
  };

  if (!status) return null;
  if (!status.capture_available && !status.vision_model_available) return null;

  const modeColor = {
    off: "#6b7280",
    passive: "#3b82f6",
    active: "#ef4444",
  };

  return (
    <div className="screen-controls" style={{
      padding: "8px 12px",
      borderRadius: "8px",
      border: `1px solid ${modeColor[status.mode]}33`,
      background: `${modeColor[status.mode]}08`,
      fontSize: "13px",
    }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "8px",
          cursor: "pointer",
        }}
        onClick={() => setExpanded(!expanded)}
      >
        {/* Status indicator dot */}
        <span style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: status.is_streaming ? modeColor[status.mode] : "#6b7280",
          display: "inline-block",
          animation: status.is_streaming ? "pulse 2s infinite" : "none",
        }} />
        <span style={{ fontWeight: 500 }}>
          Screen: {status.mode === "off" ? "Off" : status.mode === "passive" ? "Watching" : "Acting"}
        </span>
        {status.vision_model && (
          <span style={{ color: "#6b7280", fontSize: "11px" }}>
            ({status.vision_model})
          </span>
        )}
        <span style={{ marginLeft: "auto", color: "#6b7280" }}>
          {expanded ? "\u25B2" : "\u25BC"}
        </span>
      </div>

      {expanded && (
        <div style={{ marginTop: "8px", display: "flex", flexDirection: "column", gap: "6px" }}>
          {error && (
            <div style={{ color: "#ef4444", fontSize: "12px" }}>{error}</div>
          )}

          {/* Mode buttons */}
          <div style={{ display: "flex", gap: "6px" }}>
            {(["off", "passive", "active"] as ScreenMode[]).map((mode) => (
              <button
                key={mode}
                onClick={() => toggleMode(mode)}
                disabled={loading || status.mode === mode}
                style={{
                  flex: 1,
                  padding: "4px 8px",
                  borderRadius: "4px",
                  border: `1px solid ${status.mode === mode ? modeColor[mode] : "#d1d5db"}`,
                  background: status.mode === mode ? `${modeColor[mode]}15` : "transparent",
                  color: status.mode === mode ? modeColor[mode] : "#6b7280",
                  cursor: loading || status.mode === mode ? "default" : "pointer",
                  fontSize: "12px",
                  fontWeight: status.mode === mode ? 600 : 400,
                }}
              >
                {mode === "off" ? "Off" : mode === "passive" ? "Passive" : "Active"}
              </button>
            ))}
          </div>

          {/* Info row */}
          {status.is_streaming && (
            <div style={{ fontSize: "11px", color: "#6b7280" }}>
              Capturing at {status.fps} fps
              {!status.vision_model_available && " (no vision model detected)"}
            </div>
          )}

          {/* Kill switch for active mode */}
          {status.mode === "active" && (
            <button
              onClick={killSwitch}
              style={{
                padding: "4px 8px",
                borderRadius: "4px",
                border: "1px solid #ef4444",
                background: "#ef444415",
                color: "#ef4444",
                cursor: "pointer",
                fontSize: "12px",
                fontWeight: 600,
              }}
            >
              Emergency Stop
            </button>
          )}

          {/* Missing dependencies warning */}
          {!status.capture_available && (
            <div style={{ fontSize: "11px", color: "#f59e0b" }}>
              Screen capture unavailable. Install: pip install mss Pillow
            </div>
          )}
          {!status.vision_model_available && (
            <div style={{ fontSize: "11px", color: "#f59e0b" }}>
              No local vision model detected. Run a Gemma 4 model via Ollama/vLLM.
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default ScreenControls;
