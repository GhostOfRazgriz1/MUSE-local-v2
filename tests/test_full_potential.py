"""MUSE full-potential tests — exercises multi-task orchestration,
pipeline context, intermediate suppression, and edge cases.

Run with: python tests/test_full_potential.py
Requires the server to be running (preferably with --debug).

After the run, the script auto-inspects the debug log for the session
and prints a diagnostic report alongside the test results.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from websockets.asyncio.client import connect as ws_connect

TIMEOUT = 180
LOGS_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "muse" / "logs"
TOKEN_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / "muse" / ".api_token"
FILES_OUTPUT_DIR = Path.home() / "Documents" / "AgentOS"


def _ws_url() -> str:
    token = ""
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    base = "ws://localhost:8080/api/ws/chat"
    return f"{base}?token={token}" if token else base


def _safe(text: str, limit: int = 120) -> str:
    """Truncate and strip non-ASCII for safe console printing."""
    return text[:limit].replace("\n", " ").encode("ascii", errors="replace").decode("ascii")


# ── Helpers ─────────────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    passed: bool
    events: list[dict] = field(default_factory=list)
    error: str = ""
    duration: float = 0.0


def _responses(events: list[dict]) -> list[str]:
    return [e.get("content", "") for e in events if e.get("type") == "response"]


def _types(events: list[dict]) -> list[str]:
    return [e.get("type") for e in events]


def _latest_log() -> Path | None:
    candidates = sorted(LOGS_DIR.glob("debug_*.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def _parse_log(path: Path, after_epoch: float = 0.0) -> list[dict]:
    """Parse JSONL lines, filtering entries after a UTC epoch timestamp."""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts_str = entry.get("ts", "")
                if ts_str and after_epoch:
                    dt = datetime.fromisoformat(ts_str)
                    if dt.timestamp() < after_epoch:
                        continue
                entries.append(entry)
            except (json.JSONDecodeError, ValueError):
                continue
    return entries


# ── Test harness ────────────────────────────────────────────────

class AgentTester:
    """WebSocket client that simulates a real user."""

    def __init__(self):
        self.ws = None
        self.session_id: str | None = None
        self.results: list[TestResult] = []

    async def _bootstrap(self):
        """Consume connection bootstrap events (session_started, history,
        greeting response). Handles unexpected event types gracefully."""
        got_session = False
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
            mtype = msg.get("type")
            if mtype == "session_started":
                self.session_id = msg["session_id"]
                got_session = True
            elif mtype == "response":
                return msg  # greeting received — bootstrap done
            elif mtype == "error":
                raise RuntimeError(f"Server error during bootstrap: {msg.get('content', '')}")
            # Silently consume thinking, session_updated, history, etc.
        return None  # unreachable but satisfies the type checker

    async def connect(self):
        self.ws = await ws_connect(_ws_url(), open_timeout=30)
        greeting = await self._bootstrap()
        if greeting:
            print(f"  Greeting: {_safe(greeting.get('content', ''), 80)}...")

    async def reconnect(self):
        if self.ws:
            await self.ws.close()
        self.ws = await ws_connect(_ws_url(), open_timeout=30)
        await self._bootstrap()

    async def send(self, content: str, short_timeout: float | None = None) -> list[dict]:
        """Send a message and collect all events until a terminal event.

        Terminal conditions:
        - Multi-task flow  -> ``multi_task_completed``
        - Delegated (skill)-> ``task_completed`` (not ``response``, which
          arrives slightly before ``task_completed``)
        - Inline / error   -> ``response`` or ``error`` with no prior
          ``task_started``

        *short_timeout* overrides the per-recv timeout (useful for tests
        that expect zero events like whitespace messages).
        """
        await self.ws.send(json.dumps({"type": "message", "content": content}))
        events: list[dict] = []
        in_multi = False
        has_task = False  # True once we see a task_started
        recv_timeout = short_timeout or 60
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=recv_timeout)
                evt = json.loads(raw)
                events.append(evt)

                etype = evt.get("type")

                # Auto-approve permissions
                if etype == "permission_request":
                    await self.ws.send(json.dumps({
                        "type": "approve_permission",
                        "request_id": evt["request_id"],
                        "approval_mode": "session",
                    }))
                    continue

                # Auto-confirm skill questions
                if etype == "skill_confirm":
                    await self.ws.send(json.dumps({
                        "type": "user_response",
                        "request_id": evt["request_id"],
                        "response": True,
                    }))
                    continue

                # Track flow type
                if etype == "multi_task_started":
                    in_multi = True
                    continue
                if etype == "task_started":
                    has_task = True
                    continue

                # ── Terminal conditions ──

                # Multi-task: wait for multi_task_completed
                if etype == "multi_task_completed":
                    await self._drain(events)
                    break

                # Delegated single-task: wait for task_completed
                if has_task and not in_multi and etype == "task_completed":
                    await self._drain(events)
                    break

                # Inline (no task_started seen): response/error is terminal
                if not has_task and not in_multi and etype in ("response", "error"):
                    await self._drain(events)
                    break

            except asyncio.TimeoutError:
                break
        return events

    async def _drain(self, events: list[dict]):
        """Best-effort async drain of trailing events after terminal."""
        try:
            while True:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=4)
                events.append(json.loads(raw))
        except (asyncio.TimeoutError, Exception):
            pass

    async def run_test(
        self,
        name: str,
        message: str,
        checks: list,
        short_timeout: float | None = None,
    ):
        print(f"\n{'='*60}")
        print(f"TEST: {name}")
        print(f"  Sending: {_safe(message, 100)}")

        start = time.monotonic()
        events = await self.send(message, short_timeout=short_timeout)
        duration = time.monotonic() - start

        types = _types(events)
        responses = _responses(events)
        errors = [e for e in events if e.get("type") in ("error", "task_failed")]

        print(f"  Duration: {duration:.1f}s")
        print(f"  Events:   {types}")
        for r in responses:
            print(f"  Response: {_safe(r)}...")
        for e in errors:
            print(f"  ERROR:    {_safe(e.get('content', e.get('error', '')))}")

        failures = []
        for check_fn in checks:
            try:
                result = check_fn(events)
                # Support async checks (e.g. file content verification)
                if asyncio.iscoroutine(result):
                    await result
            except AssertionError as e:
                failures.append(str(e))

        passed = len(failures) == 0
        fail_reason = " | ".join(failures) if failures else ""
        status = "PASS" if passed else "FAIL"
        print(f"  Result:   {status}" + (f" -- {fail_reason}" if fail_reason else ""))

        self.results.append(TestResult(
            name=name, passed=passed, events=events,
            error=fail_reason, duration=duration,
        ))

    async def close(self):
        if self.ws:
            await self.ws.close()


# ══════════════════════════════════════════════════════════════════
# CHECK FUNCTIONS
# Each takes a list of events and raises AssertionError on failure.
# ══════════════════════════════════════════════════════════════════

def has_event(event_type: str):
    def _check(events):
        assert any(e.get("type") == event_type for e in events), \
            f"Missing event type: {event_type}"
    return _check


def has_multi_task(min_count: int = 2):
    """Assert a multi_task_started event with at least *min_count* sub-tasks."""
    def _check(events):
        mts = [e for e in events if e.get("type") == "multi_task_started"]
        assert len(mts) >= 1, "Missing multi_task_started event"
        actual = mts[0].get("sub_task_count", 0)
        assert actual >= min_count, \
            f"Expected >= {min_count} sub-tasks, got {actual}"
    return _check


def has_dependencies():
    """Assert at least one sub-task has a depends_on link (verified via
    event ordering: a task_completed must precede the next wave's
    task_started)."""
    def _check(events):
        # Wave boundary is signaled by a 'status' event containing 'wave'
        saw_wave_boundary = any(
            e.get("type") == "status" and "wave" in e.get("content", "").lower()
            for e in events
        )
        assert saw_wave_boundary, \
            "No wave boundary found — expected at least one depends_on link"
    return _check


def has_multi_task_completed(min_succeeded: int = 1):
    def _check(events):
        mtc = [e for e in events if e.get("type") == "multi_task_completed"]
        assert len(mtc) >= 1, "Missing multi_task_completed event"
        succeeded = mtc[0].get("succeeded", 0)
        assert succeeded >= min_succeeded, \
            f"Expected >= {min_succeeded} succeeded, got {succeeded}"
    return _check


def no_errors():
    def _check(events):
        errs = [e for e in events if e.get("type") in ("error", "task_failed")]
        assert len(errs) == 0, \
            f"Unexpected errors: {[_safe(e.get('content', e.get('error', '')), 80) for e in errs]}"
    return _check


def no_multi_task():
    """Assert the request was NOT decomposed into a multi-task flow."""
    def _check(events):
        assert not any(e.get("type") == "multi_task_started" for e in events), \
            "Simple request was over-decomposed into multi-task"
    return _check


def response_contains(keyword: str):
    def _check(events):
        text = " ".join(e.get("content", "") for e in events if e.get("type") == "response").lower()
        assert keyword.lower() in text, \
            f"Response missing keyword: '{keyword}'"
    return _check


def intermediate_responses_suppressed():
    """In a multi-task chain with dependencies, only leaf tasks (those
    NOT consumed by a downstream task) should emit response events.
    Intermediate tasks' responses should be suppressed.

    This mirrors the orchestrator's _intermediate set logic: a sub-task
    is intermediate if any other sub-task's depends_on includes it."""
    def _check(events):
        # Collect sub_task_index from task_started events to know which
        # indices exist, and from response events to know which responded.
        in_multi = False
        task_started_indices = set()
        response_indices = set()
        completed_before_wave2 = set()
        saw_wave_boundary = False

        for e in events:
            if e.get("type") == "multi_task_started":
                in_multi = True
                continue
            if e.get("type") == "multi_task_completed":
                break
            if not in_multi:
                continue

            sub_idx = e.get("sub_task_index")
            if e.get("type") == "task_started" and sub_idx is not None:
                task_started_indices.add(sub_idx)
            elif e.get("type") == "task_completed" and sub_idx is not None and not saw_wave_boundary:
                completed_before_wave2.add(sub_idx)
            elif e.get("type") == "status" and "wave" in e.get("content", "").lower():
                saw_wave_boundary = True
            elif e.get("type") == "response" and sub_idx is not None:
                response_indices.add(sub_idx)

        # Intermediate = completed before the wave boundary (they fed
        # downstream tasks). They should NOT have response events.
        if saw_wave_boundary and completed_before_wave2:
            leaked = response_indices & completed_before_wave2
            assert len(leaked) == 0, \
                f"Intermediate sub-tasks {leaked} leaked response events to user"
    return _check


