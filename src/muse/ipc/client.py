"""IPC client (skill side, part of the SDK).

Skills use this to communicate with the orchestrator over the same
transport that :mod:`muse.ipc.server` creates:

* **Windows** -- named pipe ``\\\\.\\pipe\\muse-{task_id}`` (with
  TCP-loopback fallback via an ``.addr`` file).
* **Unix** -- Unix domain socket at ``{ipc_dir}/{task_id}.sock``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from muse.ipc.protocol import (
    MessageType,
    deserialize_message,
    serialize_message,
)

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


class IPCClient:
    """Skill-side IPC client that connects to the orchestrator's channel."""

    def __init__(self, task_id: str, ipc_dir: str = "") -> None:
        self.task_id = task_id
        self._ipc_dir = Path(ipc_dir) if ipc_dir else Path.cwd() / ".muse" / "ipc"
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the orchestrator's IPC endpoint for this task."""
        if self._connected:
            raise RuntimeError("Already connected")

        if IS_WINDOWS:
            await self._connect_windows()
        else:
            await self._connect_unix()

        self._connected = True
        logger.info("IPC client connected for task %s", self.task_id)

    async def _connect_windows(self) -> None:
        """Connect via Windows named pipe, falling back to TCP loopback."""
        pipe_name = rf"\\.\pipe\muse-{self.task_id}"
        try:
            self._reader, self._writer = await asyncio.open_connection(
                pipe=pipe_name  # type: ignore[arg-type]
            )
            logger.debug("IPC client connected via named pipe %s", pipe_name)
            return
        except (TypeError, OSError, FileNotFoundError):
            pass

        # Fallback: read the address file written by the server.
        addr_file = self._ipc_dir / f"{self.task_id}.addr"
        if not addr_file.exists():
            raise ConnectionError(
                f"Cannot find IPC endpoint for task {self.task_id}: "
                f"no named pipe and no address file at {addr_file}"
            )

        addr_text = addr_file.read_text().strip()
        host, port_str = addr_text.rsplit(":", 1)
        port = int(port_str)
        self._reader, self._writer = await asyncio.open_connection(host, port)
        logger.debug(
            "IPC client connected via TCP fallback %s:%d", host, port
        )

    async def _connect_unix(self) -> None:
        """Connect via Unix domain socket."""
        sock_path = self._ipc_dir / f"{self.task_id}.sock"
        if not sock_path.exists():
            raise ConnectionError(
                f"IPC socket not found for task {self.task_id}: {sock_path}"
            )
        self._reader, self._writer = await asyncio.open_unix_connection(
            str(sock_path)
        )
        logger.debug("IPC client connected via Unix socket %s", sock_path)

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    async def send(self, message: MessageType) -> None:
        """Serialise and write a message as a single NDJSON line."""
        if not self._connected or self._writer is None:
            raise RuntimeError("Not connected")
        line = serialize_message(message) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()
        logger.debug("IPC client send [%s]: %s", self.task_id, line.rstrip())

    async def receive(self) -> MessageType:
        """Read one NDJSON line and return the deserialised message.

        Raises :class:`EOFError` when the orchestrator closes the
        connection.
        """
        if not self._connected or self._reader is None:
            raise RuntimeError("Not connected")
        # Limit message size to 10 MB to prevent OOM from malicious input
        try:
            raw = await self._reader.readuntil(b"\n")
        except asyncio.LimitOverrunError:
            raise ValueError("IPC message exceeds size limit")
        if not raw:
            raise EOFError(
                f"IPC client for task {self.task_id}: orchestrator closed connection"
            )
        line = raw.decode("utf-8").strip()
        logger.debug("IPC client recv [%s]: %s", self.task_id, line)
        return deserialize_message(line)

    async def receive_with_timeout(self, timeout: float) -> MessageType | None:
        """Like :meth:`receive` but returns ``None`` on timeout."""
        try:
            return await asyncio.wait_for(self.receive(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the connection to the orchestrator."""
        if not self._connected:
            return
        self._connected = False
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                logger.debug(
                    "IPC client close [%s]: ignoring error during close",
                    self.task_id,
                )
        self._reader = None
        self._writer = None
        logger.debug("IPC client closed [%s]", self.task_id)
