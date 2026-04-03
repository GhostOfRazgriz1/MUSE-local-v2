import React, { useState, useEffect, useCallback } from "react";

interface VisionTabProps {
  apiBase: string;
  token: string;
}

interface ScreenStatus {
  capture_available: boolean;
  vision_model_available: boolean;
  vision_model: string | null;
  mode: string;
  is_streaming: boolean;
  fps: number;
}

/**
 * Settings tab for desktop vision / Gemma 4 screen streaming configuration.
 */
const VisionTab: React.FC<VisionTabProps> = ({ apiBase, token }) => {
  const [status, setStatus] = useState<ScreenStatus | null>(null);
  const [fps, setFps] = useState(1);
  const [maxDimension, setMaxDimension] = useState(1280);
  const [monitor, setMonitor] = useState(0);
  const [saving, setSaving] = useState(false);

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };

  const fetchStatus = useCallback(async () => {
    try {
      const resp = await fetch(`${apiBase}/api/screen/status`, { headers });
      const data = await resp.json();
      setStatus(data);
      if (data.fps) setFps(data.fps);
    } catch {}
  }, [apiBase, token]);

  useEffect(() => {
    fetchStatus();
  }, [fetchStatus]);

  const saveConfig = async () => {
    setSaving(true);
    try {
      await fetch(`${apiBase}/api/screen/configure`, {
        method: "POST",
        headers,
        body: JSON.stringify({ fps, max_dimension: maxDimension, monitor }),
      });
      await fetchStatus();
    } catch {}
    setSaving(false);
  };

  const sectionStyle: React.CSSProperties = {
    marginBottom: "16px",
    padding: "12px",
    borderRadius: "8px",
    border: "1px solid var(--border-color, #e5e7eb)",
  };

  const labelStyle: React.CSSProperties = {
    display: "block",
    fontSize: "12px",
    color: "#6b7280",
    marginBottom: "4px",
  };

  const inputStyle: React.CSSProperties = {
    width: "100%",
    padding: "6px 8px",
    borderRadius: "4px",
    border: "1px solid var(--border-color, #d1d5db)",
    background: "var(--input-bg, #fff)",
    color: "var(--text-color, #111)",
    fontSize: "13px",
  };

  return (
    <div style={{ maxWidth: 480 }}>
      <h3 style={{ fontSize: "15px", fontWeight: 600, marginBottom: "12px" }}>
        Desktop Vision
      </h3>
      <p style={{ fontSize: "12px", color: "#6b7280", marginBottom: "16px" }}>
        Stream your desktop to a local Gemma 4 model for visual awareness and
        automation. All frames stay on your machine — nothing is sent to the cloud.
      </p>

      {/* Status */}
      <div style={sectionStyle}>
        <div style={{ fontWeight: 500, marginBottom: "8px", fontSize: "13px" }}>
          Status
        </div>
        {status ? (
          <div style={{ fontSize: "12px", display: "flex", flexDirection: "column", gap: "4px" }}>
            <div>
              Screen capture:{" "}
              <span style={{ color: status.capture_available ? "#22c55e" : "#ef4444" }}>
                {status.capture_available ? "Available" : "Not installed"}
              </span>
            </div>
            <div>
              Vision model:{" "}
              <span style={{ color: status.vision_model_available ? "#22c55e" : "#f59e0b" }}>
                {status.vision_model || "Not detected"}
              </span>
            </div>
            <div>
              Mode: <strong>{status.mode}</strong>
              {status.is_streaming && ` (${status.fps} fps)`}
            </div>
          </div>
        ) : (
          <div style={{ fontSize: "12px", color: "#6b7280" }}>Loading...</div>
        )}
      </div>

      {/* Capture Settings */}
      <div style={sectionStyle}>
        <div style={{ fontWeight: 500, marginBottom: "8px", fontSize: "13px" }}>
          Capture Settings
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          <div>
            <label style={labelStyle}>Frames per second (0.1–10)</label>
            <input
              type="number"
              min={0.1}
              max={10}
              step={0.1}
              value={fps}
              onChange={(e) => setFps(Number(e.target.value))}
              style={inputStyle}
            />
          </div>
          <div>
            <label style={labelStyle}>Max image dimension (px)</label>
            <input
              type="number"
              min={320}
              max={3840}
              step={10}
              value={maxDimension}
              onChange={(e) => setMaxDimension(Number(e.target.value))}
              style={inputStyle}
            />
          </div>
          <div>
            <label style={labelStyle}>Monitor index (0 = all)</label>
            <input
              type="number"
              min={0}
              max={10}
              value={monitor}
              onChange={(e) => setMonitor(Number(e.target.value))}
              style={inputStyle}
            />
          </div>
          <button
            onClick={saveConfig}
            disabled={saving}
            style={{
              padding: "6px 12px",
              borderRadius: "4px",
              border: "1px solid #3b82f6",
              background: "#3b82f615",
              color: "#3b82f6",
              cursor: saving ? "default" : "pointer",
              fontSize: "12px",
              fontWeight: 500,
              alignSelf: "flex-end",
            }}
          >
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      </div>

      {/* Privacy notice */}
      <div style={{
        ...sectionStyle,
        background: "#22c55e08",
        borderColor: "#22c55e33",
      }}>
        <div style={{ fontWeight: 500, marginBottom: "4px", fontSize: "13px", color: "#22c55e" }}>
          Privacy
        </div>
        <div style={{ fontSize: "12px", color: "#6b7280" }}>
          Screen frames are processed entirely on your local machine by the
          local Gemma 4 model. No screenshots or video data are ever sent to
          external servers. If no local model is detected, the feature is
          disabled — it will never silently fall back to a cloud API.
        </div>
      </div>
    </div>
  );
};

export default VisionTab;
