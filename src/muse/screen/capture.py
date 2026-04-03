"""Low-level screen capture using the ``mss`` library.

Grabs screenshots as PNG bytes or base64 strings.  Supports full-screen
and region-based capture.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

try:
    import mss
    import mss.tools
    _MSS_AVAILABLE = True
except ImportError:
    _MSS_AVAILABLE = False

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


@dataclass(frozen=True)
class CaptureRegion:
    """A rectangular region of the screen to capture."""
    left: int
    top: int
    width: int
    height: int

    def to_mss_monitor(self) -> dict:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


# Default downscale resolution for frames sent to the LLM.
# Gemma 4 handles variable resolution but smaller frames reduce
# latency and memory when streaming at 1fps.
DEFAULT_MAX_DIMENSION = 1280


class ScreenCapture:
    """Captures screenshots from the desktop.

    Uses ``mss`` for fast cross-platform screen capture and ``Pillow``
    for optional downscaling.
    """

    def __init__(self, max_dimension: int = DEFAULT_MAX_DIMENSION) -> None:
        if not _MSS_AVAILABLE:
            raise RuntimeError(
                "Screen capture requires the 'mss' package. "
                "Install it with: pip install mss"
            )
        self._max_dim = max_dimension

    def grab_frame(
        self,
        region: CaptureRegion | None = None,
        monitor: int = 0,
    ) -> bytes:
        """Capture a single frame as PNG bytes.

        Args:
            region: Optional sub-region.  If *None*, captures the full
                    monitor specified by *monitor* (0 = all monitors
                    combined, 1 = primary, etc.).
            monitor: Monitor index when *region* is not provided.

        Returns:
            PNG image bytes (possibly downscaled).
        """
        with mss.mss() as sct:
            if region:
                raw = sct.grab(region.to_mss_monitor())
            else:
                raw = sct.grab(sct.monitors[monitor])

            png_bytes = mss.tools.to_png(raw.rgb, raw.size)

        # Downscale if needed to keep frames lightweight
        if _PIL_AVAILABLE and self._max_dim:
            png_bytes = self._downscale(png_bytes)

        return png_bytes

    def grab_frame_base64(
        self,
        region: CaptureRegion | None = None,
        monitor: int = 0,
    ) -> str:
        """Capture a single frame as a base64-encoded PNG string."""
        raw = self.grab_frame(region=region, monitor=monitor)
        return base64.b64encode(raw).decode("ascii")

    def _downscale(self, png_bytes: bytes) -> bytes:
        """Downscale an image so its longest side ≤ max_dimension."""
        img = Image.open(io.BytesIO(png_bytes))
        w, h = img.size
        if max(w, h) <= self._max_dim:
            return png_bytes

        scale = self._max_dim / max(w, h)
        new_size = (int(w * scale), int(h * scale))
        img = img.resize(new_size, Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    @staticmethod
    def is_available() -> bool:
        """Check if screen capture dependencies are installed."""
        return _MSS_AVAILABLE
