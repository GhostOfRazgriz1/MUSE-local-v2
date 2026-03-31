/**
 * Frontend event compaction — structural collapse of resolved event
 * sequences so the display buffer stays bounded without losing
 * interactive prompts the user hasn't acted on.
 */

import type { ChatEvent } from "../types/events";

/** Event types that represent pending user interactions. */
const INTERACTIVE_TYPES = new Set([
  "permission_request",
  "skill_question",
  "skill_confirm",
]);

/** Transient event types safe to drop once outside the recent window. */
const TRANSIENT_TYPES = new Set([
  "thinking",
  "status",
  "steering_received",
  "plan_rewritten",
  "steering_ignored",
]);

/**
 * Structurally compact an event array while preserving:
 * - All events within the recent `windowSize` tail
 * - Any event whose `request_id` is in `pendingRequestIds`
 * - All `response` and `error` events (user-visible content)
 *
 * Compaction rules applied outside the window:
 * 1. Drop transient events (thinking, status, steering, etc.)
 * 2. Collapse resolved task lifecycles (started→completed) into
 *    a single synthetic `task_completed` event
 * 3. Drop `permission_approved`/`permission_denied` (resolution noise)
 */
export function structuralCompactEvents(
  events: ChatEvent[],
  pendingRequestIds: Set<string>,
  windowSize: number = 30,
): ChatEvent[] {
  if (events.length <= windowSize) return events;

  const boundary = events.length - windowSize;
  const window = events.slice(boundary); // always kept verbatim

  // Identify completed tasks so we can collapse their lifecycle
  const completedTasks = new Set<string>();
  for (let i = 0; i < boundary; i++) {
    const evt = events[i];
    if (
      evt.type === "task_completed" ||
      evt.type === "task_failed" ||
      evt.type === "task_killed"
    ) {
      completedTasks.add(evt.task_id);
    }
  }

  const compacted: ChatEvent[] = [];

  for (let i = 0; i < boundary; i++) {
    const evt = events[i];

    // Never drop pending interactive events
    if (
      INTERACTIVE_TYPES.has(evt.type) &&
      "request_id" in evt &&
      pendingRequestIds.has(evt.request_id)
    ) {
      compacted.push(evt);
      continue;
    }

    // Drop transients
    if (TRANSIENT_TYPES.has(evt.type)) continue;

    // Drop resolution-only events
    if (evt.type === "permission_approved" || evt.type === "permission_denied") continue;

    // Collapse task_started if the task is already resolved
    if (evt.type === "task_started" && completedTasks.has(evt.task_id)) continue;

    // Keep everything else (responses, errors, task_completed summaries, etc.)
    compacted.push(evt);
  }

  return [...compacted, ...window];
}
