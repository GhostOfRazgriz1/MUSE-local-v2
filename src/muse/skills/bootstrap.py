"""Skill process bootstrap — runs inside sandboxed subprocess.

This script is executed by the warm pool. It:
1. Connects to the orchestrator via IPC
2. Receives the INIT message with task brief and permissions
3. Imports and runs the skill
4. Reports status back via IPC
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import traceback
from pathlib import Path


async def main():
    """Bootstrap a skill process."""
    # Read config from stdin (sent by orchestrator)
    config_line = sys.stdin.readline().strip()
    if not config_line:
        sys.exit(1)

    config = json.loads(config_line)
    task_id = config["task_id"]
    skill_id = config["skill_id"]
    skill_path = config["skill_path"]
    ipc_dir = config.get("ipc_dir", "")
    brief = config["brief"]
    permissions = config["permissions"]
    skill_config = config.get("config", {})

    # Connect to orchestrator IPC
    sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "sdk"))
    from muse_sdk.ipc_client import IPCClient, StatusMsg
    from muse_sdk.context import SkillContext, SkillResult

    ipc = IPCClient(task_id, ipc_dir)

    try:
        await ipc.connect()
    except Exception as e:
        # Fallback: communicate via stdout
        print(json.dumps({"type": "status", "status": "failed", "error": f"IPC connect failed: {e}"}))
        sys.exit(1)

    try:
        # Report started
        await ipc.send(StatusMsg(status="started", description=f"Skill {skill_id} starting"))

        # Create context
        ctx = SkillContext(
            task_id=task_id,
            skill_id=skill_id,
            brief=brief,
            permissions=permissions,
            config=skill_config,
            ipc_client=ipc,
        )

        # Import and run the skill
        spec = importlib.util.spec_from_file_location("skill", skill_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if not hasattr(module, "run"):
            await ipc.send(StatusMsg(
                status="failed",
                error=f"Skill {skill_id} has no 'run' function",
            ))
            return

        result = await module.run(ctx)

        # Normalize result
        if isinstance(result, dict):
            payload = result
        elif isinstance(result, SkillResult):
            payload = {
                "payload": result.payload,
                "summary": result.summary,
                "facts": result.facts,
                "success": result.success,
                "error": result.error,
            }
        else:
            payload = {"payload": result, "summary": str(result), "success": True}

        await ipc.send(StatusMsg(
            status="completed",
            description="Task completed",
            result=payload,
        ))

    except Exception as e:
        tb = traceback.format_exc()
        await ipc.send(StatusMsg(
            status="failed",
            error=f"{type(e).__name__}: {e}",
            description=tb,
            is_retryable=False,
        ))

    finally:
        await ipc.close()


if __name__ == "__main__":
    asyncio.run(main())
