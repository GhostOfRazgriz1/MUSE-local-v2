import { useState, useEffect, useCallback } from "react";
import { IconPlug, IconPlus, IconTrash, IconRefresh } from "../Icons";
import { apiFetch } from "../../hooks/useApiToken";
import { SettingsSection, SettingsLoader } from "./shared";

/* ─── Module-private types & constants ─── */

interface MCPServer {
  server_id: string;
  name: string;
  transport: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  url: string;
  enabled: boolean;
  status: string;
  error: string | null;
  tool_count: number;
  tools?: { name: string; description: string }[];
  context_mode?: "none" | "instruction" | "full";
  enrichment_mode?: "always" | "never" | "auto";
  lifecycle?: "persistent" | "on_demand";
}

const STATUS_LABEL: Record<string, string> = {
  connected: "Connected",
  connecting: "Connecting",
  disconnected: "Disconnected",
  error: "Error",
  on_demand: "On-demand",
};

/* ─── MCPAddForm ─── */

function MCPAddForm({ onAdded, onCancel }: { onAdded: () => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"stdio" | "sse" | "streamable-http">("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [envText, setEnvText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const serverId = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

  const handleSubmit = async () => {
    if (!name.trim()) { setError("Name is required."); return; }
    if (transport === "stdio" && !command.trim()) { setError("Command is required."); return; }
    if ((transport === "sse" || transport === "streamable-http") && !url.trim()) { setError("URL is required."); return; }

    setSaving(true);
    setError("");

    const env: Record<string, string> = {};
    for (const line of envText.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || !trimmed.includes("=")) continue;
      const [k, ...vParts] = trimmed.split("=");
      env[k.trim()] = vParts.join("=").trim();
    }

    const parsedArgs = args.trim() ? args.trim().split(/\s+/) : [];

    try {
      const res = await apiFetch("/api/mcp/servers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          server_id: serverId || `server-${Date.now()}`,
          name: name.trim(),
          transport,
          command: command.trim(),
          args: parsedArgs,
          env,
          url: url.trim(),
          enabled: true,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail || data.error || "Failed to add server.");
        setSaving(false);
        return;
      }
      onAdded();
    } catch {
      setError("Failed to add server.");
      setSaving(false);
    }
  };

  return (
    <div className="mcp-add-form">
      <div className="mcp-add-header">
        <div className="mcp-add-title">Add MCP Server</div>
        <div className="mcp-add-subtitle">
          {transport === "stdio" ? "Runs as a local subprocess" : "Connects to a remote HTTP endpoint"}{transport === "streamable-http" ? " (streamable)" : ""}
        </div>
      </div>

      <div className="mcp-add-fields">
        <label className="mcp-field-group">
          <span className="mcp-field-label">Name</span>
          <input className="settings-input" placeholder="e.g., Postgres, GitHub, Filesystem" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        </label>

        <label className="mcp-field-group">
          <span className="mcp-field-label">Transport</span>
          <div className="mcp-transport-toggle">
            <button
              className={`mcp-transport-btn ${transport === "stdio" ? "active" : ""}`}
              onClick={() => setTransport("stdio")}
              type="button"
            >
              Subprocess (stdio)
            </button>
            <button
              className={`mcp-transport-btn ${transport === "sse" ? "active" : ""}`}
              onClick={() => setTransport("sse")}
              type="button"
            >
              HTTP (SSE)
            </button>
            <button
              className={`mcp-transport-btn ${transport === "streamable-http" ? "active" : ""}`}
              onClick={() => setTransport("streamable-http")}
              type="button"
            >
              Streamable HTTP
            </button>
          </div>
        </label>

        {transport === "stdio" ? (
          <>
            <label className="mcp-field-group">
              <span className="mcp-field-label">Command</span>
              <input className="settings-input" placeholder="npx, python, uvx, node..." value={command} onChange={(e) => setCommand(e.target.value)} />
            </label>
            <label className="mcp-field-group">
              <span className="mcp-field-label">Arguments</span>
              <input className="settings-input" placeholder="-m mcp_server --port 3000" value={args} onChange={(e) => setArgs(e.target.value)} />
            </label>
            <label className="mcp-field-group">
              <span className="mcp-field-label">Environment variables</span>
              <textarea className="settings-input mcp-env-input" placeholder={"DATABASE_URL=postgres://...\nAPI_KEY=sk-..."} value={envText} onChange={(e) => setEnvText(e.target.value)} rows={3} />
            </label>
          </>
        ) : (
          <label className="mcp-field-group">
            <span className="mcp-field-label">Server URL</span>
            <input className="settings-input" placeholder={transport === "streamable-http" ? "http://localhost:3001/mcp" : "http://localhost:3001/sse"} value={url} onChange={(e) => setUrl(e.target.value)} />
          </label>
        )}
      </div>

      {error && <div className="oauth-setup-error">{error}</div>}

      <div className="mcp-add-actions">
        <button className="btn btn-ghost btn-sm" onClick={onCancel}>Cancel</button>
        <button className="btn btn-primary btn-sm" onClick={handleSubmit} disabled={saving}>
          {saving ? "Adding..." : "Add & Connect"}
        </button>
      </div>
    </div>
  );
}

