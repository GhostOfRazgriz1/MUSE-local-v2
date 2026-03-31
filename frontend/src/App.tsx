import { useState, useCallback, useEffect, useRef } from "react";
import { useWebSocket } from "./hooks/useWebSocket";
import { ChatStream } from "./components/ChatStream";
import { TaskTray } from "./components/TaskTray";
import { CostDashboard } from "./components/CostDashboard";
import { Settings } from "./components/settings";
import { SessionSidebar } from "./components/SessionSidebar";
import { FileBrowser } from "./components/FileBrowser";
import { SetupCard } from "./components/SetupCard";
import { IconSettings, IconBot, IconPanelRight, IconMenu, IconFolderOpen } from "./components/Icons";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { apiFetch } from "./hooks/useApiToken";
import { useNotifications } from "./hooks/useNotifications";
import type { SessionUsage, DisplayMessage, ApprovalMode } from "./types/events";

type View = "chat" | "settings";

function App() {
  const [requestedSessionId, setRequestedSessionId] = useState<string | null>(null);
  const [reconnectToken, setReconnectToken] = useState(0);
  const [sessionUpdateTrigger, setSessionUpdateTrigger] = useState(0);
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null); // null = loading

  const { sendMessage, sendRaw, connected, events, sessionId, historyMessages } =
    useWebSocket(requestedSessionId, reconnectToken, needsSetup !== false);
  const [view, setView] = useState<View>("chat");
  const [sidebarOpen, setSidebarOpen] = useState(true); // open by default on desktop
  const [taskPopoverOpen, setTaskPopoverOpen] = useState(false);
  const [fileBrowserOpen, setFileBrowserOpen] = useState(false);
  const [sessionTitle, setSessionTitle] = useState("");
  const taskBtnRef = useRef<HTMLButtonElement>(null);

  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const notifications = useNotifications();

  // Fire desktop notifications for important events when tab is hidden
  useEffect(() => {
    const last = events[events.length - 1];
    if (!last) return;
    if (last.type === "reminder") {
      notifications.notify("Reminder", last.what || last.content);
    } else if (last.type === "suggestion") {
      notifications.notify("MUSE", last.content);
    } else if (last.type === "autonomous_action") {
      notifications.notify(`MUSE ran ${last.skill_id}`, last.reason);
    } else if (last.type === "skill_notify") {
      notifications.notify("MUSE", last.message);
    }
  }, [events.length]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (historyMessages.length > 0) {
      // Defer to avoid setState-during-render when effects cascade
      queueMicrotask(() => setMessages(historyMessages));
    }
  }, [historyMessages]);

  useEffect(() => { setMessages([]); }, [requestedSessionId]);

  useEffect(() => {
    const last = events[events.length - 1];
    if (last && last.type === "session_updated") {
      setSessionUpdateTrigger((n) => n + 1);
      setSessionTitle(last.title);
    }
  }, [events]);

  useEffect(() => {
    if (sessionId) setSessionUpdateTrigger((n) => n + 1);
  }, [sessionId]);

  // Check if any LLM provider is configured — if not, show setup card
  useEffect(() => {
    apiFetch("/api/settings/providers")
      .then((r) => r.json())
      .then((data) => {
        const providers = data.providers || [];
        const hasAny = providers.some((p: { source: string | null }) => p.source != null);
        setNeedsSetup(!hasAny);
      })
      .catch(() => setNeedsSetup(false)); // if server is down, skip setup card
  }, []);

  // Close task popover when clicking outside
  useEffect(() => {
    if (!taskPopoverOpen) return;
    const onClick = (e: MouseEvent) => {
      const popover = document.querySelector(".task-popover");
      const btn = taskBtnRef.current;
      if (popover && !popover.contains(e.target as Node) && btn && !btn.contains(e.target as Node)) {
        setTaskPopoverOpen(false);
      }
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [taskPopoverOpen]);

  const sessionUsage: SessionUsage = (() => {
    let tokens_in = 0, tokens_out = 0;
    for (const msg of historyMessages) {
      if ("type" in msg && msg.type === "response") {
        tokens_in += msg.tokens_in || 0;
        tokens_out += msg.tokens_out || 0;
      }
    }
    for (const evt of events) {
      if (evt.type === "response") {
        tokens_in += evt.tokens_in;
        tokens_out += evt.tokens_out;
      }
    }
    return { tokens_in, tokens_out };
  })();

  const runningTaskCount = (() => {
    const started = new Set<string>();
    for (const evt of events) {
      if (evt.type === "task_started") started.add(evt.task_id);
      if (evt.type === "task_completed" || evt.type === "task_failed") started.delete(evt.task_id);
    }
    return started.size;
  })();

  const handlePermissionRespond = useCallback(
    (requestId: string, allow: boolean, mode?: ApprovalMode) => {
      sendRaw(allow
        ? { type: "approve_permission", request_id: requestId, approval_mode: mode ?? "once" }
        : { type: "deny_permission", request_id: requestId }
      );
    },
    [sendRaw]
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "N") {
        e.preventDefault(); handleSelectSession(null);
      }
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "S") {
        e.preventDefault(); setView((v) => (v === "settings" ? "chat" : "settings"));
      }
      if (e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const tag = (e.target as HTMLElement)?.tagName;
        if (tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") {
          e.preventDefault();
          document.querySelector<HTMLTextAreaElement>(".input-textarea")?.focus();
        }
      }
      if (e.key === "Escape" && taskPopoverOpen) setTaskPopoverOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [taskPopoverOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSelectSession = useCallback((id: string | null) => {
    setRequestedSessionId(id);
    setMessages([]);
    setSessionTitle("");
    setView("chat");
    // On mobile, close sidebar after selecting
    if (window.innerWidth < 768) setSidebarOpen(false);
    if (id === null) setReconnectToken((t) => t + 1);
  }, []);

  const handleFork = useCallback(async (messageId: number) => {
    if (!sessionId) return;
    try {
      const res = await apiFetch(`/api/sessions/${sessionId}/fork`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_id: messageId }),
      });
      if (res.ok) {
        // Reconnect to same session — branch_head_id is now updated server-side
        setReconnectToken((t) => t + 1);
        setMessages([]);
      }
    } catch {
      // ignore
    }
  }, [sessionId]);

  const handleForkToBranch = useCallback(async (sid: string, messageId: number) => {
    try {
      const res = await apiFetch(`/api/sessions/${sid}/fork`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_id: messageId }),
      });
      if (res.ok) {
        setRequestedSessionId(sid);
        setMessages([]);
        setReconnectToken((t) => t + 1);
      }
    } catch {
      // ignore
    }
  }, []);

  const handleUpload = useCallback(async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    const res = await apiFetch("/api/files/upload", { method: "POST", body: formData });
    if (!res.ok) throw new Error("Upload failed");
    return res.json();
  }, []);

  // First-run setup — show before anything else
  if (needsSetup === true) {
    return (
      <div className="app">
        <SetupCard onComplete={() => {
          setNeedsSetup(false);
          setReconnectToken((t) => t + 1);
        }} />
      </div>
    );
  }

  // Still checking — show nothing (brief flash)
  if (needsSetup === null) {
    return <div className="app" />;
  }

  return (
    <div className="app">
      <a href="#main-content" className="skip-link">Skip to content</a>

      <header className="topbar">
        <div className="topbar-left">
          <button
            className="topbar-btn"
            onClick={() => setSidebarOpen(!sidebarOpen)}
            aria-label={sidebarOpen ? "Close sidebar" : "Open sidebar"}
          >
            <IconMenu size={18} />
          </button>
          <div className="topbar-logo">
            <div className="topbar-logo-icon">
              <IconBot size={16} />
            </div>
            <span className="topbar-title">MUSE</span>
          </div>
          {sessionTitle && view === "chat" && (
            <>
              <span className="topbar-sep">/</span>
              <span className="topbar-session-title">{sessionTitle}</span>
            </>
          )}
          <span
            className={`connection-dot ${connected ? "connected" : "disconnected"}`}
            title={connected ? "Connected" : "Disconnected"}
          />
        </div>
        <div className="topbar-right">
          <CostDashboard usage={sessionUsage} />
          {view === "chat" && (
            <>
            <button
              className={`topbar-btn ${fileBrowserOpen ? "active" : ""}`}
              onClick={() => setFileBrowserOpen(!fileBrowserOpen)}
              title="Files"
              aria-label="File browser"
            >
              <IconFolderOpen size={18} />
            </button>
            <div className="task-popover-anchor">
              <button
                ref={taskBtnRef}
                className={`topbar-btn ${taskPopoverOpen ? "active" : ""}`}
                onClick={() => setTaskPopoverOpen(!taskPopoverOpen)}
                title="Tasks"
                aria-label="Tasks"
              >
                <IconPanelRight size={18} />
                {runningTaskCount > 0 && (
                  <span className="topbar-badge">{runningTaskCount}</span>
                )}
              </button>
              <div className="task-popover" style={{ display: taskPopoverOpen ? undefined : "none" }}>
                <TaskTray events={events} />
              </div>
            </div>
            </>
          )}
          <button
            className={`topbar-btn ${view === "settings" ? "active" : ""}`}
            onClick={() => setView(view === "settings" ? "chat" : "settings")}
            title="Settings"
            aria-label="Settings"
          >
            <IconSettings size={18} />
          </button>
        </div>
      </header>

      <ErrorBoundary>
      <div className="body">
        {/* Scrim for mobile sidebar */}
        {sidebarOpen && <div className="sidebar-scrim" onClick={() => setSidebarOpen(false)} />}

        <div className={`sidebar-wrapper ${sidebarOpen ? "open" : ""}`}>
          <SessionSidebar
            activeSessionId={sessionId}
            onSelectSession={handleSelectSession}
            onForkToBranch={handleForkToBranch}
            sessionUpdateTrigger={sessionUpdateTrigger}
          />
        </div>

        <div
          id="main-content"
          className="chat-area"
          style={{ display: view === "chat" ? "flex" : "none" }}
        >
          <ChatStream
            events={events}
            connected={connected}
            onSend={sendMessage}
            onPermissionRespond={handlePermissionRespond}
            onUserResponse={(requestId, response) => {
              sendRaw({ type: "user_response", request_id: requestId, response });
            }}
            onSteer={(content) => sendRaw({ type: "steer", content })}
            onFork={handleFork}
            onRegenerate={() => {
              // Remove the last assistant message so the new response replaces it
              setMessages((prev) => {
                for (let i = prev.length - 1; i >= 0; i--) {
                  const msg = prev[i];
                  if ("type" in msg && (msg.type === "response" || msg.type === "response_chunk")) {
                    return [...prev.slice(0, i)];
                  }
                }
                return prev;
              });
              sendRaw({ type: "regenerate" });
            }}
            onUpload={handleUpload}
            onSuggestionFeedback={(sid, accepted) => {
              sendRaw({ type: "suggestion_feedback", suggestion_id: sid, accepted });
            }}
            messages={messages}
            setMessages={setMessages}
          />
        </div>

        {fileBrowserOpen && view === "chat" && (
          <div className="file-browser-panel">
            <FileBrowser onClose={() => setFileBrowserOpen(false)} />
          </div>
        )}

        {view === "settings" && (
          <div style={{ flex: 1, overflow: "hidden" }}>
            <Settings onBack={() => setView("chat")} />
          </div>
        )}
      </div>
      </ErrorBoundary>
    </div>
  );
}

export default App;
