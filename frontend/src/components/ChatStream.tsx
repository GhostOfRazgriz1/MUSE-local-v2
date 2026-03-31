import React, { useState, useRef, useEffect, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { IconSend, IconBot, IconShield, IconAlertCircle, IconCopy, IconClipboardCheck, IconCheck, IconChevronDown, IconGitBranch, IconNavigation, IconClock, IconPaperclip, IconFileText, IconDownload, IconX, IconRefresh } from "./Icons";
import { renderWithFileChips } from "./FileChip";
import type { ChatEvent, DisplayMessage, ApprovalMode } from "../types/events";

interface ChatStreamProps {
  events: ChatEvent[];
  connected: boolean;
  agentMood?: string;
  onSend: (content: string) => void;
  onPermissionRespond: (requestId: string, allow: boolean, mode?: ApprovalMode) => void;
  onUserResponse: (requestId: string, response: unknown) => void;
  onSteer: (content: string) => void;
  onFork: (messageId: number) => void;
  onUpload: (file: File) => Promise<{ path: string; filename: string }>;
  onSuggestionFeedback: (suggestionId: string, accepted: boolean) => void;
  onOpenMemories?: () => void;
  onRegenerate: () => void;
  messages: DisplayMessage[];
  setMessages: React.Dispatch<React.SetStateAction<DisplayMessage[]>>;
}

const remarkPlugins = [remarkGfm];

/**
 * Strip LLM-hallucinated tool-call XML (e.g. <function_calls>, <function_result>)
 * that should never be rendered.  Acts as a client-side safety net in case the
 * backend sanitiser misses something.
 */
const TOOL_BLOCK_RE =
  /<\s*(?:function_calls|function_result|invoke|parameter|tool_call|tool_result)[\s\S]*?<\s*\/\s*(?:function_calls|function_result|invoke|parameter|tool_call|tool_result)\s*>/gi;
const TOOL_TAG_RE =
  /<\s*\/?\s*(?:function_calls|function_result|invoke|parameter|tool_call|tool_result)(?:\s[^>]*)?\s*\/?>/gi;

function stripToolBlocks(text: string): string {
  return text
    .replace(TOOL_BLOCK_RE, "")
    .replace(TOOL_TAG_RE, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** Copy-to-clipboard button with visual feedback. */
const CopyButton: React.FC<{ text: string; className?: string; label?: string }> = ({
  text,
  className = "copy-btn",
  label = "Copy",
}) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {}
  }, [text]);

  return (
    <button
      className={className}
      onClick={handleCopy}
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied!" : label}
    >
      {copied ? <IconClipboardCheck size={14} /> : <IconCopy size={14} />}
    </button>
  );
};

/** Detect file-write responses from the Files skill. */
const FILE_WRITE_RE = /^(Created|Overwrote) \*\*(.+?)\*\* \((.+?)\)\n\n {2}(.+?)\n\n```\n([\s\S]*?)(?:\n```|$)/;

function parseFileCard(content: string): {
  action: string; filename: string; size: string; path: string; preview: string;
} | null {
  const m = content.match(FILE_WRITE_RE);
  if (!m) return null;
  return { action: m[1], filename: m[2], size: m[3], path: m[4], preview: m[5] };
}

