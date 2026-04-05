import React, { useEffect, useState, useCallback } from "react";
import ReactDOM from "react-dom";
import { IconMessageSquare, IconPlus, IconTrash, IconGitBranch } from "./Icons";
import { apiFetch } from "../hooks/useApiToken";
import type { Session } from "../types/events";

interface BranchTip {
  id: number;
  role: string;
  content: string;
  created_at: string;
}

interface SessionSidebarProps {
  activeSessionId: string | null;
  workingSessions?: Set<string>;
  onSelectSession: (sessionId: string | null) => void;
  onForkToBranch: (sessionId: string, messageId: number) => void;
  sessionUpdateTrigger: number;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  // Compare by calendar date (midnight-to-midnight), not elapsed time
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const msgDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.round((today.getTime() - msgDay.getTime()) / 86400000);
  if (diffDays === 0) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays} days ago`;
  if (diffDays < 14) return "A week ago";
  if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`;
  if (diffDays < 60) return "A month ago";
  if (diffDays < 365) return `${Math.floor(diffDays / 30)} months ago`;
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

export const SessionSidebar: React.FC<SessionSidebarProps> = ({
  activeSessionId,
  workingSessions,
  onSelectSession,
  onForkToBranch,
  sessionUpdateTrigger,
}) => {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [search, setSearch] = useState("");
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [expandedBranchSession, setExpandedBranchSession] = useState<string | null>(null);
  const [branches, setBranches] = useState<BranchTip[]>([]);
  const [searchResults, setSearchResults] = useState<any[]>([]);

  const fetchSessions = useCallback(async () => {
    try {
      const res = await apiFetch("/api/sessions");
      if (res.ok) {
        const data = await res.json();
        setSessions(data.sessions || []);
      }
    } catch {
      // Silently fail — server may not be up yet
    }
  }, []);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions, sessionUpdateTrigger]);

  const handleNew = () => {
    onSelectSession(null);
  };

  const handleDeleteClick = (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    setPendingDeleteId(sessionId);
  };

  const handleBranchToggle = async (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    if (expandedBranchSession === sessionId) {
      setExpandedBranchSession(null);
      setBranches([]);
      return;
    }
    try {
      const res = await apiFetch(`/api/sessions/${sessionId}/branches`);
      if (res.ok) {
        const data = await res.json();
        const tips = data.branches || [];
        if (tips.length > 1) {
          setBranches(tips);
          setExpandedBranchSession(sessionId);
        }
      }
    } catch {
      // ignore
    }
  };

  const executeDelete = async (purgeMemories: boolean) => {
    if (!pendingDeleteId) return;
    const sessionId = pendingDeleteId;
    setPendingDeleteId(null);

    try {
      const url = `/api/sessions/${sessionId}?purge_memories=${purgeMemories}`;
      await apiFetch(url, { method: "DELETE" });
      setSessions((prev) => prev.filter((s) => s.id !== sessionId));
      if (activeSessionId === sessionId) {
        onSelectSession(null);
      }
    } catch {
      // ignore
    }
  };

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <span className="sidebar-title">Sessions</span>
        <button
          className="sidebar-new-btn"
          onClick={handleNew}
          title="New conversation"
        >
          <IconPlus size={14} />
          New
        </button>
      </div>

      {sessions.length > 3 && (
        <div className="sidebar-search">
          <input
            type="text"
            className="sidebar-search-input"
            placeholder="Search sessions..."
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              // Deep search via API when query is 3+ chars
              const q = e.target.value.trim();
              if (q.length >= 3) {
                apiFetch(`/api/sessions/search?q=${encodeURIComponent(q)}&limit=10`)
                  .then((r) => r.json())
                  .then((d) => setSearchResults(d.results || []))
                  .catch(() => setSearchResults([]));
              } else {
                setSearchResults([]);
              }
            }}
          />
        </div>
      )}

      {/* Deep search results */}
      {searchResults.length > 0 && search.trim().length >= 3 && (
        <div className="sidebar-search-results">
          <div className="sidebar-search-results-label">Found in messages:</div>
          {searchResults.map((r: any) => (
            <div
              key={r.id}
              className={`session-item ${activeSessionId === r.id ? "active" : ""}`}
              onClick={() => { onSelectSession(r.id); setSearch(""); setSearchResults([]); }}
            >
              <span className="session-item-icon"><IconMessageSquare size={15} /></span>
              <div className="session-item-content">
                <div className="session-item-title">{r.title || "Untitled"}</div>
                <div className="session-item-date" style={{ fontSize: "11px", opacity: 0.7 }}>
                  ...{r.matches?.[0]?.content?.slice(0, 80)}...
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="sidebar-list">
        {sessions.length === 0 && (
          <div className="sidebar-empty">
            <div className="sidebar-empty-icon">
              <IconMessageSquare size={18} />
            </div>
            No conversations yet
          </div>
        )}
        {(search
          ? sessions.filter((s) => s.title.toLowerCase().includes(search.toLowerCase()))
          : sessions
        ).map((s) => (
          <React.Fragment key={s.id}>
            <div
              className={`session-item ${activeSessionId === s.id ? "active" : ""}`}
              onClick={() => onSelectSession(s.id)}
              aria-current={activeSessionId === s.id ? "true" : undefined}
            >
              <span className="session-item-icon">
                {workingSessions?.has(s.id) ? (
                  <span className="session-spinner" />
                ) : (
                  <IconMessageSquare size={15} />
                )}
              </span>
              <div className="session-item-content">
                <div className="session-item-title">{s.title}</div>
                <div className="session-item-date">{formatDate(s.updated_at)}</div>
              </div>
              <button
                className="session-item-branch"
                onClick={(e) => handleBranchToggle(e, s.id)}
                title="Show branches"
                aria-label="Show branches"
              >
                <IconGitBranch size={13} />
              </button>
              <button
                className="session-item-delete"
                onClick={(e) => handleDeleteClick(e, s.id)}
                title="Delete session"
                aria-label="Delete session"
              >
                <IconTrash size={13} />
              </button>
            </div>
            {expandedBranchSession === s.id && branches.length > 0 && (
              <div className="branch-list">
                {branches.map((b) => (
                  <div
                    key={b.id}
                    className="branch-item"
                    onClick={() => onForkToBranch(s.id, b.id)}
                    title={b.content}
                  >
                    <IconGitBranch size={11} />
                    <span className="branch-item-text">
                      {b.content.slice(0, 50)}{b.content.length > 50 ? "..." : ""}
                    </span>
                    <span className="branch-item-date">{formatDate(b.created_at)}</span>
                  </div>
                ))}
              </div>
            )}
          </React.Fragment>
        ))}
      </div>

      {/* Delete confirmation — portalled to body so backdrop-filter
          on .sidebar doesn't trap the fixed overlay */}
      {pendingDeleteId && ReactDOM.createPortal(
        <div
          className="modal-overlay"
          onClick={() => setPendingDeleteId(null)}
        >
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">Delete session</div>
            <div className="modal-text">
              Would you also like to delete memories created during this session?
            </div>
            <div className="modal-actions">
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => setPendingDeleteId(null)}
              >
                Cancel
              </button>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => executeDelete(false)}
              >
                Keep memories
              </button>
              <button
                className="btn btn-danger btn-sm"
                onClick={() => executeDelete(true)}
              >
                Delete all
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
};
