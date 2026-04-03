"""Frame buffer — captures and stores recent screen frames for LLM context.

Runs an async capture loop at a configurable FPS (default 1) and keeps
a circular buffer of the most recent frames.  Gemma 4 supports up to
60 seconds of video at 1fps, so the default buffer size is 60.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .capture import CaptureRegion, ScreenCapture

logger = logging.getLogger(__name__)


@dataclass
class TimestampedFrame:
    """A captured frame with its capture timestamp."""
    data_base64: str
    captured_at: float  # time.monotonic()


class FrameBuffer:
    """Circular buffer of screen captures, fed by an async capture loop.

    Usage::

        buf = FrameBuffer(capture=ScreenCapture())
        await buf.start()
        # ... later ...
        frames = buf.get_recent(10)  # last 10 frames
        await buf.stop()
    """

    def __init__(
        self,
        capture: ScreenCapture,
        fps: float = 1.0,
        max_frames: int = 60,
        region: CaptureRegion | None = None,
        monitor: int = 0,
    ) -> None:
        self._capture = capture
        self._fps = fps
        self._max_frames = max_frames
        self._region = region
        self._monitor = monitor
        self._frames: deque[TimestampedFrame] = deque(maxlen=max_frames)
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    @property
    def fps(self) -> float:
        return self._fps

    @fps.setter
    def fps(self, value: float) -> None:
        self._fps = max(0.1, min(value, 10.0))  # Clamp to 0.1–10 fps

    def set_region(self, region: CaptureRegion | None) -> None:
        """Update the capture region (None = full screen)."""
        self._region = region

    async def start(self) -> None:
        """Start the capture loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._capture_loop())
        logger.info(
            "Frame buffer started: %.1f fps, max %d frames",
            self._fps, self._max_frames,
        )

    async def stop(self) -> None:
        """Stop the capture loop and clear the buffer."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._frames.clear()
        logger.info("Frame buffer stopped.")

    def get_latest(self) -> str | None:
        """Return the most recent frame as base64, or None if empty."""
        if not self._frames:
            return None
        return self._frames[-1].data_base64

    def get_recent(self, n: int = 10) -> list[str]:
        """Return the *n* most recent frames as base64 strings."""
        frames = list(self._frames)
        return [f.data_base64 for f in frames[-n:]]

    def get_video_context(self, max_frames: int = 30) -> list[dict]:
        """Format recent frames as multimodal attachment dicts.

        Returns a list suitable for passing to
        ``AssembledContext.attachments``.
        """
        recent = self.get_recent(max_frames)
        return [
            {"type": "image_base64", "media_type": "image/png", "data": b64}
            for b64 in recent
        ]

    async def _capture_loop(self) -> None:
        """Background loop that captures frames at the configured FPS."""
        interval = 1.0 / self._fps
        while self._running:
            start = time.monotonic()
            try:
                # Run the synchronous capture in a thread to avoid
                # blocking the event loop.
                b64 = await asyncio.to_thread(
                    self._capture.grab_frame_base64,
                    region=self._region,
                    monitor=self._monitor,
                )
                self._frames.append(TimestampedFrame(
                    data_base64=b64,
                    captured_at=start,
                ))
            except Exception:
                logger.debug("Frame capture failed", exc_info=True)

            elapsed = time.monotonic() - start
            sleep_time = max(0, interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