/* ─── MCPTab ─── */

function MCPTab() {
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [expandedTools, setExpandedTools] = useState<{ name: string; description: string }[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [toolFilter, setToolFilter] = useState("");

  const fetchServers = useCallback(async () => {
    try {
      const res = await apiFetch("/api/mcp/servers");
      if (res.ok) {
        const data = await res.json();
        setServers(data.servers || []);
      }
    } catch {}
    setLoading(false);
  }, []);

  useEffect(() => { fetchServers(); }, [fetchServers]);

  const handleExpand = async (serverId: string) => {
    if (expandedId === serverId) { setExpandedId(null); return; }
    setExpandedId(serverId);
    setToolFilter("");
    try {
      const res = await apiFetch(`/api/mcp/servers/${serverId}`);
      if (res.ok) {
        const data = await res.json();
        setExpandedTools(data.tools || []);
      }
    } catch { setExpandedTools([]); }
  };

  const handleConnect = async (serverId: string) => {
    // Optimistic status update
    setServers((prev) => prev.map((s) => s.server_id === serverId ? { ...s, status: "connecting" } : s));
    await apiFetch(`/api/mcp/servers/${serverId}/connect`, { method: "POST" });
    fetchServers();
  };

  const handleDisconnect = async (serverId: string) => {
    await apiFetch(`/api/mcp/servers/${serverId}/disconnect`, { method: "POST" });
    fetchServers();
  };

  const handleDelete = async (serverId: string) => {
    await apiFetch(`/api/mcp/servers/${serverId}`, { method: "DELETE" });
    setServers((prev) => prev.filter((s) => s.server_id !== serverId));
    if (expandedId === serverId) setExpandedId(null);
  };

  const filteredTools = toolFilter
    ? expandedTools.filter((t) =>
        t.name.toLowerCase().includes(toolFilter.toLowerCase()) ||
        t.description?.toLowerCase().includes(toolFilter.toLowerCase())
      )
    : expandedTools;

  if (loading) return <SettingsLoader />;

  return (
    <div className="settings-tab">
      <SettingsSection
        title="MCP Servers"
        description="Connect to external tool servers using the Model Context Protocol."
        action={
          !showAdd && (
            <button className="btn btn-primary btn-sm" onClick={() => setShowAdd(true)}>
              <IconPlus size={14} /> Add Server
            </button>
          )
        }
      >
        {servers.length === 0 && !showAdd && (
          <div className="mcp-empty">
            <div className="mcp-empty-icon"><IconPlug size={32} /></div>
            <div className="mcp-empty-text">No servers configured</div>
            <div className="mcp-empty-hint">
              Add an MCP server to give the agent access to external tools — databases, APIs, file systems, and more.
            </div>
          </div>
        )}

        {showAdd && (
          <MCPAddForm
            onAdded={() => { setShowAdd(false); fetchServers(); }}
            onCancel={() => setShowAdd(false)}
          />
        )}

        <div className="mcp-server-list">
          {servers.map((s) => {
            const isExpanded = expandedId === s.server_id;
            const isConnected = s.status === "connected";
            const isConnecting = s.status === "connecting";
            const isError = s.status === "error";

            return (
              <div key={s.server_id} className={`mcp-card ${isExpanded ? "expanded" : ""}`}>
                <div className="mcp-card-header" onClick={() => handleExpand(s.server_id)}>
                  <div className="mcp-card-left">
                    <span className={`mcp-dot ${s.status}`} title={STATUS_LABEL[s.status] || s.status} />
                    <div className="mcp-card-title">
                      <span className="mcp-card-name">{s.name}</span>
                      <span className="mcp-card-meta">
                        <span className="mcp-badge">{s.transport.toUpperCase()}</span>
                        {isConnected && <span className="mcp-tool-count">{s.tool_count} tool{s.tool_count !== 1 ? "s" : ""}</span>}
                        {isConnecting && <span className="mcp-connecting-label">Connecting...</span>}
                      </span>
                    </div>
                  </div>
                  <div className="mcp-card-right" onClick={(e) => e.stopPropagation()}>
                    {s.status === "on_demand" ? (
                      <span className="mcp-on-demand-label">Connects when needed</span>
                    ) : isConnected ? (
                      <button className="btn btn-ghost btn-sm" onClick={() => handleDisconnect(s.server_id)}>
                        Disconnect
                      </button>
                    ) : (
                      <button
                        className="btn btn-primary btn-sm"
                        onClick={() => handleConnect(s.server_id)}
                        disabled={isConnecting}
                      >
                        {isError ? <><IconRefresh size={12} /> Retry</> : "Connect"}
                      </button>
                    )}
                    <button
                      className="settings-icon-btn danger"
                      onClick={() => handleDelete(s.server_id)}
                      title="Remove server"
                    >
                      <IconTrash size={13} />
                    </button>
                  </div>
                </div>

                {isError && s.error && (
                  <div className="mcp-card-error">{s.error}</div>
                )}

                {isExpanded && (
                  <div className="mcp-card-body">
                    <div className="mcp-card-config">
                      <div className="mcp-config-label">{s.transport === "stdio" ? "Command" : "Endpoint"}</div>
                      <code className="mcp-config-value">
                        {s.transport === "stdio" ? [s.command, ...s.args].join(" ") : s.url}
                      </code>
                    </div>

                    <div className="mcp-card-modes">
                      <div className="mcp-mode-row">
                        <span className="mcp-config-label">Lifecycle</span>
                        <div className="mcp-transport-toggle">
                          {(["persistent", "on_demand"] as const).map((mode) => (
                            <button
                              key={mode}
                              className={`mcp-transport-btn ${(s.lifecycle || "persistent") === mode ? "active" : ""}`}
                              onClick={async () => {
                                await apiFetch(`/api/mcp/servers/${s.server_id}`, {
                                  method: "PATCH",
                                  headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify({ lifecycle: mode }),
                                });
                                fetchServers();
                              }}
                              type="button"
                            >
                              {mode === "persistent" ? "Always On" : "On Demand"}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div className="mcp-mode-row">
                        <span className="mcp-config-label">Context injection</span>
                        <div className="mcp-transport-toggle">
                          {(["none", "instruction", "full"] as const).map((mode) => (
                            <button
                              key={mode}
                              className={`mcp-transport-btn ${(s.context_mode || "instruction") === mode ? "active" : ""}`}
                              onClick={async () => {
                                await apiFetch(`/api/mcp/servers/${s.server_id}`, {
                                  method: "PATCH",
                                  headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify({ context_mode: mode }),
                                });
                                fetchServers();
                              }}
                              type="button"
                            >
                              {mode === "none" ? "None" : mode === "instruction" ? "Instruction" : "Full conversation"}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div className="mcp-mode-row">
                        <span className="mcp-config-label">Response enrichment</span>
                        <div className="mcp-transport-toggle">
                          {(["never", "auto", "always"] as const).map((mode) => (
                            <button
                              key={mode}
                              className={`mcp-transport-btn ${(s.enrichment_mode || "auto") === mode ? "active" : ""}`}
                              onClick={async () => {
                                await apiFetch(`/api/mcp/servers/${s.server_id}`, {
                                  method: "PATCH",
                                  headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify({ enrichment_mode: mode }),
                                });
                                fetchServers();
                              }}
                              type="button"
                            >
                              {mode === "never" ? "Raw" : mode === "auto" ? "Auto" : "Always"}
                            </button>
                          ))}
                        </div>
                      </div>
                    </div>

                    {expandedTools.length > 0 && (
                      <div className="mcp-card-tools">
                        <div className="mcp-tools-header">
                          <span className="mcp-config-label">Tools ({expandedTools.length})</span>
                          {expandedTools.length > 5 && (
                            <input
                              className="mcp-tool-search"
                              placeholder="Filter tools..."
                              value={toolFilter}
                              onChange={(e) => setToolFilter(e.target.value)}
                              onClick={(e) => e.stopPropagation()}
                            />
                          )}
                        </div>
                        <div className="mcp-tool-grid">
                          {filteredTools.map((tool) => (
                            <div key={tool.name} className="mcp-tool-chip">
                              <code className="mcp-tool-name">{tool.name}</code>
                              {tool.description && (
                                <span className="mcp-tool-desc">{tool.description}</span>
                              )}
                            </div>
                          ))}
                          {filteredTools.length === 0 && toolFilter && (
                            <div className="mcp-tool-no-match">No tools match "{toolFilter}"</div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </SettingsSection>
    </div>
  );
}

export default MCPTab;
