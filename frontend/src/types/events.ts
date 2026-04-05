export type ChatEvent =
  | { type: "thinking"; content: string }
  | { type: "response_chunk"; delta: string; content?: string }
  | {
      type: "response";
      content: string;
      tokens_in: number;
      tokens_out: number;
      model: string;
    }
  | {
      type: "task_started";
      task_id: string;
      skill: string;
      skill_name: string;
      message: string;
    }
  | {
      type: "task_completed";
      task_id: string;
      result: unknown;
      summary: string;
      tokens_in: number;
      tokens_out: number;
    }
  | { type: "task_failed"; task_id: string; error: string }
  | { type: "task_killed"; task_id: string }
  | {
      type: "multi_task_started";
      sub_task_count: number;
      message: string;
    }
  | {
      type: "multi_task_completed";
      succeeded: number;
      failed: number;
      skipped: number;
    }
  | {
      type: "task_skipped";
      sub_task_index: number;
      skill_id: string;
      reason: string;
    }
  | {
      type: "permission_request";
      request_id: string;
      skill_id: string;
      permission: string;
      risk_tier: string;
      display_text: string;
      suggested_mode: ApprovalMode;
      is_first_party?: boolean;
    }
  | { type: "error"; content: string }
  | { type: "status"; content: string }
  | { type: "steering_received"; content: string }
  | { type: "plan_rewritten"; content: string; steps: unknown[] }
  | { type: "task_blocked"; task_id: string; skill_id: string; reason: string }
  | { type: "steering_ignored"; content: string }
  | { type: "reminder"; content: string; what: string; when: string; key: string }
  | { type: "suggestion"; content: string; suggestion_id: string; skill_id?: string; suggestion_type?: string }
  | { type: "autonomous_action"; skill_id: string; reason: string; result: string }
  | { type: "greeting_placeholder"; content: string }
  | {
      type: "greeting";
      content: string;
      suggestions: Array<{ id: string; content: string; skill_id: string }>;
      reminders: Array<{ what: string; when: string }>;
      stats: { sessions: number; memories: number; days_together: number; relationship_level: number; relationship_label: string };
      tokens_in: number;
      tokens_out: number;
      model: string;
    }
  | {
      type: "skill_question";
      task_id: string;
      skill_id: string;
      question: string;
      options: string[] | null;
      request_id: string;
    }
  | {
      type: "skill_confirm";
      task_id: string;
      skill_id: string;
      message: string;
      request_id: string;
    }
  | { type: "mood_changed"; mood: string }
  | {
      type: "screen_status";
      mode: "off" | "passive" | "active";
      is_streaming: boolean;
      vision_model: string | null;
      fps: number;
    }
  | {
      type: "screen_action_preview";
      action: Record<string, unknown>;
      needs_confirmation: boolean;
    }
  | {
      type: "screen_action_executed";
      action_type: string;
      success: boolean;
      details: string;
    }
  | { type: "screen_error"; content: string }
  | { type: "session_working"; session_id: string }
  | { type: "session_idle"; session_id: string }
  | { type: "permission_approved"; request_id: string }
  | { type: "permission_denied"; request_id: string }
  | { type: "skill_notify"; task_id: string; skill_id: string; message: string }
  | { type: "session_started"; session_id: string; branch_head_id?: number | null }
  | {
      type: "session_updated";
      session_id: string;
      title: string;
    }
  | {
      type: "history";
      session_id: string;
      messages: HistoryMessage[];
    };

export interface TaskInfo {
  id: string;
  skill_id: string;
  status: string;
  tokens_in: number;
  tokens_out: number;
  created_at: string;
  checkpoints: unknown[];
}

export interface UserMessage {
  role: "user";
  content: string;
  _id?: number;
  _createdAt?: string;
}

/** Runtime properties added by the frontend to chat events. */
interface DisplayMeta {
  _createdAt?: string;
  _dbId?: number;
}

export type DisplayMessage = (ChatEvent & DisplayMeta) | (UserMessage & DisplayMeta);

export interface SessionUsage {
  tokens_in: number;
  tokens_out: number;
}

export type ApprovalMode = "always" | "session" | "once";

export interface SkillPermission {
  skill_id: string;
  skill_name: string;
  permission: string;
  approval_mode: ApprovalMode;
  granted_at: string;
}

export interface AuditEntry {
  timestamp: string;
  skill_id: string;
  action: string;
  permission: string;
  result: string;
}

export interface HistoryMessage {
  id: number;
  role: string;
  content: string;
  event_type: string;
  created_at: string;
  metadata?: Record<string, unknown>;
  parent_id?: number | null;
}

export interface Session {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  branch_head_id?: number | null;
}
