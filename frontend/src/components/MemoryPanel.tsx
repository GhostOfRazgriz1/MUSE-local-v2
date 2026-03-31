import React, { useState, useEffect, useCallback, useRef } from "react";
import { IconX, IconTrash, IconPlus, IconBrain, IconChevronDown, IconChevronRight, IconCheck } from "./Icons";
import { apiFetch } from "../hooks/useApiToken";

interface MemoryItem {
  id: number;
  namespace: string;
  namespace_label: string;
  key: string;
  value: string;
  created_at: string;
  updated_at: string;
  access_count: number;
}

interface RelationshipInfo {
  level: number;
  label: string;
  progress: number;
  capabilities: string[];
  next_capabilities: string[];
}

interface MemoryPanelProps {
  onClose: () => void;
}

/** Order for known namespace categories. */
const NS_ORDER: Record<string, number> = {
  "_profile": 0,
  "_emotions": 1,
  "_facts": 2,
  "_project": 3,
  "_patterns": 4,
  "_conversation": 4,
  "_system": 5,
  "_scheduled": 6,
};

function nsSort(a: string, b: string): number {
  const oa = NS_ORDER[a] ?? 99;
  const ob = NS_ORDER[b] ?? 99;
  return oa - ob || a.localeCompare(b);
}

/** Format ISO date to a friendly relative or short date. */
function friendlyDate(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffDays = Math.floor(diffMs / 86_400_000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays} days ago`;
  if (diffDays < 30) return `${Math.floor(diffDays / 7)}w ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export const MemoryPanel: React.FC<MemoryPanelProps> = ({ onClose }) => {
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [groups, setGroups] = useState<Record<string, MemoryItem[]>>({});
  const [relationship, setRelationship] = useState<RelationshipInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [collapsedNs, setCollapsedNs] = useState<Set<string>>(new Set());
  const [addingValue, setAddingValue] = useState("");
  const [addingOpen, setAddingOpen] = useState(false);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const addInputRef = useRef<HTMLInputElement>(null);

  const fetchMemories = useCallback(async () => {
    try {
      const [memRes, statsRes] = await Promise.all([
        apiFetch("/api/memories"),
        apiFetch("/api/memories/stats"),
      ]);
      if (memRes.ok) {
        const data = await memRes.json();
        setItems(data.memories || []);
        setGroups(data.groups || {});
      }
      if (statsRes.ok) {
        const stats = await statsRes.json();
        if (stats.relationship) setRelationship(stats.relationship);
      }
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchMemories();
  }, [fetchMemories]);

  useEffect(() => {
    if (addingOpen && addInputRef.current) addInputRef.current.focus();
  }, [addingOpen]);

  const handleDelete = useCallback(async (ns: string, key: string) => {
    setDeletingKey(`${ns}/${key}`);
    try {
      const res = await apiFetch(`/api/memories/${encodeURIComponent(ns)}/${encodeURIComponent(key)}`, {
        method: "DELETE",
      });
      if (res.ok) {
        setItems((prev) => prev.filter((m) => !(m.namespace === ns && m.key === key)));
        setGroups((prev) => {
          const next = { ...prev };
          if (next[ns]) {
            next[ns] = next[ns].filter((m) => m.key !== key);
            if (next[ns].length === 0) delete next[ns];
          }
          return next;
        });
      }
    } catch {
      // ignore
    } finally {
      setDeletingKey(null);
    }
  }, []);

  const handleAdd = useCallback(async () => {
    const val = addingValue.trim();
    if (!val) return;
    try {
      const res = await apiFetch("/api/memories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: val, namespace: "_profile" }),
      });
      if (res.ok) {
        setAddingValue("");
        setAddingOpen(false);
        fetchMemories();
      }
    } catch {
      // ignore
    }
  }, [addingValue, fetchMemories]);

  const toggleNs = useCallback((ns: string) => {
    setCollapsedNs((prev) => {
      const next = new Set(prev);
      if (next.has(ns)) next.delete(ns);
      else next.add(ns);
      return next;
    });
  }, []);

  const namespaces = Object.keys(groups).sort(nsSort);
  const totalCount = items.length;

  // Build timeline: all items sorted by created_at desc.
  const timeline = [...items].sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  );

  // Group timeline by date label.
  const timelineGroups: { label: string; items: MemoryItem[] }[] = [];
  for (const item of timeline) {
    const label = friendlyDate(item.created_at);
    const last = timelineGroups[timelineGroups.length - 1];
    if (last && last.label === label) {
      last.items.push(item);
    } else {
      timelineGroups.push({ label, items: [item] });
    }
  }

  return (
    <div className="memory-panel">
      {/* Header */}
      <div className="memory-panel-header">
        <div className="memory-panel-title">
          <IconBrain size={16} />
          <span>What I Know</span>
        </div>
        <button className="memory-panel-close" onClick={onClose} aria-label="Close">
          <IconX size={16} />
        </button>
      </div>

      <div className="memory-panel-body">
        {loading ? (
          <div className="memory-panel-empty">Loading memories...</div>
        ) : totalCount === 0 ? (
          <div className="memory-panel-empty">
            <IconBrain size={24} style={{ opacity: 0.3 }} />
            <p>I'm still getting to know you.</p>
            <p className="memory-panel-empty-sub">
              Tell me something about yourself, or just keep chatting — I'll pick things up naturally.
            </p>
            <button className="memory-add-btn" onClick={() => setAddingOpen(true)}>
              <IconPlus size={14} />
              Tell me something
            </button>
          </div>
        ) : (
          <>
            {/* Relationship progression */}
            {relationship && (
              <div className="memory-progression">
                <div className="memory-progression-header">
                  <span className="memory-progression-label">{relationship.label}</span>
                </div>
                <div className="memory-progression-bar-track">
                  <div
                    className="memory-progression-bar-fill"
                    style={{ width: `${Math.round(relationship.progress * 100)}%` }}
                  />
                </div>
                <div className="memory-progression-caps">
                  {relationship.capabilities.map((cap) => (
                    <div key={cap} className="memory-cap-item unlocked">
                      <IconCheck size={12} />
                      <span>{cap}</span>
                    </div>
                  ))}
                  {relationship.next_capabilities.map((cap) => (
                    <div key={cap} className="memory-cap-item locked">
                      <span className="memory-cap-dot" />
                      <span>{cap}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Profile card — grouped by namespace */}
            <div className="memory-profile-section">
              <div className="memory-section-header">
                <span className="memory-section-label">
                  {totalCount} {totalCount === 1 ? "memory" : "memories"}
                </span>
                <button
                  className="memory-add-btn-sm"
                  onClick={() => setAddingOpen(!addingOpen)}
                  title="Add a memory"
                >
                  <IconPlus size={14} />
                </button>
              </div>

              {addingOpen && (
                <div className="memory-add-row">
                  <input
                    ref={addInputRef}
                    className="memory-add-input"
                    type="text"
                    placeholder="e.g. I like sushi, I work at..."
                    value={addingValue}
                    onChange={(e) => setAddingValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleAdd();
                      if (e.key === "Escape") { setAddingOpen(false); setAddingValue(""); }
                    }}
                  />
                  <button className="memory-add-confirm" onClick={handleAdd} disabled={!addingValue.trim()}>
                    Add
                  </button>
                </div>
              )}

              {namespaces.map((ns) => {
                const nsItems = groups[ns] || [];
                const label = nsItems[0]?.namespace_label || ns;
                const collapsed = collapsedNs.has(ns);
                return (
                  <div key={ns} className="memory-ns-group">
                    <button className="memory-ns-toggle" onClick={() => toggleNs(ns)}>
                      {collapsed ? <IconChevronRight size={14} /> : <IconChevronDown size={14} />}
                      <span className="memory-ns-label">{label}</span>
                      <span className="memory-ns-count">{nsItems.length}</span>
                    </button>
                    {!collapsed && (
                      <div className="memory-ns-items">
                        {nsItems.map((m) => (
                          <div key={`${m.namespace}/${m.key}`} className="memory-item">
                            <span className="memory-item-value">{m.value}</span>
                            <button
                              className="memory-item-delete"
                              onClick={() => handleDelete(m.namespace, m.key)}
                              disabled={deletingKey === `${m.namespace}/${m.key}`}
                              title="Forget this"
                              aria-label="Delete memory"
                            >
                              <IconTrash size={12} />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            {/* Timeline section */}
            <div className="memory-timeline-section">
              <div className="memory-section-header">
                <span className="memory-section-label">Timeline</span>
              </div>
              {timelineGroups.map((tg) => (
                <div key={tg.label} className="memory-timeline-day">
                  <div className="memory-timeline-date">{tg.label}</div>
                  {tg.items.map((m) => (
                    <div key={`${m.namespace}/${m.key}`} className="memory-timeline-item">
                      <div className="memory-timeline-dot" />
                      <div className="memory-timeline-content">
                        <span className="memory-timeline-text">{m.value}</span>
                        <span className="memory-timeline-ns">{m.namespace_label}</span>
                      </div>
                      <button
                        className="memory-item-delete"
                        onClick={() => handleDelete(m.namespace, m.key)}
                        disabled={deletingKey === `${m.namespace}/${m.key}`}
                        title="Forget this"
                        aria-label="Delete memory"
                      >
                        <IconTrash size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
};
