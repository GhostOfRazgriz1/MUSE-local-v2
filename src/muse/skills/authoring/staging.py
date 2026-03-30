"""Staging — manage temporary directories for skills under review."""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from muse.skills.manifest import SkillManifest

logger = logging.getLogger(__name__)


class StagingArea:
    """Manages a staging directory where generated skills live until they
    pass audit and get installed (or are rejected and cleaned up)."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir / "_staging"
        self._base.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, skill_name: str) -> Path:
        """Create a clean staging slot for *skill_name*.

        If a previous staging slot exists it is wiped first.
        Returns the directory path.
        """
        slot = self._base / skill_name
        if slot.exists():
            shutil.rmtree(slot)
        slot.mkdir(parents=True)
        logger.info("Created staging slot: %s", slot)
        return slot

    def write_skill(
        self,
        skill_name: str,
        code: str,
        manifest: dict[str, Any],
    ) -> Path:
        """Write skill.py and manifest.json into the staging slot.

        Creates the slot if it doesn't already exist.
        Returns the staging directory path.
        """
        slot = self._base / skill_name
        if not slot.exists():
            slot = self.create(skill_name)

        (slot / "skill.py").write_text(code, encoding="utf-8")
        (slot / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8",
        )

        # Write a small metadata sidecar for traceability
        meta = {
            "staged_at": datetime.now(timezone.utc).isoformat(),
            "skill_name": skill_name,
            "status": "pending_audit",
        }
        (slot / ".staging_meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8",
        )

        logger.info("Wrote skill files to staging: %s", slot)
        return slot

    def read_skill(self, skill_name: str) -> tuple[str, dict[str, Any]]:
        """Read skill.py and manifest.json from a staging slot.

        Returns (code, manifest_dict).
        Raises FileNotFoundError if the slot or files are missing.
        """
        slot = self._base / skill_name
        code_path = slot / "skill.py"
        manifest_path = slot / "manifest.json"

        if not code_path.exists():
            raise FileNotFoundError(f"No staged skill.py for '{skill_name}'")
        if not manifest_path.exists():
            raise FileNotFoundError(f"No staged manifest.json for '{skill_name}'")

        code = code_path.read_text(encoding="utf-8")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return code, manifest

    def get_path(self, skill_name: str) -> Path:
        """Return the staging directory for *skill_name*.

        Raises FileNotFoundError if no slot exists.
        """
        slot = self._base / skill_name
        if not slot.exists():
            raise FileNotFoundError(f"No staging slot for '{skill_name}'")
        return slot

    def remove(self, skill_name: str) -> None:
        """Delete the staging slot for *skill_name* (cleanup after
        install or rejection)."""
        slot = self._base / skill_name
        if slot.exists():
            shutil.rmtree(slot)
            logger.info("Removed staging slot: %s", slot)

    def list_staged(self) -> list[dict[str, Any]]:
        """Return metadata for every skill currently in staging."""
        results: list[dict[str, Any]] = []
        if not self._base.exists():
            return results

        for slot in sorted(self._base.iterdir()):
            if not slot.is_dir() or slot.name.startswith("."):
                continue
            meta_path = slot / ".staging_meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            else:
                meta = {"skill_name": slot.name, "status": "unknown"}
            meta["path"] = str(slot)
            results.append(meta)

        return results

    def update_status(self, skill_name: str, status: str) -> None:
        """Update the staging metadata status field."""
        slot = self._base / skill_name
        meta_path = slot / ".staging_meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        else:
            meta = {"skill_name": skill_name}
        meta["status"] = status
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