/** File preview card for skill-created files. */
const FileCard: React.FC<{ info: ReturnType<typeof parseFileCard> & {} }> = ({ info }) => {
  const handleReveal = async () => {
    try {
      const { apiFetch } = await import("../hooks/useApiToken");
      await apiFetch("/api/files/reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: info.path }),
      });
    } catch {}
  };

  const handleDownload = async () => {
    try {
      const { apiFetch } = await import("../hooks/useApiToken");
      const res = await apiFetch(`/api/files/download?path=${encodeURIComponent(info.path)}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = info.path.split(/[/\\]/).pop() || "download";
      a.click();
      URL.revokeObjectURL(url);
    } catch {}
  };

  return (
    <div className="file-card">
      <div className="file-card-header">
        <IconFileText size={16} />
        <span className="file-card-name">{info.filename}</span>
        <span className="file-card-size">{info.size}</span>
      </div>
      {info.preview && (
        <pre className="file-card-preview">{info.preview.slice(0, 500)}</pre>
      )}
      <div className="file-card-actions">
        <button className="btn btn-ghost btn-sm" onClick={handleReveal}>
          Show in folder
        </button>
        <button className="btn btn-ghost btn-sm" onClick={handleDownload}>
          <IconDownload size={12} /> Download
        </button>
        <CopyButton text={info.preview || ""} className="msg-copy-btn" label="Copy content" />
      </div>
    </div>
  );
};

/** Render markdown content for agent messages. */
const MarkdownContent: React.FC<{ content: string }> = React.memo(({ content }) => {
  const cleaned = stripToolBlocks(content);
  return (
    <div className="md-content">
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        components={{
          code({ className, children, ...props }) {
            const isBlock = className?.startsWith("language-");
            if (isBlock) {
              return (
                <code className={`md-code-block-code ${className ?? ""}`} {...props}>
                  {children}
                </code>
              );
            }
            return (
              <code className="md-inline-code" {...props}>
                {children}
              </code>
            );
          },
          pre({ children }) {
            const codeText = extractText(children);
            // Extract language from <code className="language-xxx">
            let lang = "";
            if (children && typeof children === "object" && "props" in (children as React.ReactElement)) {
              const cls = (children as React.ReactElement).props?.className || "";
              const m = cls.match(/language-(\w+)/);
              if (m) lang = m[1];
            }
            return (
              <div className="md-code-block-wrapper">
                <div className="code-block-header">
                  {lang && <span className="code-lang-label">{lang}</span>}
                  <CopyButton text={codeText} className="code-copy-btn" label="Copy code" />
                </div>
                <pre className="md-code-block">{children}</pre>
              </div>
            );
          },
          a({ href, children }) {
            return (
              <a href={href} target="_blank" rel="noopener noreferrer" className="md-link">
                {children}
              </a>
            );
          },
          // Detect file paths in text content and render FileChips
          p({ children }) {
            return <p>{processChildren(children)}</p>;
          },
          li({ children }) {
            return <li>{processChildren(children)}</li>;
          },
        }}
      >
        {cleaned}
      </ReactMarkdown>
    </div>
  );
});

/** Recursively extract text content from React children. */
function extractText(node: React.ReactNode): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (!node) return "";
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (typeof node === "object" && "props" in node) {
    return extractText((node as React.ReactElement).props.children);
  }
  return "";
}

/** Process children to replace file paths with FileChip components. */
function processChildren(children: React.ReactNode): React.ReactNode {
  if (typeof children === "string") {
    return renderWithFileChips(children);
  }
  if (Array.isArray(children)) {
    return children.map((child, i) =>
      typeof child === "string" ? (
        <React.Fragment key={i}>{renderWithFileChips(child)}</React.Fragment>
      ) : (
        child
      )
    );
  }
  return children;
}

export const ChatStream: React.FC<ChatStreamProps> = ({
  events,
  connected,
  agentMood = "neutral",
  onSend,
  onPermissionRespond,
  onUserResponse,
  onSteer,
  onFork,
  onUpload,
  onSuggestionFeedback,
  onOpenMemories,
  onRegenerate,
  messages,
  setMessages,
}) => {
  const [input, setInput] = useState("");
  const [isThinking, setIsThinking] = useState(false);
  // Track active skills that haven't completed yet
  const [activeSkills, setActiveSkills] = useState<
    { skill: string; taskId: string }[]
  >([]);
  const [respondedPermissions, setRespondedPermissions] = useState<Set<string>>(
    new Set()
  );
  const [dismissedSuggestions, setDismissedSuggestions] = useState<Set<string>>(
    new Set()
  );
  const messageEndRef = useRef<HTMLDivElement>(null);
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const processedCountRef = useRef(0);
  // Unique ID per user message — distinguishes intentional repeats from dupes
  const msgIdRef = useRef(0);
  // Steering: active plan detection + steering input
  const [planActive, setPlanActive] = useState(false);
  const [steerInput, setSteerInput] = useState("");
  // File upload
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Reset state when events are cleared (session switch)
  useEffect(() => {
    if (events.length === 0) {
      processedCountRef.current = 0;
      setActiveSkills([]);
      setIsThinking(false);
      setPlanActive(false);
    }
  }, [events.length]);

  // Process new events into the message list
  useEffect(() => {
    if (events.length <= processedCountRef.current) return;

    const newEvents = events.slice(processedCountRef.current);
    processedCountRef.current = events.length;

    setMessages((prev) => {
      let updated = [...prev];

      for (const evt of newEvents) {
        if (evt.type === "thinking") {
          setIsThinking(true);
          continue;
        }

        // Streaming: accumulate chunks into the last message
        if (evt.type === "response_chunk") {
          setIsThinking(false);
          const last = updated[updated.length - 1];
          if (last && "type" in last && last.type === "response_chunk") {
            const accumulated = (last.content || "") + evt.delta;
            updated[updated.length - 1] = {
              ...last,
              delta: accumulated,
              content: accumulated,
            };
          } else {
            updated.push({
              type: "response_chunk" as const,
              delta: evt.delta,
              content: evt.delta,
            });
          }
          continue;
        }

        // When the final response arrives, replace the streaming placeholder
        if (evt.type === "response") {
          const lastIdx = updated.length - 1;
          const lastMsg = updated[lastIdx];
          if (lastIdx >= 0 && lastMsg && "type" in lastMsg && lastMsg.type === "response_chunk") {
            updated[lastIdx] = evt;
            continue;
          }
        }

        if (evt.type === "task_started" && !("sub_task_index" in evt)) {
          setActiveSkills((prev) => [
            ...prev,
            { skill: evt.skill_name, taskId: evt.task_id },
          ]);
        }

        if (
          evt.type === "task_completed" ||
          evt.type === "task_failed"
        ) {
          setActiveSkills((prev) =>
            prev.filter((s) => s.taskId !== evt.task_id)
          );
        }

        if (evt.type === "multi_task_completed") {
          setActiveSkills([]);
          setPlanActive(false);
        }

        // Plan/multi-task started — enable steering
        if (evt.type === "multi_task_started" ||
            (evt.type === "status" && evt.content?.includes("Step "))) {
          setPlanActive(true);
        }

        // Terminal events clear thinking
        if (
          evt.type === "response" ||
          evt.type === "error" ||
          evt.type === "multi_task_completed"
        ) {
          setIsThinking(false);
        }

        if (
          evt.type === "response" ||
          evt.type === "greeting" ||
          evt.type === "error" ||
          evt.type === "task_started" ||
          evt.type === "task_completed" ||
          evt.type === "task_failed" ||
          evt.type === "permission_request" ||
          evt.type === "skill_question" ||
          evt.type === "skill_confirm" ||
          evt.type === "skill_notify" ||
          evt.type === "multi_task_started" ||
          evt.type === "multi_task_completed" ||
          evt.type === "task_skipped" ||
          evt.type === "status" ||
          evt.type === "steering_received" ||
          evt.type === "plan_rewritten" ||
          evt.type === "task_blocked" ||
          evt.type === "steering_ignored" ||
          evt.type === "reminder" ||
          evt.type === "suggestion" ||
          evt.type === "autonomous_action"
        ) {
          updated.push(evt);
        }
      }

      return updated;
    });
  }, [events, setMessages]);

  // Auto-scroll (only if user is near the bottom)
  useEffect(() => {
    const el = scrollAreaRef.current;
    if (!el) { messageEndRef.current?.scrollIntoView({ behavior: "smooth" }); return; }
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distFromBottom < 150) {
      messageEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isThinking, activeSkills]);

  // Track scroll position for scroll-to-bottom button
  useEffect(() => {
    const el = scrollAreaRef.current;
    if (!el) return;
    const onScroll = () => {
      const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
      setShowScrollBtn(dist > 200);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // Auto-resize textarea — grows smoothly, toggles multiline class
  const adjustTextareaHeight = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    // Read the CSS variable for single-line height (send-btn size)
    const singleH = parseInt(getComputedStyle(el).getPropertyValue("--ui-send-size")) || 38;
    el.style.height = singleH + "px";
    const isMulti = el.scrollHeight > singleH;
    if (isMulti) {
      el.style.height = Math.min(el.scrollHeight, 140) + "px";
      el.style.overflowY = el.scrollHeight > 140 ? "auto" : "hidden";
    } else {
      el.style.overflowY = "hidden";
    }
    el.classList.toggle("multiline", isMulti);
  }, []);

  const resetTextarea = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    const singleH = parseInt(getComputedStyle(el).getPropertyValue("--ui-send-size")) || 38;
    el.style.height = singleH + "px";
    el.style.overflowY = "hidden";
    el.classList.remove("multiline");
  }, []);

  const handleSend = async () => {
    const trimmed = input.trim();
    if ((!trimmed && attachedFiles.length === 0) || !connected) return;

    let messageText = trimmed;

    // Upload attached files first
    if (attachedFiles.length > 0) {
      setIsUploading(true);
      try {
        const uploaded: string[] = [];
        for (const f of attachedFiles) {
          const result = await onUpload(f);
          uploaded.push(`${result.filename} (${result.path})`);
        }
        const fileList = uploaded.join(", ");
        messageText = messageText
          ? `[Attached files: ${fileList}]\n\n${messageText}`
          : `[Attached files: ${fileList}]\n\nPlease review the attached files.`;
      } catch {
        // If upload fails, send without attachments
      } finally {
        setIsUploading(false);
        setAttachedFiles([]);
      }
    }

    // Auto-dismiss any visible suggestions — the user chose to type
    // instead of clicking, so the suggestions are stale.
    setDismissedSuggestions((prev) => {
      const next = new Set(prev);
      for (const m of messages) {
        if ("type" in m && m.type === "suggestion" && "suggestion_id" in m) {
          next.add(m.suggestion_id);
        }
      }
      return next;
    });

    const id = ++msgIdRef.current;
    setMessages((prev) => [...prev, { role: "user" as const, content: messageText, _id: id }]);
    onSend(messageText);
    setInput("");
    resetTextarea();
  };

  // Drag-and-drop handlers
  const dragCounter = useRef(0);

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragCounter.current++;
    if (e.dataTransfer.types.includes("Files")) setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragCounter.current--;
    if (dragCounter.current === 0) setIsDragging(false);
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragCounter.current = 0;
    setIsDragging(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length > 0) {
      setAttachedFiles((prev) => [...prev, ...files]);
    }
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handlePermission = (requestId: string, allow: boolean, mode?: ApprovalMode) => {
    if (respondedPermissions.has(requestId)) return;
    setRespondedPermissions((prev) => new Set(prev).add(requestId));
    onPermissionRespond(requestId, allow, mode);
  };

  return (
    <div
      className="chat-inner"
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      {/* Drag overlay */}
      {isDragging && (
        <div className="drag-overlay">
          <div className="drag-overlay-content">
            <IconPaperclip size={32} />
            <div>Drop files to attach</div>
          </div>
        </div>
      )}

      {!connected && (
        <div className="chat-disconnected-bar" role="alert" aria-live="assertive">
          <IconAlertCircle size={14} />
          Disconnected — reconnecting...
        </div>
      )}

      <div className="chat-messages" ref={scrollAreaRef}>
        {showScrollBtn && (
          <button
            className="scroll-to-bottom"
            onClick={() => messageEndRef.current?.scrollIntoView({ behavior: "smooth" })}
            aria-label="Scroll to bottom"
          >
            <IconChevronDown size={18} />
          </button>
        )}
        {messages.length === 0 && !isThinking ? (
          <div className="chat-empty">
            <div className="chat-empty-icon">
              <IconBot size={28} />
            </div>
            {connected ? (
              <div className="typing-indicator" aria-label="Agent is typing">
                <span /><span /><span />
              </div>
            ) : (
              <>
                <div className="chat-empty-title">Connecting...</div>
                <div className="chat-empty-text">Establishing connection to MUSE...</div>
              </>
            )}
          </div>
        ) : (
          <div className="chat-messages-inner">
            {messages.map((msg, i) => {
              // Insert date separators when the day changes between messages
              const createdAt = "_createdAt" in msg ? msg._createdAt : undefined;
              const msgDate = createdAt ? new Date(createdAt) : (i === 0 ? new Date() : undefined);
              let dateSep: React.ReactNode = null;
              if (msgDate) {
                const prevMsg = i > 0 ? messages[i - 1] : undefined;
                const prevCreatedAt = prevMsg && "_createdAt" in prevMsg ? prevMsg._createdAt : undefined;
                const prevDate = prevCreatedAt ? new Date(prevCreatedAt) : undefined;
                const showSep = i === 0 || (prevDate && msgDate.toDateString() !== prevDate.toDateString());
                if (showSep) {
                  const now = new Date();
                  const diffDays = Math.floor((now.getTime() - msgDate.getTime()) / 86400000);
                  let label: string;
                  if (diffDays === 0) label = "Today";
                  else if (diffDays === 1) label = "Yesterday";
                  else if (diffDays < 7) label = `${diffDays} days ago`;
                  else label = msgDate.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
                  dateSep = <div className="date-separator"><span>{label}</span></div>;
                }
              }
              // First message with no date info — show "Today" only for
              // genuinely new sessions (no history loaded).  For resumed
              // sessions the first history message will have _createdAt and
              // supply its own separator with the correct date.
              if (i === 0 && !dateSep && !createdAt && messages.every((m) => !("_createdAt" in m))) {
                dateSep = <div className="date-separator"><span>Today</span></div>;
              }
              // Render helper — wraps any message element with a date separator if needed
              const wrapMsg = (el: React.ReactNode) =>
                dateSep ? <React.Fragment key={i}>{dateSep}{el}</React.Fragment>
                        : <React.Fragment key={i}>{el}</React.Fragment>;

              if ("role" in msg && msg.role === "user") {
                return wrapMsg(
                  <div className="msg-row user">
                    <div className="msg-bubble user">{msg.content}</div>
                  </div>
                );
              }

              const evt = msg as ChatEvent;

              switch (evt.type) {
                case "response_chunk":
                  return wrapMsg(
                    <div className="msg-row agent">
                      <div className={`msg-avatar agent streaming mood-${agentMood}`}>
                        <IconBot size={16} />
                      </div>
                      <div className="msg-bubble agent">
                        <MarkdownContent content={evt.content || evt.delta} />
                      </div>
                    </div>
                  );

                case "response": {
                  const fileInfo = parseFileCard(evt.content);
                  return wrapMsg(
                    <div className="msg-row agent">
                      <div className={`msg-avatar agent mood-${agentMood}`}>
                        <IconBot size={16} />
                      </div>
                      <div className="msg-bubble agent">
                        {fileInfo ? (
                          <FileCard info={fileInfo} />
                        ) : (
                          <MarkdownContent content={evt.content} />
                        )}
                        <div className="msg-footer">
                          {evt.model && <span className="msg-model">{evt.model}</span>}
                          {!fileInfo && <CopyButton text={evt.content} className="msg-copy-btn" label="Copy message" />}
                          {/* Regenerate — only on the last assistant response */}
                          {i === messages.length - 1 && (
                            <button
                              className="msg-regenerate-btn"
                              onClick={onRegenerate}
                              title="Regenerate response"
                              aria-label="Regenerate response"
                            >
                              <IconRefresh size={13} />
                            </button>
                          )}
                          {msg._dbId != null && (
                            <button
                              className="msg-fork-btn"
                              onClick={() => onFork(msg._dbId!)}
                              title="Fork from this message"
                              aria-label="Fork conversation from this message"
                            >
                              <IconGitBranch size={13} />
                            </button>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                }

                case "greeting": {
                  const gs = evt.suggestions || [];
                  const gr = evt.reminders || [];
                  const gst = evt.stats || { sessions: 0, memories: 0, days_together: 0 };
                  const hasExtras = gs.length > 0 || gr.length > 0 || gst.sessions > 0;
                  return wrapMsg(
                    <div className="greeting-card">
                      <div className="greeting-card-header">
                        <div className="msg-avatar agent">
                          <IconBot size={16} />
                        </div>
                        <div className="greeting-card-text">
                          <MarkdownContent content={evt.content} />
                        </div>
                      </div>
                      {gs.length > 0 && (
                        <div className="greeting-card-chips">
                          {gs.map((s) => (
                            <button
                              key={s.id}
                              className="greeting-chip"
                              onClick={() => {
                                onSuggestionFeedback(s.id, true);
                                onSend(s.content);
                              }}
                            >
                              {s.content}
                            </button>
                          ))}
                        </div>
                      )}
                      {gr.length > 0 && (
                        <div className="greeting-card-reminders">
                          {gr.map((r, ri) => (
                            <div key={ri} className="greeting-reminder-item">
                              <IconClock size={12} />
                              <span className="greeting-reminder-what">{r.what}</span>
                              {r.when && <span className="greeting-reminder-when">{r.when}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                      {hasExtras && gst.sessions > 0 && (
                        <div className="greeting-card-stats">
                          {gst.sessions} {gst.sessions === 1 ? "session" : "sessions"}
                          {gst.memories > 0 && <> · <span className="greeting-stats-memories" onClick={onOpenMemories} role="button" tabIndex={0}>{gst.memories} {gst.memories === 1 ? "memory" : "memories"}</span></>}
                          {" · "}{gst.relationship_label || (gst.days_together <= 1 && gst.sessions <= 1 ? "Just getting started" : `${gst.days_together} days`)}
                        </div>
                      )}
                    </div>
                  );
                }

                case "task_started":
                  // Suppress individual start notifications when part of a
                  // multi-task — the multi_task_started bubble already shows
                  // the count, avoiding the "2X tasks" impression.
                  if ("sub_task_index" in evt) return null;
                  return wrapMsg(
                    <div className="task-notification started">
                      <strong>{evt.skill_name}</strong>&nbsp;started — {evt.message}
                    </div>
                  );

                case "task_completed":
                  if (!evt.summary) return null;
                  return wrapMsg(
                    <div className="task-notification completed">
                      Task completed — {evt.summary}
                    </div>
                  );

                case "task_failed":
                  return wrapMsg(
                    <div className="task-notification failed">
                      Task failed — {evt.error}
                    </div>
                  );

                case "multi_task_started":
                  return wrapMsg(
                    <div className="task-notification started">
                      Running {evt.sub_task_count} tasks...
                    </div>
                  );

                case "multi_task_completed": {
                  const parts: string[] = [];
                  if (evt.succeeded > 0) parts.push(`${evt.succeeded} succeeded`);
                  if (evt.failed > 0) parts.push(`${evt.failed} failed`);
                  if (evt.skipped > 0) parts.push(`${evt.skipped} skipped`);
                  return wrapMsg(
                    <div
                      className={`task-notification ${evt.failed > 0 ? "failed" : "completed"}`}
                    >
                      All tasks finished — {parts.join(", ")}
                    </div>
                  );
                }

                case "task_skipped":
                  return wrapMsg(
                    <div className="task-notification" style={{ opacity: 0.6 }}>
                      Skipped {evt.skill_id} — {evt.reason}
                    </div>
                  );

                case "permission_request": {
                  const responded = respondedPermissions.has(evt.request_id);
                  if (responded) {
                    return wrapMsg(
                      <div className="task-notification completed">
                        <IconShield size={13} />
                        Permission granted — {evt.skill_id}: {evt.permission}
                      </div>
                    );
                  }
                  return wrapMsg(
                    <div className={`permission-card ${evt.is_first_party ? "first-party" : ""}`}>
                      <div className="permission-card-title">
                        <IconShield size={14} />
                        Permission Request
                        {evt.is_first_party && (
                          <span className="permission-recommended-badge">Recommended</span>
                        )}
                      </div>
                      <div className="permission-card-text">
                        {evt.display_text}
                      </div>
                      <div className="permission-card-meta">
                        Skill: {evt.skill_id}
                        {evt.is_first_party && " (built-in)"}
                        {" "}&middot; Risk: {evt.risk_tier} &middot; {evt.permission}
                      </div>
                      <PermissionActions
                        suggestedMode={evt.suggested_mode ?? "once"}
                        onAllow={(mode) =>
                          handlePermission(evt.request_id, true, mode)
                        }
                        onDeny={() =>
                          handlePermission(evt.request_id, false)
                        }
                      />
                    </div>
                  );
                }

                case "skill_question": {
                  const answered = respondedPermissions.has(evt.request_id);
                  if (answered) {
                    return wrapMsg(
                      <div className="task-notification completed">
                        <IconCheck size={13} />
                        Answered — {evt.skill_id}
                      </div>
                    );
                  }
                  return wrapMsg(
                    <div className="skill-card">
                      <div className="skill-card-title">
                        {evt.skill_id} asks:
                      </div>
                      <div className="skill-card-text">{evt.question}</div>
                      {evt.options ? (
                        <div className="skill-options-row">
                          {evt.options.map((opt) => (
                            <button
                              key={opt}
                              className="btn btn-option btn-sm"
                              onClick={() => {
                                setRespondedPermissions((p) =>
                                  new Set(p).add(evt.request_id)
                                );
                                onUserResponse(evt.request_id, opt);
                              }}
                            >
                              {opt}
                            </button>
                          ))}
                        </div>
                      ) : (
                        <SkillTextInput
                          onSubmit={(text) => {
                            setRespondedPermissions((p) =>
                              new Set(p).add(evt.request_id)
                            );
                            onUserResponse(evt.request_id, text);
                          }}
                        />
                      )}
                    </div>
                  );
                }

                case "skill_confirm": {
                  const confirmed = respondedPermissions.has(evt.request_id);
                  if (confirmed) {
                    return wrapMsg(
                      <div className="task-notification completed">
                        <IconCheck size={13} />
                        Confirmed — {evt.skill_id}
                      </div>
                    );
                  }
                  return wrapMsg(
                    <div className="permission-card">
                      <div className="permission-card-title">
                        <IconShield size={14} />
                        {evt.skill_id} wants to confirm:
                      </div>
                      <div className="permission-card-text">{evt.message}</div>
                      <div className="permission-card-actions">
                        <button
                          className="btn btn-success btn-sm"
                          onClick={() => {
                            setRespondedPermissions((p) =>
                              new Set(p).add(evt.request_id)
                            );
                            onUserResponse(evt.request_id, true);
                          }}
                        >
                          Yes
                        </button>
                        <button
                          className="btn btn-danger btn-sm"
                          onClick={() => {
                            setRespondedPermissions((p) =>
                              new Set(p).add(evt.request_id)
                            );
                            onUserResponse(evt.request_id, false);
                          }}
                        >
                          No
                        </button>
                      </div>
                    </div>
                  );
                }

                case "skill_notify":
                  return wrapMsg(
                    <div className="task-notification started">
                      {evt.message}
                    </div>
                  );

                case "status":
                  return wrapMsg(
                    <div className="task-notification started">
                      {evt.content}
                    </div>
                  );

                case "steering_received":
                  return wrapMsg(
                    <div className="task-notification started steering">
                      <IconNavigation size={13} />
                      &nbsp;Steering applied: {evt.content}
                    </div>
                  );

                case "plan_rewritten":
                  return wrapMsg(
                    <div className="task-notification started steering">
                      <IconNavigation size={13} />
                      &nbsp;Plan revised
                    </div>
                  );

                case "task_blocked":
                  return wrapMsg(
                    <div className="task-notification failed">
                      <IconShield size={13} />
                      &nbsp;Blocked: {evt.reason}
                    </div>
                  );

                case "steering_ignored":
                  return wrapMsg(
                    <div className="task-notification" style={{ opacity: 0.5 }}>
                      <IconNavigation size={13} />
                      &nbsp;{evt.content}
                    </div>
                  );

                case "reminder":
                  return wrapMsg(
                    <div className="reminder-bubble" role="alert">
                      <IconClock size={15} />
                      <div>
                        <strong>{evt.what}</strong>
                        {evt.when && (
                          <span className="reminder-time"> — {new Date(evt.when).toLocaleTimeString()}</span>
                        )}
                      </div>
                    </div>
                  );

                case "suggestion": {
                  const sugId = evt.suggestion_id;
                  if (dismissedSuggestions.has(sugId)) return null;
                  return wrapMsg(
                    <div className="suggestion-card">
                      <div className="suggestion-card-text">
                        {evt.content}
                      </div>
                      <div className="suggestion-card-actions">
                        <button
                          className="btn btn-sm btn-primary"
                          onClick={() => {
                            onSuggestionFeedback(sugId, true);
                            setDismissedSuggestions((s) => new Set(s).add(sugId));
                            onSend(evt.content);
                          }}
                        >
                          Do it
                        </button>
                        <button
                          className="btn btn-sm btn-ghost"
                          onClick={() => {
                            onSuggestionFeedback(sugId, false);
                            setDismissedSuggestions((s) => new Set(s).add(sugId));
                          }}
                        >
                          Dismiss
                        </button>
                      </div>
                    </div>
                  );
                }

                case "autonomous_action":
                  return (
                    <div key={i} className="autonomous-card">
                      <div className="autonomous-card-header">
                        <IconBot size={14} />
                        Background action: <strong>{evt.skill_id}</strong>
                      </div>
                      <div className="autonomous-card-reason">{evt.reason}</div>
                      <div className="autonomous-card-result">{evt.result?.slice(0, 300)}</div>
                    </div>
                  );

                case "error":
                  return wrapMsg(
                    <div className="error-bubble" role="alert">
                      <IconAlertCircle size={15} />
                      {evt.content}
                    </div>
                  );

                default:
                  return null;
              }
            })}

            {/* Activity indicator */}
            {(isThinking || activeSkills.length > 0) && (
              <div className="activity-row">
                <div className="msg-avatar agent">
                  <IconBot size={16} />
                </div>
                <div className="activity-bubble">
                  <div className="activity-label">
                    {activeSkills.length > 0
                      ? activeSkills.length === 1
                        ? `Using ${activeSkills[0].skill}`
                        : `Running ${activeSkills.length} tasks`
                      : "Thinking"}
                  </div>
                  <div className="activity-dots">
                    <div className="thinking-dot" />
                    <div className="thinking-dot" />
                    <div className="thinking-dot" />
                  </div>
                </div>
              </div>
            )}

            {/* Steering bar — visible during plan/multi-task execution */}
            {planActive && (isThinking || activeSkills.length > 0) && (
              <div className="steering-bar">
                <IconNavigation size={14} />
                <input
                  className="steering-input"
                  placeholder="Redirect the plan..."
                  value={steerInput}
                  onChange={(e) => setSteerInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && steerInput.trim()) {
                      onSteer(steerInput.trim());
                      setSteerInput("");
                    }
                  }}
                />
                <button
                  className="btn btn-sm steering-send"
                  disabled={!steerInput.trim()}
                  onClick={() => {
                    if (steerInput.trim()) {
                      onSteer(steerInput.trim());
                      setSteerInput("");
                    }
                  }}
                >
                  Steer
                </button>
              </div>
            )}

            <div ref={messageEndRef} />
          </div>
        )}
      </div>

      <div className="input-area">
        {/* Attached file chips */}
        {attachedFiles.length > 0 && (
          <div className="attached-files">
            {attachedFiles.map((f, i) => (
              <div key={i} className="attached-chip">
                <IconFileText size={12} />
                <span className="attached-chip-name">{f.name}</span>
                <button
                  className="attached-chip-remove"
                  onClick={() => setAttachedFiles((prev) => prev.filter((_, j) => j !== i))}
                  aria-label={`Remove ${f.name}`}
                >
                  <IconX size={10} />
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="input-area-inner">
          <input
            type="file"
            ref={fileInputRef}
            multiple
            style={{ display: "none" }}
            onChange={(e) => {
              const files = Array.from(e.target.files || []);
              if (files.length > 0) setAttachedFiles((prev) => [...prev, ...files]);
              e.target.value = "";  // reset so same file can be re-selected
            }}
          />
          <button
            className="attach-btn"
            onClick={() => fileInputRef.current?.click()}
            disabled={!connected}
            title="Attach files"
            aria-label="Attach files"
          >
            <IconPaperclip size={16} />
          </button>
          <textarea
            ref={textareaRef}
            className="input-textarea"
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              requestAnimationFrame(adjustTextareaHeight);
            }}
            onKeyDown={handleKeyDown}
            placeholder={connected ? "Message MUSE..." : "Connecting..."}
            disabled={!connected}
          />
          <button
            className="send-btn"
            onClick={handleSend}
            disabled={!connected || (!input.trim() && attachedFiles.length === 0) || isUploading}
            title="Send message"
            aria-label="Send message"
          >
            <IconSend size={16} />
          </button>
        </div>
        <div className="input-hint">Enter to send, Shift+Enter for new line</div>
      </div>
    </div>
  );
};

/** Mode-selector + allow/deny for permission requests. */
const APPROVAL_MODES: { value: ApprovalMode; label: string; hint: string }[] = [
  { value: "once", label: "Once", hint: "Allow this one time only" },
  { value: "session", label: "This Session", hint: "Until the session ends" },
  { value: "always", label: "Always", hint: "Permanent grant" },
];

const PermissionActions: React.FC<{
  suggestedMode: ApprovalMode;
  onAllow: (mode: ApprovalMode) => void;
  onDeny: () => void;
}> = ({ suggestedMode, onAllow, onDeny }) => {
  const [selectedMode, setSelectedMode] = useState<ApprovalMode>(suggestedMode);
  return (
    <div className="permission-card-actions-v2">
      <div className="perm-mode-selector">
        {APPROVAL_MODES.map((m) => (
          <button
            key={m.value}
            className={`perm-mode-btn ${selectedMode === m.value ? "active" : ""}`}
            onClick={() => setSelectedMode(m.value)}
            title={m.hint}
          >
            {m.label}
          </button>
        ))}
      </div>
      <div className="perm-action-btns">
        <button
          className="btn btn-success btn-sm"
          onClick={() => onAllow(selectedMode)}
        >
          Allow {selectedMode === "once" ? "Once" : selectedMode === "session" ? "for Session" : "Always"}
        </button>
        <button className="btn btn-danger btn-sm" onClick={onDeny}>
          Deny
        </button>
      </div>
    </div>
  );
};

/** Small inline text input for answering skill questions. */
const SkillTextInput: React.FC<{ onSubmit: (text: string) => void }> = ({
  onSubmit,
}) => {
  const [value, setValue] = useState("");
  return (
    <div className="skill-text-input">
      <input
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && value.trim()) {
            onSubmit(value.trim());
          }
        }}
        placeholder="Type your answer..."
        autoFocus
      />
      <button
        className="btn btn-primary btn-sm"
        onClick={() => value.trim() && onSubmit(value.trim())}
      >
        Submit
      </button>
    </div>
  );
};
