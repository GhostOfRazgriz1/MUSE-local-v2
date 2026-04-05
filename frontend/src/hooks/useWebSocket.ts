import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatEvent, DisplayMessage, HistoryMessage } from "../types/events";
import { getApiToken } from "./useApiToken";
import { structuralCompactEvents } from "../utils/compaction";

// Derive WebSocket and API URLs from the current page location.
// In dev (Vite proxy), this resolves to localhost:3000 → proxied to :8080.
// In production (static files served by backend), this hits :8080 directly.
const _loc = typeof window !== "undefined" ? window.location : { protocol: "http:", host: "localhost:8080" };
const _wsProto = _loc.protocol === "https:" ? "wss:" : "ws:";
const WS_BASE = `${_wsProto}//${_loc.host}/api/ws/chat`;
const MAX_BACKOFF = 30000;
const INITIAL_BACKOFF = 1000;
const MAX_EVENTS = 500;

/** Event types that create pending user interactions. */
const INTERACTION_START_TYPES = new Set(["permission_request", "skill_question", "skill_confirm"]);
/** Event types that resolve pending interactions. */
const INTERACTION_END_TYPES = new Set(["permission_approved", "permission_denied"]);

/** Notification that a background session received a completion. */
export interface BackgroundSessionNotification {
  sessionId: string;
  summary: string;
  timestamp: number;
}

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
  /** Request IDs of unresolved interactive prompts */
  pendingInteractions: Set<string>;
  /** Sessions that completed work while the user was on a different session */
  backgroundNotifications: BackgroundSessionNotification[];
  /** Clear a background notification after the user acknowledges it */
  clearBackgroundNotification: (sessionId: string) => void;
}

export function useWebSocket(requestedSessionId: string | null, reconnectToken?: number, paused?: boolean): UseWebSocketReturn {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [historyMessages, setHistoryMessages] = useState<DisplayMessage[]>([]);

  const [pendingInteractions, setPendingInteractions] = useState<Set<string>>(new Set());
  const pendingRef = useRef<Set<string>>(new Set());
  const [backgroundNotifications, setBackgroundNotifications] = useState<BackgroundSessionNotification[]>([]);

  const clearBackgroundNotification = useCallback((sid: string) => {
    setBackgroundNotifications((prev) => prev.filter((n) => n.sessionId !== sid));
  }, []);

  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(INITIAL_BACKOFF);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const requestedSessionRef = useRef(requestedSessionId);
  // Generation counter — incremented on every session switch. Events from
  // a stale generation are dropped to prevent cross-session leakage.
  const generationRef = useRef(0);

  // Keep ref in sync so reconnect uses latest value
  useEffect(() => {
    requestedSessionRef.current = requestedSessionId;
  }, [requestedSessionId]);

  const pausedRef = useRef(!!paused);
  pausedRef.current = !!paused;

  const connect = useCallback(() => {
    if (pausedRef.current) return;
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
      const connGeneration = generationRef.current;
      const connSessionId = requestedSessionRef.current;

      ws.onopen = () => {
        if (!mountedRef.current || connGeneration !== generationRef.current) return;
        setConnected(true);
        backoffRef.current = INITIAL_BACKOFF;
      };

      ws.onmessage = (event) => {
        if (!mountedRef.current) return;
        // Drop events from a stale session — prevents cross-session leakage
        // when the user switches sessions while a response is in flight.
        if (connGeneration !== generationRef.current) {
          // But intercept completion events and surface as notifications
          // so the user knows the background session finished.
          try {
            const stale: ChatEvent = JSON.parse(event.data);
            if (connSessionId && (
              stale.type === "response" ||
              stale.type === "task_completed" ||
              stale.type === "multi_task_completed" ||
              stale.type === "error"
            )) {
              const summary = stale.type === "response"
                ? (stale.content || "").slice(0, 80)
                : stale.type === "error"
                  ? "Task failed"
                  : "Task completed";
              setBackgroundNotifications((prev) => {
                // Don't duplicate for same session
                if (prev.some((n) => n.sessionId === connSessionId)) return prev;
                return [...prev, { sessionId: connSessionId, summary, timestamp: Date.now() }];
              });
            }
          } catch { /* ignore parse errors on stale events */ }
          return;
        }
        try {
          const parsed: ChatEvent = JSON.parse(event.data);

          // Intercept session-level events
          if (parsed.type === "session_started") {
            setSessionId(parsed.session_id);
            return;
          }
          if (parsed.type === "history") {
            const msgs = (parsed as any).messages || [];
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
          // Track pending interactive events
          if (INTERACTION_START_TYPES.has(parsed.type) && "request_id" in parsed) {
            pendingRef.current = new Set(pendingRef.current).add(parsed.request_id);
            setPendingInteractions(pendingRef.current);
          } else if (INTERACTION_END_TYPES.has(parsed.type) && "request_id" in parsed) {
            const next = new Set(pendingRef.current);
            next.delete(parsed.request_id);
            pendingRef.current = next;
            setPendingInteractions(next);
          }

          setEvents((prev) => {
            const next = [...prev, parsed];
            return next.length > MAX_EVENTS
              ? structuralCompactEvents(next, pendingRef.current)
              : next;
          });
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
      wsRef.current.send(JSON.stringify({ type: "message", content }));
    }
  }, []);

  const sendRaw = useCallback((data: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  // Reconnect when the requested session or paused state changes
  useEffect(() => {
    mountedRef.current = true;
    // Bump generation so any in-flight events from the old session are dropped.
    generationRef.current += 1;

    // Reset UI state
    setEvents([]);
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
  }, [requestedSessionId, reconnectToken, paused, connect]);

  const lastEvent = events.length > 0 ? events[events.length - 1] : null;

  return { sendMessage, sendRaw, lastEvent, connected, events, sessionId, historyMessages, pendingInteractions, backgroundNotifications, clearBackgroundNotification };
}