def has_wave_status():
    """Multi-wave chains should emit a 'Starting wave N' status event."""
    def _check(events):
        wave_msgs = [
            e for e in events
            if e.get("type") == "status" and "wave" in e.get("content", "").lower()
        ]
        assert len(wave_msgs) >= 1, "Missing wave status event for multi-wave chain"
    return _check


def every_task_completed():
    """Every task_started must have a corresponding task_completed with
    the same task_id. Catches orphaned tasks."""
    def _check(events):
        started = {e["task_id"] for e in events if e.get("type") == "task_started" and "task_id" in e}
        completed = {e["task_id"] for e in events if e.get("type") == "task_completed" and "task_id" in e}
        orphaned = started - completed
        assert len(orphaned) == 0, \
            f"Tasks started but never completed: {orphaned}"
    return _check


def wave_order_respected():
    """In a sequential chain, all wave-1 task_completed events must
    precede any wave-2 task_started events."""
    def _check(events):
        in_multi = False
        saw_wave_boundary = False
        completed_count_before_wave2 = 0
        started_after_wave2 = 0

        for e in events:
            if e.get("type") == "multi_task_started":
                in_multi = True
                continue
            if not in_multi:
                continue
            if e.get("type") == "multi_task_completed":
                break
            if e.get("type") == "status" and "wave" in e.get("content", "").lower():
                saw_wave_boundary = True
                continue
            if not saw_wave_boundary and e.get("type") == "task_completed":
                completed_count_before_wave2 += 1
            if saw_wave_boundary and e.get("type") == "task_started":
                started_after_wave2 += 1

        if saw_wave_boundary:
            assert completed_count_before_wave2 >= 1, \
                "Wave 2 started but no wave 1 tasks completed first"
            assert started_after_wave2 >= 1, \
                "Wave boundary emitted but no wave 2 tasks started"
    return _check


