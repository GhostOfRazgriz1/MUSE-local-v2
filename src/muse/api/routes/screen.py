"""Screen vision REST + WebSocket endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from muse.api.app import get_orchestrator
from muse.screen.capture import CaptureRegion
from muse.screen.manager import ScreenMode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screen", tags=["screen"])


def _get_screen_manager():
    orch = get_orchestrator()
    if not orch or not hasattr(orch, "screen_manager"):
        return None
    return orch.screen_manager


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------

@router.get("/status")
async def screen_status():
    """Return current screen vision status and capabilities."""
    mgr = _get_screen_manager()
    if not mgr:
        return {"available": False, "reason": "Screen manager not initialized"}
    return await mgr.check_readiness()


# ------------------------------------------------------------------
# Start / Stop
# ------------------------------------------------------------------

@router.post("/start")
async def screen_start(body: dict | None = None):
    """Start screen streaming.

    Body (optional):
        mode: "passive" | "active"  (default: "passive")
    """
    mgr = _get_screen_manager()
    if not mgr:
        return {"error": "Screen manager not initialized"}

    mode_str = (body or {}).get("mode", "passive")
    try:
        mode = ScreenMode(mode_str)
    except ValueError:
        return {"error": f"Invalid mode: {mode_str}. Use 'passive' or 'active'."}

    try:
        await mgr.start(mode=mode)
    except RuntimeError as exc:
        return {"error": str(exc)}

    return {"status": "started", "mode": mode.value}


@router.post("/stop")
async def screen_stop():
    """Stop screen streaming."""
    mgr = _get_screen_manager()
    if not mgr:
        return {"error": "Screen manager not initialized"}
    await mgr.stop()
    return {"status": "stopped"}


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

@router.post("/configure")
async def screen_configure(body: dict):
    """Update screen capture configuration.

    Body fields (all optional):
        fps: float (0.1–10.0)
        max_frames: int
        max_dimension: int
        monitor: int
        region: {left, top, width, height} | null
    """
    mgr = _get_screen_manager()
    if not mgr:
        return {"error": "Screen manager not initialized"}

    region_data = body.get("region")
    region = None
    if region_data is None:
        region = "clear"
    elif isinstance(region_data, dict):
        region = CaptureRegion(
            left=region_data["left"],
            top=region_data["top"],
            width=region_data["width"],
            height=region_data["height"],
        )

    mgr.configure(
        fps=body.get("fps"),
        max_frames=body.get("max_frames"),
        max_dimension=body.get("max_dimension"),
        monitor=body.get("monitor"),
        region=region,
    )
    return {"status": "configured"}


# ------------------------------------------------------------------
# Safety
# ------------------------------------------------------------------

@router.post("/kill")
async def screen_kill():
    """Emergency stop — halt all screen automation immediately."""
    mgr = _get_screen_manager()
    if not mgr:
        return {"error": "Screen manager not initialized"}
    if hasattr(mgr, "_safety"):
        mgr._safety.kill()
    await mgr.stop()
    return {"status": "killed"}


@router.post("/resume")
async def screen_resume():
    """Resume automation after a kill switch."""
    mgr = _get_screen_manager()
    if not mgr:
        return {"error": "Screen manager not initialized"}
    if hasattr(mgr, "_safety"):
        mgr._safety.resume()
    return {"status": "resumed"}


@router.get("/audit")
async def screen_audit(last_n: int = 50):
    """Return the recent action audit log."""
    mgr = _get_screen_manager()
    if not mgr or not hasattr(mgr, "_safety"):
        return {"entries": []}
    return {"entries": mgr._safety.get_audit_log(last_n)}
