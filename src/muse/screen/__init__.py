"""Desktop screen capture and interaction for MUSE vision features."""

from .capture import ScreenCapture
from .stream import FrameBuffer
from .manager import ScreenManager
from .actions import ActionExecutor, ActionResult
from .safety import SafetyGuard, SafetyConfig, SafetyViolation

__all__ = [
    "ScreenCapture",
    "FrameBuffer",
    "ScreenManager",
    "ActionExecutor",
    "ActionResult",
    "SafetyGuard",
    "SafetyConfig",
    "SafetyViolation",
]
