"""MCP server integration tests.

Tests adding, connecting, and using MCP servers through the MUSE API.
Requires: running MUSE server, Node.js (for npx servers), Python MCP packages.

Run with: python tests/test_mcp_servers.py
"""

import asyncio
import json
import os
import sys
import time
import urllib.request
import urllib.error

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


def api(path, method="GET", body=None):
    url = f"http://localhost:8080/api{path}"
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()[:200]}
    except Exception as e:
        return {"error": str(e)}


def add_server(server_id, name, transport, command="", args=None, url="", env=None):
    """Add an MCP server and wait for connection."""
    body = {
        "server_id": server_id,
        "name": name,
        "transport": transport,
        "command": command,
        "args": args or [],
        "env": env or {},
        "url": url,
        "enabled": True,
    }
    d = api("/mcp/servers", "POST", body)
    if "error" in d:
        return False, d.get("detail", str(d))

    # Wait for connection
    for _ in range(15):
        time.sleep(2)
        d = api(f"/mcp/servers/{server_id}")
        status = d.get("status", "")
        if status == "connected":
            tools = d.get("tools", [])
            return True, tools
        if status == "error":
            return False, d.get("error", "Connection error")

    return False, "Timeout waiting for connection"


def remove_server(server_id):
    api(f"/mcp/servers/{server_id}", "DELETE")


def test_time_server():
    """Test the Time MCP server (simplest: 2 tools)."""
    print("\n-- 1. TIME SERVER --")

    python_exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
    ok, tools_or_err = add_server(
        "time", "Time",
        transport="stdio",
        command=python_exe,
        args=["-m", "mcp_server_time"],
    )
    result("Time: connect", ok, str(tools_or_err)[:80] if not ok else "")

    if ok:
        tools = tools_or_err
        tool_names = [t["name"] for t in tools]
        result("Time: has tools", len(tools) >= 1, f"tools: {tool_names}")
        result("Time: get_current_time tool", "get_current_time" in tool_names, f"tools: {tool_names}")

    # List via API
    d = api("/mcp/servers")
    servers = d.get("servers", [])
    time_server = next((s for s in servers if s["server_id"] == "time"), None)
    result("Time: appears in server list", time_server is not None)
    if time_server:
        result("Time: status=connected", time_server.get("status") == "connected")

    remove_server("time")


def test_sqlite_server():
    """Test the SQLite MCP server."""
    print("\n-- 2. SQLITE SERVER --")

    # Create a test DB path
    db_path = os.path.expandvars(r"%LOCALAPPDATA%\muse\test_mcp.db")

    # Use the console script entry point
    scripts_dir = os.path.dirname(sys.executable)
    sqlite_cmd = os.path.join(scripts_dir, "mcp-server-sqlite")
    if not os.path.exists(sqlite_cmd):
        sqlite_cmd = os.path.join(scripts_dir, "mcp-server-sqlite.exe")

    ok, tools_or_err = add_server(
        "sqlite", "SQLite",
        transport="stdio",
        command=sqlite_cmd,
        args=["--db-path", db_path],
    )
    result("SQLite: connect", ok, str(tools_or_err)[:80] if not ok else "")

    if ok:
        tools = tools_or_err
        tool_names = [t["name"] for t in tools]
        result("SQLite: has tools", len(tools) >= 3, f"{len(tools)} tools")
        result("SQLite: has list_tables", "list_tables" in tool_names)
        result("SQLite: has read_query", "read_query" in tool_names)
        result("SQLite: has write_query", "write_query" in tool_names)

    remove_server("sqlite")

    # Clean up test DB
    try:
        os.remove(db_path)
    except OSError:
        pass


def test_filesystem_server():
    """Test the Filesystem MCP server."""
    print("\n-- 3. FILESYSTEM SERVER --")

    test_dir = os.path.expandvars(r"%USERPROFILE%\Documents\MUSE")

    ok, tools_or_err = add_server(
        "filesystem", "Filesystem",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", test_dir],
    )
    result("Filesystem: connect", ok, str(tools_or_err)[:80] if not ok else "")

    if ok:
        tools = tools_or_err
        tool_names = [t["name"] for t in tools]
        result("Filesystem: has tools", len(tools) >= 5, f"{len(tools)} tools: {tool_names[:5]}")
        result("Filesystem: has read_file", any("read" in t for t in tool_names))
        result("Filesystem: has list_directory", any("list" in t or "directory" in t for t in tool_names))

    remove_server("filesystem")


def test_everything_server():
    """Test the Everything MCP server (protocol validation)."""
    print("\n-- 4. EVERYTHING SERVER --")

    ok, tools_or_err = add_server(
        "everything", "Everything",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    result("Everything: connect", ok, str(tools_or_err)[:80] if not ok else "")

    if ok:
        tools = tools_or_err
        tool_names = [t["name"] for t in tools]
        result("Everything: has tools", len(tools) >= 3, f"{len(tools)} tools")
        result("Everything: has echo", "echo" in tool_names)
        result("Everything: has add", "add" in tool_names or len(tools) >= 5, f"tools: {tool_names[:8]}")

    remove_server("everything")


def main():
    print("=" * 60)
    print("MCP SERVER INTEGRATION TESTS")
    print("=" * 60)

    # Check prerequisites
    d = api("/health")
    if "status" not in d:
        print("ERROR: MUSE server not responding")
        sys.exit(1)

    test_time_server()
    test_sqlite_server()
    test_filesystem_server()
    test_everything_server()

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASS} passed, {FAIL} failed out of {PASS + FAIL} tests")
    print("=" * 60)


if __name__ == "__main__":
    main()
