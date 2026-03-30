import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatEvent, DisplayMessage, HistoryMessage } from "../types/events";
import { getApiToken } from "./useApiToken";

// Derive WebSocket and API URLs from the current page location.
// In dev (Vite proxy), this resolves to localhost:3000 → proxied to :8080.
// In production (static files served by backend), this hits :8080 directly.
const _loc = typeof window !== "undefined" ? window.location : { protocol: "http:", host: "localhost:8080" };
const _wsProto = _loc.protocol === "https:" ? "wss:" : "ws:";
const WS_BASE = `${_wsProto}//${_loc.host}/api/ws/chat`;
const API_BASE = `${_loc.protocol}//${_loc.host}/api`;
const MAX_BACKOFF = 30000;
const INITIAL_BACKOFF = 1000;

export interface UseWebSocketReturn {
  sendMessage: (content: string) => void;
  sendRaw: (data: object) => void;
  lastEvent: ChatEvent | null;
  connected: boolean;
  events: ChatEvent[];
  /** The active session id reported by the server */
  sessionId: string | null;
  /** Restored history messages (sent once on connect) */
  historyMessages: DisplayMessage[];
}

export function useWebSocket(requestedSessionId: string | null, reconnectToken?: number): UseWebSocketReturn {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [lastEvent, setLastEvent] = useState<ChatEvent | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [historyMessages, setHistoryMessages] = useState<DisplayMessage[]>([]);

  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(INITIAL_BACKOFF);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const requestedSessionRef = useRef(requestedSessionId);

  // Track whether the user sent at least one message in this session.
  // If they didn't and the session was newly created (not resumed),
  // we delete it on disconnect to avoid phantom empty sessions.
  const userSentMessageRef = useRef(false);
  const sessionIdRef = useRef<string | null>(null);
  // true when we opened a NEW session (requestedSessionId was null),
  // false when we resumed an existing one.
  const isNewSessionRef = useRef(!requestedSessionId);
  // true if restored history had messages (session already had content)
  const hadHistoryRef = useRef(false);

  // Keep ref in sync so reconnect uses latest value
  useEffect(() => {
    requestedSessionRef.current = requestedSessionId;
  }, [requestedSessionId]);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN || wsRef.current?.readyState === WebSocket.CONNECTING) return;

    // Fetch token first, then open WebSocket
    getApiToken().then((apiToken) => {
      if (!mountedRef.current) return;
      if (wsRef.current?.readyState === WebSocket.OPEN || wsRef.current?.readyState === WebSocket.CONNECTING) return;

    try {
      const params = new URLSearchParams();
      if (requestedSessionRef.current) {
        params.set("session_id", requestedSessionRef.current);
      }
      if (apiToken) {
        params.set("token", apiToken);
      }
      // Send the browser's timezone so the agent knows the user's local time
      try {
        params.set("tz", Intl.DateTimeFormat().resolvedOptions().timeZone);
      } catch { /* Intl not available — server falls back to UTC */ }
      const qs = params.toString();
      const url = qs ? `${WS_BASE}?${qs}` : WS_BASE;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        if (!mountedRef.current) return;
        setConnected(true);
        backoffRef.current = INITIAL_BACKOFF;
      };

      ws.onmessage = (event) => {
        if (!mountedRef.current) return;
        try {
          const parsed: ChatEvent = JSON.parse(event.data);

          // Intercept session-level events
          if (parsed.type === "session_started") {
            sessionIdRef.current = parsed.session_id;
            setSessionId(parsed.session_id);
            return;
          }
          if (parsed.type === "history") {
            const msgs = (parsed as any).messages || [];
            // If the restored history contains user messages, the session
            // already had interaction — don't clean it up on close.
            if (msgs.some((m: HistoryMessage) => m.role === "user")) {
              hadHistoryRef.current = true;
            }
            // Convert persisted messages into DisplayMessage[]
            const restored: DisplayMessage[] = msgs.map(
              (m: HistoryMessage) => {
                if (m.role === "user") {
                  return { role: "user" as const, content: m.content, _id: m.id, _dbId: m.id, _createdAt: m.created_at };
                }
                // Assistant messages — reconstruct as a response event
                const meta = m.metadata || {};
                return {
                  type: "response" as const,
                  content: m.content,
                  tokens_in: (meta.tokens_in as number) || 0,
                  tokens_out: (meta.tokens_out as number) || 0,
                  cost_usd: (meta.cost_usd as number) || 0,
                  model: (meta.model as string) || "",
                  _dbId: m.id,
                  _createdAt: m.created_at,
                };
              }
            );
            setHistoryMessages(restored);
            // Clear events so previously-accumulated events don't overlap
            // with the history that was just loaded.
            setEvents([]);
            return;
          }
          if (parsed.type === "session_updated") {
            // Bubble up but don't add to chat events
            setEvents((prev) => [...prev, parsed]);
            setLastEvent(parsed);
            return;
          }

          setEvents((prev) => [...prev, parsed]);
          setLastEvent(parsed);
        } catch {
          // Ignore malformed messages
        }
      };

      ws.onclose = () => {
        if (!mountedRef.current) return;
        setConnected(false);
        wsRef.current = null;
        scheduleReconnect();
      };

      ws.onerror = () => {
        if (!mountedRef.current) return;
        ws.close();
      };
    } catch {
      scheduleReconnect();
    }
    }).catch(() => { scheduleReconnect(); });
  }, []);

  const scheduleReconnect = useCallback(() => {
    if (!mountedRef.current) return;
    if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);

    const delay = backoffRef.current;
    backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF);

    reconnectTimerRef.current = setTimeout(() => {
      if (mountedRef.current) connect();
    }, delay);
  }, [connect]);

  const sendMessage = useCallback((content: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      userSentMessageRef.current = true;
      wsRef.current.send(JSON.stringify({ type: "message", content }));
    }
  }, []);

  const sendRaw = useCallback((data: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  // Delete an empty session that was newly created but never used.
  // Uses authenticated fetch through the Vite proxy for session switches,
  // and sendBeacon (unauthenticated, best-effort) for page unload only.
  const cleanupEmptySession = useCallback((isUnload = false) => {
    const sid = sessionIdRef.current;
    if (!sid) return;
    if (userSentMessageRef.current) return;  // user interacted — keep it
    if (!isNewSessionRef.current) return;     // resumed existing — keep it
    if (hadHistoryRef.current) return;        // had prior messages — keep it

    if (isUnload) {
      // Page unload — sendBeacon is best-effort (can't carry auth)
      const url = `${API_BASE}/sessions/${sid}/close`;
      navigator.sendBeacon?.(url, new Blob([], { type: "text/plain" }));
      return;
    }

    // Normal session switch — use authenticated fetch via proxy
    const relUrl = `/api/sessions/${sid}/close`;
    getApiToken().then((t) => {
      const headers: Record<string, string> = {};
      if (t) headers["Authorization"] = `Bearer ${t}`;
      fetch(relUrl, { method: "POST", keepalive: true, headers }).catch(() => {});
    }).catch(() => {});
  }, []);

  // Register a beforeunload listener — this is the ONLY reliable way to
  // fire cleanup when the user closes the tab or browser. React's
  // useEffect cleanup does NOT run on tab close.
  useEffect(() => {
    const onBeforeUnload = () => cleanupEmptySession(true);
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [cleanupEmptySession]);

  // Reconnect when the requested session changes
  useEffect(() => {
    mountedRef.current = true;

    // Before switching sessions, clean up the previous one if empty
    cleanupEmptySession();

    // Reset tracking for the new session
    userSentMessageRef.current = false;
    sessionIdRef.current = null;
    isNewSessionRef.current = !requestedSessionId;
    hadHistoryRef.current = false;

    // Reset UI state
    setEvents([]);
    setLastEvent(null);
    setSessionId(null);
    setHistoryMessages([]);
    // Close existing connection and reconnect
    if (wsRef.current) {
      wsRef.current.onclose = null;
      wsRef.current.close();
      wsRef.current = null;
    }
    if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
    backoffRef.current = INITIAL_BACKOFF;
    connect();

    return () => {
      mountedRef.current = false;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [requestedSessionId, reconnectToken, connect, cleanupEmptySession]);

  return { sendMessage, sendRaw, lastEvent, connected, events, sessionId, historyMessages };
}