def file_contains_search_data():
    """Verify the most recently created file in the AgentOS output
    directory contains substantive content from search results (not
    just a generic LLM generation)."""
    def _check(events):
        # Extract the file path from the response
        resp_text = " ".join(e.get("content", "") for e in events if e.get("type") == "response")
        # The Files skill response format: Created **filename** (size)\n  /full/path
        path_match = re.search(r"[A-Z]:\\[^\n]+\.md", resp_text)
        if not path_match:
            # Try to find any .md file in output dir created in last 60s
            candidates = sorted(FILES_OUTPUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                raise AssertionError("No output file found to verify pipeline content")
            file_path = candidates[0]
        else:
            file_path = Path(path_match.group(0))

        assert file_path.exists(), f"Output file not found: {file_path}"

        content = file_path.read_text(encoding="utf-8")
        # A file generated from pipeline context should be substantial
        # (the search results alone are typically 500+ chars)
        assert len(content) > 300, \
            f"Output file suspiciously short ({len(content)} chars) — pipeline context may not have been used"
        # Check for hallmarks of real content, not a stub
        assert any(marker in content.lower() for marker in ["http", "source", "according", "report", "analysis", "trend", "market", "data"]), \
            "Output file lacks substantive content — may be generated without search data"
    return _check


# ══════════════════════════════════════════════════════════════════
# TEST CASES
# ══════════════════════════════════════════════════════════════════

async def main():
    t = AgentTester()
    test_start_epoch = datetime.now(timezone.utc).timestamp()
    # Track which test name produced each multi-task flow
    flow_test_names: list[str] = []

    print("Connecting to MUSE...")
    await t.connect()
    print(f"Session: {t.session_id}\n")

    # ─── T1: Inline greeting (baseline) ──────────────────────────
    await t.run_test(
        "T1: Inline greeting",
        "Hello! What can you help me with?",
        checks=[has_event("response"), no_errors()],
    )

    # ─── T2: Single skill delegation (search) ────────────────────
    await t.run_test(
        "T2: Single search delegation",
        "Search for the latest trends in renewable energy.",
        checks=[
            has_event("task_started"),
            has_event("response"),
            every_task_completed(),
            no_errors(),
        ],
    )

    # ─── T3: Single skill delegation (note) ──────────────────────
    await t.run_test(
        "T3: Single note delegation",
        "Save a note: Review renewable energy trends tomorrow.",
        checks=[
            has_event("task_started"),
            has_event("response"),
            every_task_completed(),
            no_errors(),
        ],
    )

    # ─── T4: Multi-task parallel (no dependencies) ───────────────
    await t.reconnect()
    flow_test_names.append("T4")

    await t.run_test(
        "T4: Parallel multi-task (search + note, no deps)",
        "Search for Python 3.13 features and also save a note about today's test session.",
        checks=[
            has_multi_task(min_count=2),
            has_multi_task_completed(min_succeeded=2),
            every_task_completed(),
            no_errors(),
        ],
    )

    # ─── T5: Multi-task sequential chain (search -> file) ────────
    # Key test for the pipeline + suppression fixes.
    await t.reconnect()
    flow_test_names.append("T5")

    await t.run_test(
        "T5: Sequential chain (search -> file)",
        "Search for the top programming languages in 2026 and then save a summary report to a file.",
        checks=[
            has_multi_task(min_count=2),
            has_dependencies(),
            has_multi_task_completed(min_succeeded=2),
            has_wave_status(),
            wave_order_respected(),
            intermediate_responses_suppressed(),
            every_task_completed(),
            file_contains_search_data(),
            no_errors(),
        ],
    )

    # ─── T6: Multi-task diamond (2x search -> file) ──────────────
    await t.reconnect()
    flow_test_names.append("T6")

    await t.run_test(
        "T6: Diamond dependency (2x search -> file)",
        "Search for recent AI safety news and also search for AI regulation updates, then combine both into a report file.",
        checks=[
            has_multi_task(min_count=2),
            has_dependencies(),
            has_multi_task_completed(min_succeeded=2),
            wave_order_respected(),
            intermediate_responses_suppressed(),
            every_task_completed(),
            file_contains_search_data(),
            no_errors(),
        ],
    )

    # ─── T7: Conversation context carry-over ─────────────────────
    await t.reconnect()

    await t.run_test(
        "T7a: Context setup (search)",
        "Search for SpaceX latest launch updates.",
        checks=[has_event("response"), every_task_completed(), no_errors()],
    )
    await t.run_test(
        "T7b: Context follow-up (save those results)",
        "Save those results to a file.",
        checks=[
            has_event("task_started"),
            has_event("response"),
            every_task_completed(),
            no_errors(),
        ],
    )

    # ─── T8: Simple note (should NOT multi-decompose) ────────────
    await t.run_test(
        "T8: Simple request (no over-decomposition)",
        "Save a note: buy groceries.",
        checks=[
            has_event("response"),
            no_multi_task(),
            every_task_completed(),
            no_errors(),
        ],
    )

    # ─── T9: Ambiguous message (no crash) ────────────────────────
    await t.run_test(
        "T9: Ambiguous message (graceful handling)",
        "Help me think through my project architecture.",
        checks=[has_event("response"), no_errors()],
    )

    # ─── T10: Empty-ish message (edge case) ──────────────────────
    await t.run_test(
        "T10: Near-empty message",
        "   ",
        checks=[no_errors()],
        short_timeout=8,
    )

    # ─── T11: Multi-turn summarization ───────────────────────────
    await t.run_test(
        "T11: Session summarization",
        "Summarize everything we've done so far.",
        checks=[has_event("response"), no_errors()],
    )

    await t.close()

    # ══════════════════════════════════════════════════════════════
    # TEST SUMMARY
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    total = len(t.results)
    passed = sum(1 for r in t.results if r.passed)
    failed = total - passed
    for r in t.results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name} ({r.duration:.1f}s)")
        if not r.passed:
            print(f"         {r.error}")
    print(f"\n  {passed}/{total} passed, {failed} failed")

    # Save raw events
    out_path = Path(__file__).parent / "test_full_potential_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            [{"name": r.name, "passed": r.passed, "error": r.error,
              "duration": r.duration, "events": r.events}
             for r in t.results],
            f, indent=2, default=str,
        )
    print(f"  Raw events saved to {out_path}")

    # ══════════════════════════════════════════════════════════════
    # LOG INSPECTION
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("LOG INSPECTION")
    print("=" * 60)

    log_path = _latest_log()
    if not log_path:
        print("  No debug log found. Is debug mode enabled?")
    else:
        print(f"  Log file: {log_path}")
        entries = _parse_log(log_path, after_epoch=test_start_epoch)
        print(f"  Entries since test start: {len(entries)}")

        # ── Classify events ──
        classify_results = [
            e for e in entries
            if e.get("category") == "classify" and e.get("event") == "result"
        ]
        llm_events = [e for e in entries if e.get("category") == "llm"]
        task_events = [
            e for e in entries
            if e.get("category") == "orchestrator"
            and e.get("event") in ("task_spawn", "task_complete")
        ]
        ws_sends = [
            e for e in entries
            if e.get("category") == "ws" and e.get("event") == "send"
        ]
        brief_events = [
            e for e in entries
            if e.get("category") == "orchestrator" and e.get("event") == "brief"
        ]

        print(f"\n  Classification decisions: {len(classify_results)}")
        for ce in classify_results:
            d = ce.get("data", {})
            mode = d.get("mode", "?")
            skills = d.get("skill_ids") or [d.get("skill_id", "?")]
            subs = d.get("sub_tasks", [])
            deps = [s.get("depends_on", []) for s in subs]
            print(f"    {mode}: skills={skills}, sub_tasks={len(subs)}, deps={deps}")

        total_tok_in = sum(e.get("data", {}).get("tokens_in", 0) for e in llm_events)
        total_tok_out = sum(e.get("data", {}).get("tokens_out", 0) for e in llm_events)
        print(f"\n  LLM calls: {len(llm_events)} ({total_tok_in} tokens in / {total_tok_out} tokens out)")

        spawned = [e for e in task_events if e.get("event") == "task_spawn"]
        completed = [e for e in task_events if e.get("event") == "task_complete"]
        print(f"\n  Tasks: {len(spawned)} spawned, {len(completed)} completed")
        for te in task_events:
            d = te.get("data", {})
            print(f"    {te['event']}: {d.get('skill_id', '?')} [{d.get('status', '')}]"
                  f" task={d.get('task_id', '?')[:12]}")

        # ── Pipeline context ──
        pipeline_briefs = [b for b in brief_events if b.get("data", {}).get("has_pipeline_ctx")]
        print(f"\n  Pipeline context deliveries: {len(pipeline_briefs)}")
        for be in pipeline_briefs:
            d = be.get("data", {})
            print(f"    task={d.get('task_id', '?')[:12]}: "
                  f"keys={d.get('pipeline_keys', [])}")

        # ── Intermediate suppression diagnostic ──
        # Walk ws:send events and count responses per multi-task flow,
        # correlating each flow to the test that produced it.
        ws_events_typed = [(e.get("data", {}).get("event_type", ""), e) for e in ws_sends]

        print(f"\n  Multi-task flow analysis:")
        flow_idx = 0
        in_multi = False
        resp_count = 0
        leaf_count = 0  # tasks that responded (leaf nodes)

        for et, _ in ws_events_typed:
            if et == "multi_task_started":
                in_multi = True
                resp_count = 0
                continue
            elif et == "multi_task_completed":
                test_name = flow_test_names[flow_idx] if flow_idx < len(flow_test_names) else "?"
                # For chains with wave boundaries, only 1 response expected.
                # For pure parallel (no deps), all responses are expected.
                print(f"    Flow {flow_idx+1} ({test_name}): {resp_count} response(s)")
                flow_idx += 1
                in_multi = False
            elif et == "response" and in_multi:
                resp_count += 1

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
