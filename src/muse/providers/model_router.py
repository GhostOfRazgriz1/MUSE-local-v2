from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from .registry import ProviderRegistry

logger = logging.getLogger(__name__)

# Model IDs must match provider/model format or a simple name (local models).
# Rejects URLs, paths, and other injection attempts.
_VALID_MODEL_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,200}$")

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
        vision_model: str | None = None,
    ) -> None:
        self._provider = provider
        self._db = db
        self.default_model = default_model
        self._vision_model = vision_model
        # In-memory cache for per-skill model overrides (populated lazily).
        self._override_cache: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    async def resolve_model(
        self,
        skill_id: str | None = None,
        task_override: str | None = None,
        required_capabilities: list[str] | None = None,
    ) -> str:
        """Return the model ID to use, following the priority chain.

        When *required_capabilities* is provided (e.g. ``["video"]``),
        the router first checks the dedicated ``vision_model`` config,
        then scans all registered models for one that advertises the
        requested capabilities.  Falls back to the normal priority chain
        if no capable model is found.
        """
        if task_override:
            if not _VALID_MODEL_ID.match(task_override):
                logger.warning("Rejected invalid task_override model ID: %s", task_override[:50])
            else:
                return task_override

        # Capability-based routing (e.g. vision/video tasks)
        if required_capabilities:
            capable = await self._find_capable_model(required_capabilities)
            if capable:
                return capable

        if skill_id:
            if self._override_cache is None:
                await self._load_override_cache()
            override = self._override_cache.get(skill_id)  # type: ignore[union-attr]
            if override:
                return override

        return self.default_model

    async def _find_capable_model(self, capabilities: list[str]) -> str | None:
        """Find a model that supports all requested capabilities.

        Checks the explicit vision_model config first, then scans the
        provider registry for any model advertising the capabilities.
        """
        # 1. Explicit vision_model config takes priority
        if self._vision_model:
            try:
                info = await self._provider.get_model_info(self._vision_model)
                if info and all(c in info.capabilities for c in capabilities):
                    return self._vision_model
            except Exception:
                logger.debug("Vision model %s not available", self._vision_model)

        # 2. Scan all registered models
        try:
            all_models = await self._provider.list_models()
            for model in all_models:
                if all(c in model.capabilities for c in capabilities):
                    logger.info(
                        "Auto-selected model %s for capabilities %s",
                        model.id, capabilities,
                    )
                    return model.id
        except Exception:
            logger.debug("Failed to scan models for capabilities %s", capabilities)

        return None

    async def _load_override_cache(self) -> None:
        """Populate the override cache from the DB (once)."""
        cursor = await self._db.execute("SELECT skill_id, model_id FROM model_overrides")
        rows = await cursor.fetchall()
        self._override_cache = {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------
    # Per-skill overrides
    # ------------------------------------------------------------------

    async def set_skill_override(self, skill_id: str, model_id: str) -> None:
        """Create or update a per-skill model override."""
        if not _VALID_MODEL_ID.match(model_id):
            raise ValueError(f"Invalid model ID: {model_id!r}")
        if "/" not in model_id:
            logger.warning(
                "Model override '%s' has no provider prefix (expected 'provider/model'). "
                "It will be routed to the fallback provider.",
                model_id,
            )
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
        # Invalidate cache
        if self._override_cache is not None:
            self._override_cache[skill_id] = model_id
        logger.info("Set model override for skill %s -> %s", skill_id, model_id)

    async def remove_skill_override(self, skill_id: str) -> None:
        """Remove a per-skill model override."""
        await self._db.execute(
            "DELETE FROM model_overrides WHERE skill_id = ?",
            (skill_id,),
        )
        await self._db.commit()
        # Invalidate cache
        if self._override_cache is not None:
            self._override_cache.pop(skill_id, None)
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
