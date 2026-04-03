"""Screen manager — lifecycle and coordination for desktop vision.

Owns the ScreenCapture, FrameBuffer, and ActionExecutor.  Checks
whether a capable local vision model is available before enabling
screen features.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .capture import CaptureRegion, ScreenCapture
from .stream import FrameBuffer

if TYPE_CHECKING:
    from muse.providers.model_router import ModelRouter

logger = logging.getLogger(__name__)


class ScreenMode(str, Enum):
    """Operating mode for the screen vision system."""
    OFF = "off"
    PASSIVE = "passive"   # Observe only — provide visual context
    ACTIVE = "active"     # Observe + act — execute desktop actions


@dataclass
class ScreenConfig:
    """Runtime configuration for screen vision."""
    fps: float = 1.0
    max_frames: int = 60
    max_dimension: int = 1280
    monitor: int = 0
    region: CaptureRegion | None = None
    mode: ScreenMode = ScreenMode.OFF


class ScreenManager:
    """Central manager for the desktop vision pipeline.

    Responsible for:
    - Checking if screen capture dependencies are available
    - Checking if a local vision model is available
    - Managing the frame buffer lifecycle
    - Providing visual context for LLM calls
    """

    def __init__(
        self,
        model_router: ModelRouter | None = None,
        config: ScreenConfig | None = None,
    ) -> None:
        self._router = model_router
        self._config = config or ScreenConfig()
        self._capture: ScreenCapture | None = None
        self._buffer: FrameBuffer | None = None
        self._vision_model: str | None = None

    @property
    def mode(self) -> ScreenMode:
        return self._config.mode

    @property
    def is_streaming(self) -> bool:
        return self._buffer is not None and self._buffer.is_running

    @property
    def vision_model(self) -> str | None:
        return self._vision_model

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_capture_available(self) -> bool:
        """Check if screen capture dependencies (mss) are installed."""
        return ScreenCapture.is_available()

    async def is_vision_model_available(self) -> bool:
        """Check if a model with video/vision capability is reachable."""
        if not self._router:
            return False
        model = await self._router._find_capable_model(["vision"])
        if model:
            self._vision_model = model
            return True
        return False

    async def check_readiness(self) -> dict:
        """Return a status dict describing what's available.

        Useful for the frontend to know whether to show screen controls.
        """
        capture_ok = self.is_capture_available()
        vision_ok = await self.is_vision_model_available() if self._router else False
        return {
            "capture_available": capture_ok,
            "vision_model_available": vision_ok,
            "vision_model": self._vision_model,
            "mode": self._config.mode.value,
            "is_streaming": self.is_streaming,
            "fps": self._config.fps,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, mode: ScreenMode = ScreenMode.PASSIVE) -> None:
        """Start screen streaming in the given mode.

        Raises RuntimeError if dependencies or a vision model are missing.
        """
        if not self.is_capture_available():
            raise RuntimeError(
                "Screen capture unavailable — install 'mss' and 'Pillow': "
                "pip install mss Pillow"
            )
        if self._router and not await self.is_vision_model_available():
            raise RuntimeError(
                "No local vision model found. Start Ollama/vLLM/llama.cpp "
                "with a Gemma 4 model, then try again."
            )

        self._config.mode = mode
        self._capture = ScreenCapture(max_dimension=self._config.max_dimension)
        self._buffer = FrameBuffer(
            capture=self._capture,
            fps=self._config.fps,
            max_frames=self._config.max_frames,
            region=self._config.region,
            monitor=self._config.monitor,
        )
        await self._buffer.start()
        logger.info("Screen manager started in %s mode.", mode.value)

    async def stop(self) -> None:
        """Stop screen streaming and clean up."""
        if self._buffer:
            await self._buffer.stop()
            self._buffer = None
        self._capture = None
        self._config.mode = ScreenMode.OFF
        logger.info("Screen manager stopped.")

    # ------------------------------------------------------------------
    # Visual context for LLM calls
    # ------------------------------------------------------------------

    def get_visual_context(self, max_frames: int = 1) -> list[dict]:
        """Get recent frames as attachment dicts for context assembly.

        For passive mode, typically 1 frame (current screenshot).
        For active mode action loops, may use more frames for continuity.
        """
        if not self._buffer or not self._buffer.is_running:
            return []
        return self._buffer.get_video_context(max_frames=max_frames)

    def get_single_screenshot(self) -> dict | None:
        """Capture a single on-demand screenshot (not from the buffer).

        Useful for the action loop where we need a fresh frame after
        each action.
        """
        if not self._capture:
            return None
        try:
            b64 = self._capture.grab_frame_base64(
                region=self._config.region,
                monitor=self._config.monitor,
            )
            return {"type": "image_base64", "media_type": "image/png", "data": b64}
        except Exception:
            logger.debug("On-demand screenshot failed", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure(
        self,
        fps: float | None = None,
        max_frames: int | None = None,
        max_dimension: int | None = None,
        monitor: int | None = None,
        region: CaptureRegion | None | str = None,
    ) -> None:
        """Update screen configuration.  Changes take effect on next start."""
        if fps is not None:
            self._config.fps = max(0.1, min(fps, 10.0))
            if self._buffer:
                self._buffer.fps = self._config.fps
        if max_frames is not None:
            self._config.max_frames = max_frames
        if max_dimension is not None:
            self._config.max_dimension = max_dimension
        if monitor is not None:
            self._config.monitor = monitor
        if region == "clear":
            self._config.region = None
            if self._buffer:
                self._buffer.set_region(None)
        elif isinstance(region, CaptureRegion):
            self._config.region = region
            if self._buffer:
                self._buffer.set_region(region)
