"""Debug tracer — structured JSON logging for MUSE.

When debug mode is enabled, every significant event is written as a
JSON line to a timestamped log file under the ``logs/`` directory.

Usage:
    tracer = DebugTracer(enabled=True, logs_dir=Path("logs"))
    tracer.event("classify", skill_id="search", score=0.82)

Each log line is a self-contained JSON object with:
    ts          — ISO-8601 timestamp
    elapsed_ms  — milliseconds since tracer was created
    category    — event category (classify, execute, bridge, ws, …)
    event       — short event name within the category
    data        — arbitrary payload dict
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum size of a single debug log file before rotation (10 MB).
_MAX_LOG_SIZE = 10 * 1024 * 1024
# Maximum number of debug log files to retain (older ones are deleted).
_MAX_LOG_FILES = 10


class DebugTracer:
    """Structured event tracer that writes JSON lines to a log file.

    When ``enabled=False`` every method is a no-op (zero overhead).

    Log rotation: files are rotated at ``_MAX_LOG_SIZE`` bytes and the
    oldest files beyond ``_MAX_LOG_FILES`` are automatically cleaned up.
    """

    def __init__(self, enabled: bool = False, logs_dir: Path | None = None):
        self.enabled = enabled
        self._file = None
        self._start = time.monotonic()
        self._path: Path | None = None
        self._logs_dir = logs_dir
        self._bytes_written = 0

        if enabled and logs_dir:
            logs_dir.mkdir(parents=True, exist_ok=True)
            self._cleanup_old_logs(logs_dir)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._path = logs_dir / f"debug_{stamp}.jsonl"
            self._file = open(self._path, "a", encoding="utf-8")
            logger.info("Debug tracer logging to %s", self._path)
            self.event("tracer", "started", path=str(self._path))

    @staticmethod
    def _cleanup_old_logs(logs_dir: Path) -> None:
        """Remove old debug log files beyond the retention limit."""
        try:
            log_files = sorted(
                logs_dir.glob("debug_*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old_file in log_files[_MAX_LOG_FILES - 1:]:
                old_file.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("Log cleanup failed: %s", e)

    def _rotate_if_needed(self) -> None:
        """Rotate to a new log file if the current one exceeds the size limit."""
        if self._bytes_written < _MAX_LOG_SIZE or not self._logs_dir:
            return
        try:
            if self._file:
                self._file.close()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._path = self._logs_dir / f"debug_{stamp}.jsonl"
            self._file = open(self._path, "a", encoding="utf-8")
            self._bytes_written = 0
            self._cleanup_old_logs(self._logs_dir)
        except Exception as e:
            logger.debug("Log rotation failed: %s", e)

    # ── Core ────────────────────────────────────────────────────

    def event(self, category: str, event: str, **data: Any) -> None:
        """Write a single event line."""
        if not self.enabled or not self._file:
            return
        line = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((time.monotonic() - self._start) * 1000, 1),
            "category": category,
            "event": event,
            "data": _sanitize(data),
        }
        try:
            encoded = json.dumps(line, default=str) + "\n"
            self._file.write(encoded)
            self._file.flush()
            self._bytes_written += len(encoded)
            self._rotate_if_needed()
        except Exception as e:
            import sys
            print(f"[DebugTracer] write failed: {e}", file=sys.stderr)

    def close(self) -> None:
        if self._file:
            self.event("tracer", "stopped")
            self._file.close()
            self._file = None

    # ── Convenience methods for common events ───────────────────

    # -- WebSocket --

    def ws_connect(self, session_id: str) -> None:
        self.event("ws", "connect", session_id=session_id)

    def ws_disconnect(self, session_id: str) -> None:
        self.event("ws", "disconnect", session_id=session_id)

    def ws_receive(self, msg_type: str, data: dict) -> None:
        self.event("ws", "receive", msg_type=msg_type,
                   content=_truncate(data.get("content", "")),
                   request_id=data.get("request_id"))

    def ws_send(self, event_dict: dict) -> None:
        self.event("ws", "send", event_type=event_dict.get("type"),
                   content=_truncate(event_dict.get("content", "")),
                   task_id=event_dict.get("task_id"),
                   skill_id=event_dict.get("skill_id", event_dict.get("skill")))

    # -- Intent classification --

    def classify_start(self, message: str) -> None:
        self.event("classify", "start", message=_truncate(message, 300))

    def classify_result(self, intent) -> None:
        self.event("classify", "result",
                   mode=intent.mode.value,
                   skill_id=intent.skill_id,
                   skill_ids=intent.skill_ids,
                   sub_tasks=[
                       {"skill_id": st.skill_id,
                        "instruction": _truncate(st.instruction, 200),
                        "depends_on": st.depends_on}
                       for st in intent.sub_tasks
                   ] if intent.sub_tasks else [],
                   confidence=round(intent.confidence, 3))

    # -- Orchestrator --

    def handle_message(self, user_message: str, session_id: str | None) -> None:
        self.event("orchestrator", "handle_message",
                   message=_truncate(user_message, 300),
                   session_id=session_id)

    def route_decision(self, mode: str, skill_id: str | None = None, count: int = 1) -> None:
        self.event("orchestrator", "route", mode=mode, skill_id=skill_id, task_count=count)

    def permission_check(self, skill_id: str, missing: list[str], granted: list[str]) -> None:
        self.event("orchestrator", "permission_check",
                   skill_id=skill_id, missing=missing, granted=granted)

    def task_spawn(self, task_id: str, skill_id: str, parent_task_id: str | None = None) -> None:
        self.event("orchestrator", "task_spawn",
                   task_id=task_id, skill_id=skill_id, parent_task_id=parent_task_id)

    def task_complete(self, task_id: str, skill_id: str, status: str,
                      summary: str = "", error: str = "",
                      tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.event("orchestrator", "task_complete",
                   task_id=task_id, skill_id=skill_id, status=status,
                   summary=_truncate(summary, 500), error=error,
                   tokens_in=tokens_in, tokens_out=tokens_out)

    def multi_task_wave(self, wave_idx: int, task_indices: list[int], skill_ids: list[str]) -> None:
        self.event("orchestrator", "multi_task_wave",
                   wave=wave_idx, tasks=task_indices, skills=skill_ids)

    def pipeline_context(self, task_idx: int, skill_id: str, context_keys: list[str]) -> None:
        self.event("orchestrator", "pipeline_context",
                   task_idx=task_idx, skill_id=skill_id, keys=context_keys)

    # -- Sandbox / skill execution --

    def skill_load(self, skill_id: str, module_path: str) -> None:
        self.event("sandbox", "skill_load", skill_id=skill_id, path=module_path)

    def skill_start(self, task_id: str, skill_id: str, tier: str) -> None:
        self.event("sandbox", "skill_start",
                   task_id=task_id, skill_id=skill_id, tier=tier)

    def skill_finish(self, task_id: str, skill_id: str, status: str, error: str = "") -> None:
        self.event("sandbox", "skill_finish",
                   task_id=task_id, skill_id=skill_id, status=status, error=error)

    # -- LocalBridge IPC --

    def bridge_send(self, task_id: str, msg_type: str, **extra: Any) -> None:
        self.event("bridge", "send", task_id=task_id, msg_type=msg_type, **extra)

    def bridge_receive(self, task_id: str, msg_type: str, **extra: Any) -> None:
        self.event("bridge", "receive", task_id=task_id, msg_type=msg_type, **extra)

    # -- LLM calls --

    def llm_call(self, purpose: str, model: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.event("llm", "call", purpose=purpose, model=model,
                   tokens_in=tokens_in, tokens_out=tokens_out)

    # -- Conversation summary --

    def conversation_summary(self, turns: int, raw_len: int, summary_len: int) -> None:
        self.event("orchestrator", "conversation_summary",
                   turns=turns, raw_chars=raw_len, summary_chars=summary_len)

    # -- Errors --

    def error(self, category: str, message: str, **extra: Any) -> None:
        self.event(category, "error", message=message, **extra)


# ── Helpers ─────────────────────────────────────────────────────

def _truncate(text: Any, limit: int = 200) -> str:
    s = str(text) if text else ""
    return s[:limit] + "…" if len(s) > limit else s


def _sanitize(data: dict) -> dict:
    """Remove None values and ensure JSON-serializable."""
    return {k: v for k, v in data.items() if v is not None}


# ── Global singleton (set during startup) ───────────────────────

_tracer = DebugTracer(enabled=False)


def get_tracer() -> DebugTracer:
    return _tracer


def set_tracer(tracer: DebugTracer) -> None:
    global _tracer
    _tracer = tracer
