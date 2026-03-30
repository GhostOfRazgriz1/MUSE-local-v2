"""MUSE integration tests — exercises skills via WebSocket like a real user.

Run with: python tests/test_agent.py
Requires the server to be running (preferably with --debug).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field

import websockets
from websockets.asyncio.client import connect as ws_connect

WS_URL = "ws://localhost:8080/api/ws/chat"
TIMEOUT = 120  # max seconds to wait for a response


@dataclass
class TestResult:
    name: str
    passed: bool
    events: list[dict] = field(default_factory=list)
    error: str = ""
    duration: float = 0.0


class AgentTester:
    """Connects via WebSocket and runs test scenarios."""

    def __init__(self):
        self.ws = None
        self.session_id: str | None = None
        self.results: list[TestResult] = []

    async def connect(self):
        self.ws = await ws_connect(WS_URL, open_timeout=30)
        # Read session_started + greeting
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=15))
            if msg.get("type") == "session_started":
                self.session_id = msg["session_id"]
            elif msg.get("type") == "response":
                print(f"  Greeting: {msg['content'][:80]}...")
                break
            elif msg.get("type") == "history":
                continue

    async def reconnect(self):
        """Start a fresh session."""
        if self.ws:
            await self.ws.close()
        self.ws = await ws_connect(WS_URL, open_timeout=30)
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=15))
            if msg.get("type") == "session_started":
                self.session_id = msg["session_id"]
            elif msg.get("type") == "response":
                break
            elif msg.get("type") == "history":
                continue

    async def send(self, content: str) -> list[dict]:
        """Send a message and collect all response events until idle."""
        await self.ws.send(json.dumps({"type": "message", "content": content}))
        events = []
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
                evt = json.loads(raw)
                events.append(evt)

                # Auto-approve permissions
                if evt.get("type") == "permission_request":
                    await self.ws.send(json.dumps({
                        "type": "approve_permission",
                        "request_id": evt["request_id"],
                        "approval_mode": "session",
                    }))
                    continue

                # Auto-confirm skill questions (e.g., directory access)
                if evt.get("type") == "skill_confirm":
                    await self.ws.send(json.dumps({
                        "type": "user_response",
                        "request_id": evt["request_id"],
                        "response": True,
                    }))
                    continue

                # Terminal events — stop collecting
                if evt.get("type") in ("response", "error", "multi_task_completed"):
                    # Keep draining for a bit in case there are follow-up events
                    try:
                        while True:
                            raw = await asyncio.wait_for(self.ws.recv(), timeout=3)
                            events.append(json.loads(raw))
                    except (asyncio.TimeoutError, Exception):
                        pass
                    break

            except asyncio.TimeoutError:
                break

        return events

    async def run_test(self, name: str, message: str,
                       expect_types: list[str] | None = None,
                       expect_content: list[str] | None = None,
                       expect_no_error: bool = True):
        """Run a single test case."""
        print(f"\n{'='*60}")
        print(f"TEST: {name}")
        print(f"  Sending: {message}")

        start = time.monotonic()
        events = await self.send(message)
        duration = time.monotonic() - start

        # Collect event types
        types = [e.get("type") for e in events]
        responses = [e.get("content", "") for e in events if e.get("type") == "response"]
        errors = [e for e in events if e.get("type") in ("error", "task_failed")]

        print(f"  Duration: {duration:.1f}s")
        print(f"  Events: {types}")
        if responses:
            for r in responses:
                preview = r[:150].replace("\n", " ")
                print(f"  Response: {preview}...")
        if errors:
            for e in errors:
                print(f"  ERROR: {e.get('content', e.get('error', ''))}")

        # Check assertions
        passed = True
        fail_reason = ""

        if expect_no_error and errors:
            passed = False
            fail_reason = f"Unexpected errors: {[e.get('content', e.get('error', '')) for e in errors]}"

        if expect_types:
            for t in expect_types:
                if t not in types:
                    passed = False
                    fail_reason += f" Missing expected event type: {t}."

        if expect_content:
            full_text = " ".join(responses).lower()
            for keyword in expect_content:
                if keyword.lower() not in full_text:
                    passed = False
                    fail_reason += f" Missing expected content: '{keyword}'."

        status = "PASS" if passed else "FAIL"
        print(f"  Result: {status}" + (f" — {fail_reason}" if fail_reason else ""))

        self.results.append(TestResult(
            name=name, passed=passed, events=events,
            error=fail_reason, duration=duration,
        ))

    async def close(self):
        if self.ws:
            await self.ws.close()


async def main():
    t = AgentTester()

    print("Connecting to MUSE...")
    await t.connect()
    print(f"Session: {t.session_id}")

    # ─── Test 1: Inline response (general chat) ──────────────
    await t.run_test(
        "Inline: greeting",
        "Hello! What can you do?",
        expect_types=["response"],
    )

    # ─── Test 2: Search skill routing ─────────────────────────
    await t.run_test(
        "Search: basic query",
        "Search for the latest developments in quantum computing.",
        expect_types=["task_started", "response"],
    )

    # ─── Test 3: Notes skill ─────────────────────────────────
    await t.run_test(
        "Notes: create a note",
        "Save a note: Remember to review the quantum computing results tomorrow.",
        expect_types=["task_started", "response"],
    )

    # ─── Test 4: Notes skill — list ──────────────────────────
    await t.run_test(
        "Notes: list notes",
        "Show me my notes.",
        expect_types=["task_started", "response"],
    )

    # ─── Test 5: Files skill — content generation ─────────────
    await t.run_test(
        "Files: generate content",
        "Write a Python script that calculates the Fibonacci sequence.",
        expect_types=["task_started", "response"],
        expect_content=["fibonacci", ".py"],
    )

    # ─── Test 6: Files skill — save conversation content ──────
    await t.run_test(
        "Files: save this to a file",
        "Save the quantum computing search results to a file.",
        expect_types=["task_started", "response"],
    )

    # ─── Test 7: Multi-task — parallel ────────────────────────
    # Fresh session to avoid permission caching noise
    await t.reconnect()

    await t.run_test(
        "Multi-task: parallel (search + note)",
        "Search for recent AI safety news and also create a note about today's testing session.",
        expect_types=["response"],
    )

    # ─── Test 8: Multi-task — sequential chain ────────────────
    await t.reconnect()

    await t.run_test(
        "Multi-task: sequential (search then save)",
        "Search for Python 3.13 new features and then save the results to a file.",
        expect_types=["response"],
    )

    # ─── Test 9: Intent classification — edge cases ───────────
    await t.run_test(
        "Classification: ambiguous (should not crash)",
        "Can you help me organize my thoughts about the project?",
        expect_types=["response"],
        expect_no_error=True,
    )

    # ─── Test 10: Inline follow-up with context ──────────────
    await t.run_test(
        "Inline: follow-up (uses conversation context)",
        "Summarize everything we've done in this session.",
        expect_types=["response"],
    )

    await t.close()

    # ─── Summary ──────────────────────────────────────────────
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

    # Save raw events for inspection
    out_path = "tests/test_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            [{"name": r.name, "passed": r.passed, "error": r.error,
              "duration": r.duration, "events": r.events}
             for r in t.results],
            f, indent=2, default=str,
        )
    print(f"  Raw events saved to {out_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
