"""Comprehensive user simulation test for MUSE.

Run with: python tests/test_comprehensive.py
Requires: running server on localhost:8080, Ollama with models pulled.
"""

import asyncio
import json
import os
import sys
import time
import urllib.request
import urllib.error

# Load token
TOKEN_PATH = os.path.expandvars(r"%LOCALAPPDATA%\muse\.api_token")
if not os.path.exists(TOKEN_PATH):
    print("ERROR: No .api_token found. Is the server running?")
    sys.exit(1)
TOKEN = open(TOKEN_PATH).read().strip()

PASS = 0
FAIL = 0


def result(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: {detail}")


async def api(path, method="GET", body=None):
    url = f"http://localhost:8080/api{path}"
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()[:200]}


async def ws_send_and_wait(ws, content, timeout=300):
    await ws.send(json.dumps({"type": "message", "content": content}))
    t0 = time.time()
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            t = msg["type"]
            if t == "permission_request":
                rid = msg.get("request_id")
                await ws.send(json.dumps({
                    "type": "approve_permission",
                    "request_id": rid,
                    "approval_mode": "session",
                }))
            elif t == "response":
                return msg.get("content", ""), time.time() - t0
            elif t == "error":
                return f"ERROR: {msg.get('content', '')}", time.time() - t0
            elif t == "greeting":
                return msg.get("content", ""), time.time() - t0
        except asyncio.TimeoutError:
            return "TIMEOUT", timeout


async def run_comprehensive_test():
    import websockets

    print("=" * 60)
    print("MUSE COMPREHENSIVE TEST — Fresh Install")
    print("=" * 60)

    # --1. SETUP CARD ──
    print("\n--1. SETUP CARD --")

    d = await api("/settings/local")
    result("Setup detection (no config)", d.get("config") is None)

    d = await api("/settings/local/test", "POST", {"address": "localhost", "port": 11434})
    result("Test connection", d.get("status") == "ok", d.get("message", ""))
    models = d.get("models", [])
    result("Models discovered", len(models) > 0, f"{len(models)} models")

    d = await api("/settings/local", "PUT", {
        "runtime": "ollama", "address": "localhost", "port": 11434,
        "models": models, "max_workers": 2,
    })
    result("Save config", d.get("status") == "configured")

    d = await api("/settings/local")
    result("Config persisted", d.get("config") is not None)

    # --2. ONBOARDING ──
    print("\n--2. ONBOARDING --")

    uri = f"ws://localhost:8080/api/ws/chat?token={TOKEN}&tz=America/New_York"
    async with websockets.connect(uri, ping_interval=30, ping_timeout=300) as ws:
        for _ in range(10):
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            msg = json.loads(raw)
            if msg["type"] == "response":
                result("Onboarding greeting", "call you" in msg["content"].lower() or "set up" in msg["content"].lower(), msg["content"][:60])
                break

        resp, t = await ws_send_and_wait(ws, "Call me Alex")
        result("Name extraction", "alex" in resp.lower(), resp[:60])

        resp, t = await ws_send_and_wait(ws, "Name yourself Nova")
        result("Agent name", "nova" in resp.lower(), resp[:60])

        resp, t = await ws_send_and_wait(ws, "professional")
        result("Personality set", "professional" in resp.lower() or "polished" in resp.lower(), resp[:80])

    # Verify identity file
    identity_path = os.path.expandvars(r"%LOCALAPPDATA%\muse\identity.md")
    if os.path.exists(identity_path):
        identity = open(identity_path, encoding="utf-8").read()
        result("Identity file created", "Nova" in identity and "Alex" in identity)
        result("Identity has personality", "professional" in identity.lower() or "polished" in identity.lower())
    else:
        result("Identity file created", False, "File not found")
        result("Identity has personality", False)

    # --3. CHAT & CLASSIFICATION ──
    print("\n--3. CHAT & CLASSIFICATION --")

    async with websockets.connect(uri, ping_interval=30, ping_timeout=300) as ws:
        for _ in range(10):
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            msg = json.loads(raw)
            if msg["type"] in ("response", "greeting"):
                result("Session greeting", len(msg.get("content", "")) > 10, msg["content"][:60])
                break

        resp, t = await ws_send_and_wait(ws, "Hello!")
        result("Inline chat", "TIMEOUT" not in resp and "ERROR" not in resp, f"{t:.1f}s")

        resp, t = await ws_send_and_wait(ws, "What is 2 + 2?")
        result("Knowledge answer", "4" in resp, f"{t:.1f}s — {resp[:60]}")

        # --4. SKILL EXECUTION ──
        print("\n--4. SKILL EXECUTION --")

        resp, t = await ws_send_and_wait(ws, "Set a reminder to take a break in 10 minutes")
        result("Reminder skill", "reminder" in resp.lower() or "set" in resp.lower(), f"{t:.1f}s — {resp[:80]}")

        resp, t = await ws_send_and_wait(ws, "Write a short poem about the ocean and save it to ocean.txt")
        result("File write skill", "ocean" in resp.lower() or ".txt" in resp.lower(), f"{t:.1f}s — {resp[:80]}")

        resp, t = await ws_send_and_wait(ws, "Save a note: project deadline is next Friday")
        result("Notes skill", "saved" in resp.lower() or "note" in resp.lower(), f"{t:.1f}s — {resp[:80]}")

        resp, t = await ws_send_and_wait(ws, "Read test_docs/architecture.md and tell me the tech stack")
        result("Documents skill", "TIMEOUT" not in resp and "ERROR" not in resp, f"{t:.1f}s — {resp[:80]}")

        # --5. IDENTITY EDITING ──
        print("\n--5. IDENTITY EDITING --")

        resp, t = await ws_send_and_wait(ws, "Change your name to Spark")
        result("Identity edit", "spark" in resp.lower() or "change" in resp.lower() or "updat" in resp.lower(), f"{t:.1f}s — {resp[:80]}")

    # --6. REST API ENDPOINTS ──
    print("\n--6. REST API --")

    d = await api("/settings/providers")
    result("Provider list", len(d.get("providers", [])) > 0)

    d = await api("/settings/models")
    result("Model list", len(d.get("models", [])) > 0, f"{len(d.get('models', []))} models")

    d = await api("/sessions")
    result("Session list", len(d) > 0, f"{len(d)} sessions")

    d = await api("/skills")
    skills = d if isinstance(d, list) else d.get("skills", [])
    result("Skills loaded", len(skills) >= 8, f"{len(skills)} skills")

    d = await api("/memories/stats")
    result("Memory stats", "total" in d, f"{d.get('total', 0)} memories")
    rel = d.get("relationship", {})
    result("Relationship tracking", rel.get("level", 0) >= 1, f"level {rel.get('level')}: {rel.get('label', '')}")

    d = await api("/tasks/usage")
    result("Usage tracking", d.get("llm", {}).get("calls", 0) > 0, f"{d.get('llm', {}).get('calls', 0)} LLM calls")

    # --7. SETTINGS ──
    print("\n--7. SETTINGS --")

    d = await api("/settings")
    result("Settings endpoint", "settings" in d)

    d = await api("/settings/models/overrides")
    result("Model overrides", "overrides" in d)

    # --8. MCP ──
    print("\n--8. MCP --")

    d = await api("/mcp/servers")
    result("MCP server list", "servers" in d)

    # --SUMMARY ──
    print("\n" + "=" * 60)
    print(f"RESULTS: {PASS} passed, {FAIL} failed out of {PASS + FAIL} tests")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_comprehensive_test())
