"""WarmPool — pre-forked Python interpreter pool for fast skill startup."""
from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PooledProcess:
    """A reusable interpreter process managed by :class:`WarmPool`."""

    process: asyncio.subprocess.Process
    use_count: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


class WarmPool:
    """Pool of pre-forked Python interpreters for low-latency skill launch.

    Each process runs the ``muse.skills._bootstrap`` module, which
    imports the SDK and then blocks on stdin waiting for a work payload.
    """

    def __init__(
        self,
        pool_size: int = 4,
        max_reuse: int = 50,
        python_executable: str = sys.executable,
    ) -> None:
        self._pool_size = pool_size
        self._max_reuse = max_reuse
        self._python_executable = python_executable

        self._idle: asyncio.Queue[PooledProcess] = asyncio.Queue()
        self._all: list[PooledProcess] = []
        self._started = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Pre-fork *pool_size* interpreter processes."""
        if self._started:
            return
        self._started = True
        spawn_tasks = [self._spawn_process() for _ in range(self._pool_size)]
        processes = await asyncio.gather(*spawn_tasks, return_exceptions=True)
        for proc in processes:
            if isinstance(proc, PooledProcess):
                self._idle.put_nowait(proc)
                self._all.append(proc)
            else:
                logger.error("Failed to pre-fork process: %s", proc)
        logger.info("WarmPool started with %d processes", self._idle.qsize())

    async def stop(self) -> None:
        """Kill all pooled processes (idle and in-use)."""
        self._started = False
        for pp in self._all:
            try:
                pp.process.kill()
            except ProcessLookupError:
                pass
        # Drain the idle queue
        while not self._idle.empty():
            try:
                self._idle.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._all.clear()
        logger.info("WarmPool stopped")

    # ------------------------------------------------------------------
    # Checkout / return
    # ------------------------------------------------------------------

    async def checkout(self) -> PooledProcess:
        """Get an idle process from the pool.

        If the pool is empty, a cold process is spawned (slower, but
        callers are never blocked indefinitely).
        """
        try:
            pp = self._idle.get_nowait()
        except asyncio.QueueEmpty:
            logger.debug("Pool exhausted — cold-spawning a process")
            pp = await self._spawn_process()
            async with self._lock:
                self._all.append(pp)

        pp.use_count += 1
        return pp

    async def return_process(self, process: PooledProcess) -> None:
        """Return a process to the pool, or kill it if max reuse exceeded."""
        if process.use_count >= self._max_reuse:
            logger.debug(
                "Process %s reached max reuse (%d) — killing",
                process.id, self._max_reuse,
            )
            await self._kill_and_replace(process)
            return

        # Check the process is still alive
        if process.process.returncode is not None:
            logger.debug("Process %s already exited — replacing", process.id)
            await self._kill_and_replace(process)
            return

        self._idle.put_nowait(process)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _spawn_process(self) -> PooledProcess:
        """Spawn a single bootstrap interpreter process."""
        import os
        safe_env = {
            k: v for k, v in os.environ.items()
            if k in ("PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP",
                     "USERPROFILE", "COMSPEC", "LANG", "LC_ALL")
        }
        proc = await asyncio.create_subprocess_exec(
            self._python_executable, "-m", "muse.skills._bootstrap",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )
        pp = PooledProcess(process=proc)
        logger.debug("Spawned warm process %s (pid %s)", pp.id, proc.pid)
        return pp

    async def _kill_and_replace(self, process: PooledProcess) -> None:
        """Kill *process*, remove from tracking, and spawn a replacement."""
        try:
            process.process.kill()
        except ProcessLookupError:
            pass

        async with self._lock:
            if process in self._all:
                self._all.remove(process)

        if self._started:
            try:
                replacement = await self._spawn_process()
                self._idle.put_nowait(replacement)
                async with self._lock:
                    self._all.append(replacement)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to spawn replacement process: %s", exc)
