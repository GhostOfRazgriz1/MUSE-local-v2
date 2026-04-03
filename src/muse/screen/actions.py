"""Action executor — translates structured LLM actions into desktop interactions.

Parses JSON action dicts from Gemma 4 and executes them via ``pyautogui``.
Each action produces a result with a fresh screenshot so the LLM can
verify the outcome and decide the next step.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

try:
    import pyautogui
    pyautogui.FAILSAFE = True  # Move mouse to corner to abort
    pyautogui.PAUSE = 0.3     # Brief pause between actions
    _PYAUTOGUI_AVAILABLE = True
except ImportError:
    _PYAUTOGUI_AVAILABLE = False


class ActionType(str, Enum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE = "type"
    HOTKEY = "hotkey"
    SCROLL = "scroll"
    MOVE = "move"
    DRAG = "drag"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    DONE = "done"


@dataclass
class ActionResult:
    """Result of executing a single action."""
    action_type: str
    success: bool
    details: str = ""
    screenshot_base64: str | None = None  # Fresh screenshot after action
    timestamp: float = field(default_factory=time.time)


# Actions that modify external state and should require confirmation
_DESTRUCTIVE_ACTIONS = {
    "hotkey": {
        ("ctrl", "enter"), ("return",), ("enter",),
        ("ctrl", "s"), ("ctrl", "shift", "s"),
        ("delete",), ("ctrl", "d"),
        ("alt", "f4"), ("ctrl", "w"),
    },
}


class ActionExecutor:
    """Executes desktop actions from structured LLM output.

    Typical usage in an action loop::

        executor = ActionExecutor()
        result = await executor.execute({"action": "click", "x": 100, "y": 200})
    """

    def __init__(self) -> None:
        if not _PYAUTOGUI_AVAILABLE:
            raise RuntimeError(
                "Desktop actions require 'pyautogui'. "
                "Install it with: pip install pyautogui"
            )

    async def execute(self, action: dict[str, Any]) -> ActionResult:
        """Execute a single action dict.

        The action format matches Gemma 4's structured output:

        .. code-block:: json

            {"action": "click", "x": 450, "y": 312}
            {"action": "type", "text": "hello"}
            {"action": "hotkey", "keys": ["ctrl", "c"]}
            {"action": "scroll", "direction": "down", "amount": 3}
            {"action": "move", "x": 100, "y": 200}
            {"action": "drag", "start_x": 0, "start_y": 0, "end_x": 100, "end_y": 100}
            {"action": "wait", "seconds": 1.0}
            {"action": "done", "summary": "Task completed"}
        """
        action_type = action.get("action", "").lower()

        try:
            handler = getattr(self, f"_do_{action_type}", None)
            if handler is None:
                return ActionResult(
                    action_type=action_type,
                    success=False,
                    details=f"Unknown action type: {action_type}",
                )
            return await handler(action)
        except Exception as exc:
            logger.error("Action %s failed: %s", action_type, exc, exc_info=True)
            return ActionResult(
                action_type=action_type,
                success=False,
                details=f"Error: {exc}",
            )

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    async def _do_click(self, action: dict) -> ActionResult:
        x, y = int(action["x"]), int(action["y"])
        button = action.get("button", "left")
        await asyncio.to_thread(pyautogui.click, x, y, button=button)
        return ActionResult(
            action_type="click",
            success=True,
            details=f"Clicked ({x}, {y}) button={button}",
        )

    async def _do_double_click(self, action: dict) -> ActionResult:
        x, y = int(action["x"]), int(action["y"])
        await asyncio.to_thread(pyautogui.doubleClick, x, y)
        return ActionResult(
            action_type="double_click",
            success=True,
            details=f"Double-clicked ({x}, {y})",
        )

    async def _do_right_click(self, action: dict) -> ActionResult:
        x, y = int(action["x"]), int(action["y"])
        await asyncio.to_thread(pyautogui.rightClick, x, y)
        return ActionResult(
            action_type="right_click",
            success=True,
            details=f"Right-clicked ({x}, {y})",
        )

    async def _do_type(self, action: dict) -> ActionResult:
        text = action.get("text", "")
        interval = action.get("interval", 0.02)
        await asyncio.to_thread(pyautogui.typewrite, text, interval=interval)
        return ActionResult(
            action_type="type",
            success=True,
            details=f"Typed {len(text)} chars",
        )

    async def _do_hotkey(self, action: dict) -> ActionResult:
        keys = action.get("keys", [])
        if not keys:
            return ActionResult(action_type="hotkey", success=False, details="No keys specified")
        await asyncio.to_thread(pyautogui.hotkey, *keys)
        return ActionResult(
            action_type="hotkey",
            success=True,
            details=f"Pressed {'+'.join(keys)}",
        )

    async def _do_scroll(self, action: dict) -> ActionResult:
        direction = action.get("direction", "down")
        amount = int(action.get("amount", 3))
        clicks = -amount if direction == "down" else amount
        x = action.get("x")
        y = action.get("y")
        if x is not None and y is not None:
            await asyncio.to_thread(pyautogui.scroll, clicks, int(x), int(y))
        else:
            await asyncio.to_thread(pyautogui.scroll, clicks)
        return ActionResult(
            action_type="scroll",
            success=True,
            details=f"Scrolled {direction} by {amount}",
        )

    async def _do_move(self, action: dict) -> ActionResult:
        x, y = int(action["x"]), int(action["y"])
        duration = float(action.get("duration", 0.3))
        await asyncio.to_thread(pyautogui.moveTo, x, y, duration=duration)
        return ActionResult(
            action_type="move",
            success=True,
            details=f"Moved to ({x}, {y})",
        )

    async def _do_drag(self, action: dict) -> ActionResult:
        sx, sy = int(action["start_x"]), int(action["start_y"])
        ex, ey = int(action["end_x"]), int(action["end_y"])
        duration = float(action.get("duration", 0.5))
        await asyncio.to_thread(pyautogui.moveTo, sx, sy)
        await asyncio.to_thread(pyautogui.drag, ex - sx, ey - sy, duration=duration)
        return ActionResult(
            action_type="drag",
            success=True,
            details=f"Dragged ({sx},{sy}) -> ({ex},{ey})",
        )

    async def _do_wait(self, action: dict) -> ActionResult:
        seconds = min(float(action.get("seconds", 1.0)), 10.0)
        await asyncio.sleep(seconds)
        return ActionResult(
            action_type="wait",
            success=True,
            details=f"Waited {seconds:.1f}s",
        )

    async def _do_screenshot(self, _action: dict) -> ActionResult:
        return ActionResult(
            action_type="screenshot",
            success=True,
            details="Screenshot requested (handled by caller)",
        )

    async def _do_done(self, action: dict) -> ActionResult:
        summary = action.get("summary", "Task completed")
        return ActionResult(
            action_type="done",
            success=True,
            details=summary,
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def is_available() -> bool:
        return _PYAUTOGUI_AVAILABLE

    @staticmethod
    def needs_confirmation(action: dict) -> bool:
        """Check if an action should require user confirmation."""
        action_type = action.get("action", "").lower()
        if action_type == "hotkey":
            keys = tuple(k.lower() for k in action.get("keys", []))
            return keys in _DESTRUCTIVE_ACTIONS.get("hotkey", set())
        return False
