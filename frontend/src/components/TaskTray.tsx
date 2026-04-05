import React, { useState, useEffect, useRef } from "react";
import { IconX, IconClock } from "./Icons";
import { apiFetch } from "../hooks/useApiToken";
import type { ChatEvent } from "../types/events";

interface TaskTrayProps {
  events: ChatEvent[];
}

interface TrackedTask {
  id: string;
  skill: string;
  skill_name: string;
  status: "running" | "completed" | "failed";
  message: string;
  summary?: string;
  error?: string;
  startedAt: number;
  elapsed: string;
}

function formatElapsed(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${minutes}m ${secs}s`;
}

export const TaskTray: React.FC<TaskTrayProps> = ({ events }) => {
  const [tasks, setTasks] = useState<Map<string, TrackedTask>>(new Map());
  const [expandedTask, setExpandedTask] = useState<string | null>(null);
  const processedCountRef = useRef(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Reset when events are cleared (session switch)
  useEffect(() => {
    if (events.length === 0) {
      processedCountRef.current = 0;
      setTasks(new Map());
    }
  }, [events.length]);

  useEffect(() => {
    if (events.length <= processedCountRef.current) return;
    const newEvents = events.slice(processedCountRef.current);
    processedCountRef.current = events.length;

    setTasks((prev) => {
      const next = new Map(prev);
      for (const evt of newEvents) {
        if (evt.type === "task_started") {
          next.set(evt.task_id, {
            id: evt.task_id, skill: evt.skill, skill_name: evt.skill_name,
            status: "running", message: evt.message,
            startedAt: Date.now(), elapsed: "0s",
          });
        } else if (evt.type === "task_completed") {
          const existing = next.get(evt.task_id);
          if (existing) {
            next.set(evt.task_id, {
              ...existing, status: "completed", summary: evt.summary,
            });
          }
        } else if (evt.type === "task_failed") {
          const existing = next.get(evt.task_id);
          if (existing) next.set(evt.task_id, { ...existing, status: "failed", error: evt.error });
        } else if (evt.type === "task_killed") {
          const existing = next.get(evt.task_id);
          if (existing) next.set(evt.task_id, { ...existing, status: "failed", error: "Cancelled" });
        }
      }
      return next;
    });
  }, [events]);

  useEffect(() => {
    intervalRef.current = setInterval(() => {
      setTasks((prev) => {
        let changed = false;
        const next = new Map(prev);
        for (const [id, task] of next) {
          if (task.status === "running") {
            const newElapsed = formatElapsed(Date.now() - task.startedAt);
            if (newElapsed !== task.elapsed) {
              next.set(id, { ...task, elapsed: newElapsed });
              changed = true;
            }
          }
        }
        return changed ? next : prev;
      });
    }, 1000);
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, []);

  const handleKill = async (taskId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try { await apiFetch(`/api/tasks/${taskId}/kill`, { method: "POST" }); } catch {}
  };

  const taskList = Array.from(tasks.values()).sort((a, b) => {
    if (a.status === "running" && b.status !== "running") return -1;
    if (b.status === "running" && a.status !== "running") return 1;
    return b.startedAt - a.startedAt;
  });

  return (
    <div className="task-popover-inner">
      <div className="task-popover-header">
        <span className="task-popover-title">Tasks</span>
        <span className="task-popover-count">{taskList.length}</span>
      </div>

      <div className="task-popover-list">
        {taskList.length === 0 ? (
          <div className="task-tray-empty">
            <div className="task-tray-empty-icon"><IconClock size={16} /></div>
            No active tasks
          </div>
        ) : (
          taskList.map((task) => (
            <div
              key={task.id}
              className="task-card"
              onClick={() => setExpandedTask(expandedTask === task.id ? null : task.id)}
            >
              <div className="task-card-header">
                <span className="task-card-name">{task.skill_name}</span>
                <div className="task-card-status">
                  <span className={`status-badge ${task.status}`}>{task.status}</span>
                  {task.status === "running" && (
                    <button
                      className="task-card-kill"
                      onClick={(e) => handleKill(task.id, e)}
                      title="Kill task"
                      aria-label="Kill task"
                    >
                      <IconX size={14} />
                    </button>
                  )}
                </div>
              </div>
              <div className="task-card-meta">
                <span className="task-meta-item"><IconClock size={11} />{task.elapsed}</span>
              </div>
              {expandedTask === task.id && (
                <div className="task-card-details">
                  <div className="task-detail-row"><span className="task-detail-label">Message</span> {task.message}</div>
                  {task.summary && <div className="task-detail-row"><span className="task-detail-label">Summary</span> {task.summary}</div>}
                  {task.error && <div className="task-detail-row task-detail-error"><span className="task-detail-label">Error</span> {task.error}</div>}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
};
