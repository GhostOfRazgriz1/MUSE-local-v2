"""IPC server (orchestrator side).

Creates one channel per task.  On **Windows** the transport is a named
pipe (``\\\\.\\pipe\\muse-{task_id}``).  On **Unix** the transport
is a Unix domain socket at ``{ipc_dir}/{task_id}.sock``.

Both transports use asyncio streams so that the higher-level
send/receive API is identical regardless of platform.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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


class IPCChannel:
    """Bidirectional message channel to a single skill process."""

    def __init__(
        self,
        task_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.task_id = task_id
        self._reader = reader
        self._writer = writer
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send(self, message: MessageType) -> None:
        """Serialise *message* and write it as a single NDJSON line."""
        if self._closed:
            raise RuntimeError(f"Channel for task {self.task_id} is closed")
        line = serialize_message(message) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()
        logger.debug("IPC send [%s]: %s", self.task_id, line.rstrip())

    async def receive(self) -> MessageType:
        """Read one NDJSON line and return the deserialised message.

        Raises :class:`EOFError` if the remote end closed the connection.
        """
        if self._closed:
            raise RuntimeError(f"Channel for task {self.task_id} is closed")
        try:
            raw = await self._reader.readuntil(b"\n")
        except asyncio.LimitOverrunError:
            raise ValueError("IPC message exceeds size limit")
        if not raw:
            raise EOFError(f"IPC channel for task {self.task_id}: remote closed")
        line = raw.decode("utf-8").strip()
        logger.debug("IPC recv [%s]: %s", self.task_id, line)
        return deserialize_message(line)

    async def receive_with_timeout(
        self, timeout: float
    ) -> MessageType | None:
        """Like :meth:`receive` but returns ``None`` on timeout."""
        try:
            return await asyncio.wait_for(self.receive(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def close(self) -> None:
        """Close the underlying transport and clean up."""
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            logger.debug("IPC close [%s]: ignoring error during close", self.task_id)
        logger.debug("IPC channel closed [%s]", self.task_id)


class IPCServer:
    """Orchestrator-side IPC server that creates per-task channels."""

    def __init__(self, ipc_dir: Path) -> None:
        self._ipc_dir = ipc_dir
        self._ipc_dir.mkdir(parents=True, exist_ok=True)
        # task_id -> (IPCChannel, cleanup callback)
        self._channels: dict[str, IPCChannel] = {}
        self._servers: dict[str, asyncio.AbstractServer] = {}

    # ------------------------------------------------------------------
    # Channel lifecycle
    # ------------------------------------------------------------------

    async def create_channel(self, task_id: str) -> IPCChannel:
        """Create and return an :class:`IPCChannel` for *task_id*.

        The method starts a one-shot server that waits for exactly one
        client connection (the skill process).  It returns as soon as
        that connection is established.
        """
        if task_id in self._channels:
            raise RuntimeError(f"Channel for task {task_id} already exists")

        connected: asyncio.Future[IPCChannel] = asyncio.get_event_loop().create_future()

        async def _on_connect(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            channel = IPCChannel(task_id, reader, writer)
            self._channels[task_id] = channel
            if not connected.done():
                connected.set_result(channel)

        if IS_WINDOWS:
            channel = await self._create_windows_channel(task_id, _on_connect, connected)
        else:
            channel = await self._create_unix_channel(task_id, _on_connect, connected)

        return channel

    # ------------------------------------------------------------------
    # Platform-specific helpers
    # ------------------------------------------------------------------

    async def _create_windows_channel(
        self,
        task_id: str,
        on_connect: Any,
        connected: asyncio.Future[IPCChannel],
    ) -> IPCChannel:
        """Create a channel using a Windows named pipe via proactor loop.

        asyncio on Windows with the proactor event loop supports
        ``asyncio.start_server`` on named pipes when the address is
        passed as the *path* argument (Python 3.12+) or we can fall back
        to a localhost TCP socket for broad compatibility.
        """
        pipe_name = rf"\\.\pipe\muse-{task_id}"
        try:
            # Python 3.12+ proactor loop supports named-pipe path.
            server = await asyncio.start_server(
                on_connect, path=pipe_name  # type: ignore[arg-type]
            )
        except (TypeError, OSError):
            # Fallback: use a TCP loopback socket and write an address
            # file so the client knows which port to connect to.
            server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
            addr = server.sockets[0].getsockname()
            addr_file = self._ipc_dir / f"{task_id}.addr"
            addr_file.write_text(f"127.0.0.1:{addr[1]}")
            logger.info(
                "IPC server for %s listening on tcp://127.0.0.1:%d (fallback)",
                task_id,
                addr[1],
            )

        self._servers[task_id] = server
        logger.info("IPC server for %s waiting for skill connection", task_id)
        channel = await connected
        return channel

    async def _create_unix_channel(
        self,
        task_id: str,
        on_connect: Any,
        connected: asyncio.Future[IPCChannel],
    ) -> IPCChannel:
        """Create a channel using a Unix domain socket."""
        sock_path = self._ipc_dir / f"{task_id}.sock"
        # Remove stale socket if present.
        if sock_path.exists():
            sock_path.unlink()

        server = await asyncio.start_unix_server(
            on_connect, path=str(sock_path)
        )
        self._servers[task_id] = server
        logger.info("IPC server for %s at %s", task_id, sock_path)
        channel = await connected
        return channel

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def close_channel(self, task_id: str) -> None:
        """Close and clean up the channel for *task_id*."""
        channel = self._channels.pop(task_id, None)
        if channel:
            await channel.close()

        server = self._servers.pop(task_id, None)
        if server:
            server.close()
            await server.wait_closed()

        # Remove socket / address file.
        for suffix in (".sock", ".addr"):
            p = self._ipc_dir / f"{task_id}{suffix}"
            if p.exists():
                p.unlink(missing_ok=True)

    async def close_all(self) -> None:
        """Shut down every open channel."""
        task_ids = list(self._channels.keys())
        for tid in task_ids:
            await self.close_channel(tid)
