from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from .registry import ProviderRegistry

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOW = 128_000


class ModelRouter:
    """Resolves which model to use for a given skill or task.

    Resolution priority (highest first):
      1. Explicit task_override passed at call-time
      2. Per-skill override stored in the model_overrides table
      3. The default_model configured at init
    """

    def __init__(
        self,
        provider: ProviderRegistry,
        db: aiosqlite.Connection,
        default_model: str,
    ) -> None:
        self._provider = provider
        self._db = db
        self.default_model = default_model

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    async def resolve_model(
        self,
        skill_id: str | None = None,
        task_override: str | None = None,
    ) -> str:
        """Return the model ID to use, following the priority chain."""
        if task_override:
            return task_override

        if skill_id:
            row = await self._db.execute(
                "SELECT model_id FROM model_overrides WHERE skill_id = ?",
                (skill_id,),
            )
            result = await row.fetchone()
            if result:
                return result[0]

        return self.default_model

    # ------------------------------------------------------------------
    # Per-skill overrides
    # ------------------------------------------------------------------

    async def set_skill_override(self, skill_id: str, model_id: str) -> None:
        """Create or update a per-skill model override."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """
            INSERT INTO model_overrides (skill_id, model_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(skill_id) DO UPDATE SET model_id = excluded.model_id,
                                                updated_at = excluded.updated_at
            """,
            (skill_id, model_id, now),
        )
        await self._db.commit()
        logger.info("Set model override for skill %s -> %s", skill_id, model_id)

    async def remove_skill_override(self, skill_id: str) -> None:
        """Remove a per-skill model override."""
        await self._db.execute(
            "DELETE FROM model_overrides WHERE skill_id = ?",
            (skill_id,),
        )
        await self._db.commit()
        logger.info("Removed model override for skill %s", skill_id)

    async def get_skill_overrides(self) -> dict[str, str]:
        """Return all per-skill model overrides as {skill_id: model_id}."""
        cursor = await self._db.execute("SELECT skill_id, model_id FROM model_overrides")
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------
    # Model metadata
    # ------------------------------------------------------------------

    async def get_context_window(self, model_id: str) -> int:
        """Return the context window size for a model (default 128 000)."""
        try:
            info = await self._provider.get_model_info(model_id)
            if info is not None and info.context_window > 0:
                return info.context_window
        except Exception:
            logger.debug("Could not fetch model info for %s, using default", model_id)
        return DEFAULT_CONTEXT_WINDOW
