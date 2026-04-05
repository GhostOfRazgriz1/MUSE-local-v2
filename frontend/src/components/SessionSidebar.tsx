import React, { useEffect, useState, useCallback, useRef } from "react";
import ReactDOM from "react-dom";
import { IconMessageSquare, IconPlus, IconTrash, IconGitBranch } from "./Icons";
import { apiFetch } from "../hooks/useApiToken";
import { useLocale, type TranslateFn } from "../i18n";
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

function formatDate(iso: string, t: TranslateFn): string {
  const d = new Date(iso);
  const now = new Date();
  // Compare by calendar date (midnight-to-midnight), not elapsed time
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const msgDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.round((today.getTime() - msgDay.getTime()) / 86400000);
  if (diffDays === 0) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (diffDays === 1) return t("date_yesterday");
  if (diffDays < 7) return t("date_days_ago", { n: diffDays });
  if (diffDays < 14) return t("date_a_week_ago");
  if (diffDays < 30) return t("date_weeks_ago", { n: Math.floor(diffDays / 7) });
  if (diffDays < 60) return t("date_a_month_ago");
  if (diffDays < 365) return t("date_months_ago", { n: Math.floor(diffDays / 30) });
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

export const SessionSidebar: React.FC<SessionSidebarProps> = ({
  activeSessionId,
  workingSessions,
  onSelectSession,
  onForkToBranch,
  sessionUpdateTrigger,
}) => {
  const { t } = useLocale();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [search, setSearch] = useState("");
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [expandedBranchSession, setExpandedBranchSession] = useState<string | null>(null);
  const [branches, setBranches] = useState<BranchTip[]>([]);
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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
        <span className="sidebar-title">{t("sidebar_sessions")}</span>
        <button
          className="sidebar-new-btn"
          onClick={handleNew}
          title={t("sidebar_new_conversation")}
        >
          <IconPlus size={14} />
          {t("sidebar_new")}
        </button>
      </div>

      {sessions.length > 3 && (
        <div className="sidebar-search">
          <input
            type="text"
            className="sidebar-search-input"
            placeholder={t("sidebar_search")}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              // Debounced deep search via API when query is 3+ chars
              if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
              const q = e.target.value.trim();
              if (q.length >= 3) {
                searchTimerRef.current = setTimeout(() => {
                  apiFetch(`/api/sessions/search?q=${encodeURIComponent(q)}&limit=10`)
                    .then((r) => r.json())
                    .then((d) => setSearchResults(d.results || []))
                    .catch(() => setSearchResults([]));
                }, 300);
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
          <div className="sidebar-search-results-label">{t("sidebar_found_in")}</div>
          {searchResults.map((r: any) => (
            <div
              key={r.id}
              className={`session-item ${activeSessionId === r.id ? "active" : ""}`}
              onClick={() => { onSelectSession(r.id); setSearch(""); setSearchResults([]); }}
            >
              <span className="session-item-icon"><IconMessageSquare size={15} /></span>
              <div className="session-item-content">
                <div className="session-item-title">{r.title || t("sidebar_untitled")}</div>
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
            {t("sidebar_no_conversations")}
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
                <div className="session-item-date">{formatDate(s.updated_at, t)}</div>
              </div>
              <button
                className="session-item-branch"
                onClick={(e) => handleBranchToggle(e, s.id)}
                title={t("sidebar_show_branches")}
                aria-label={t("sidebar_show_branches")}
              >
                <IconGitBranch size={13} />
              </button>
              <button
                className="session-item-delete"
                onClick={(e) => handleDeleteClick(e, s.id)}
                title={t("sidebar_delete_title")}
                aria-label={t("sidebar_delete_title")}
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
                    <span className="branch-item-date">{formatDate(b.created_at, t)}</span>
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
            <div className="modal-title">{t("sidebar_delete_title")}</div>
            <div className="modal-text">
              {t("sidebar_delete_confirm")}
            </div>
            <div className="modal-actions">
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => setPendingDeleteId(null)}
              >
                {t("cancel")}
              </button>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => executeDelete(false)}
              >
                {t("sidebar_keep_memories")}
              </button>
              <button
                className="btn btn-danger btn-sm"
                onClick={() => executeDelete(true)}
              >
                {t("sidebar_delete_all")}
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
};
