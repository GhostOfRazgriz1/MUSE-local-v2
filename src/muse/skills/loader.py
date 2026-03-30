"""SkillLoader — install, uninstall, and manage MUSE skills."""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from muse.skills.manifest import SkillManifest

logger = logging.getLogger(__name__)

# Validation patterns
_PERMISSION_RE = re.compile(r"^\w+:\w+$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+")


class SkillLoader:
    """Manages skill installation, updates, and removal."""

    def __init__(self, db: aiosqlite.Connection, skills_dir: Path) -> None:
        self._db = db
        self._skills_dir = skills_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def install(self, skill_path: Path) -> SkillManifest:
        """Install a skill from *skill_path*.

        Reads ``manifest.json`` from the skill directory, validates it,
        copies files into ``skills_dir/<skill_name>/``, and records the
        skill in the database.
        """
        manifest = self._read_and_validate_manifest(skill_path)
        skill_id = manifest.name

        dest = self._skills_dir / skill_id
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_path, dest)

        await self._upsert_skill_row(skill_id, manifest)
        logger.info("Installed skill %s v%s", skill_id, manifest.version)
        return manifest

    async def uninstall(self, skill_id: str) -> None:
        """Remove an installed skill."""
        dest = self._skills_dir / skill_id
        if dest.exists():
            shutil.rmtree(dest)
        await self._db.execute(
            "DELETE FROM installed_skills WHERE skill_id = ?", (skill_id,),
        )
        await self._db.commit()
        logger.info("Uninstalled skill %s", skill_id)

    async def get_installed(self) -> list[dict[str, Any]]:
        """Return a list of all installed skills with their manifests."""
        cursor = await self._db.execute(
            "SELECT skill_id, manifest_json, installed_at, updated_at FROM installed_skills",
        )
        rows = await cursor.fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            manifest_data = json.loads(row[1])
            results.append({
                "skill_id": row[0],
                "manifest": manifest_data,
                "installed_at": row[2],
                "updated_at": row[3],
            })
        return results

    async def get_manifest(self, skill_id: str) -> SkillManifest | None:
        """Return the manifest for an installed skill, or ``None``."""
        cursor = await self._db.execute(
            "SELECT manifest_json FROM installed_skills WHERE skill_id = ?",
            (skill_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return SkillManifest.from_json(json.loads(row[0]))

    async def update_skill(self, skill_id: str, skill_path: Path) -> SkillManifest:
        """Update an already-installed skill from a new source path."""
        manifest = self._read_and_validate_manifest(skill_path)
        if manifest.name != skill_id:
            raise ValueError(
                f"Manifest name '{manifest.name}' does not match skill_id '{skill_id}'",
            )

        dest = self._skills_dir / skill_id
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_path, dest)

        await self._upsert_skill_row(skill_id, manifest, is_update=True)
        logger.info("Updated skill %s to v%s", skill_id, manifest.version)
        return manifest

    async def load_first_party_skills(self, builtin_skills_dir: Path) -> None:
        """Scan *builtin_skills_dir* for built-in skills and install or update.

        Also removes any previously-installed first-party skills whose
        source directories no longer exist (e.g., deleted skills).
        """
        if not builtin_skills_dir.is_dir():
            logger.warning("Built-in skills directory not found: %s", builtin_skills_dir)
            return

        # Collect the set of skill names that exist in the source tree
        source_skills: set[str] = set()

        for child in sorted(builtin_skills_dir.iterdir()):
            manifest_file = child / "manifest.json"
            if not child.is_dir() or not manifest_file.exists():
                continue
            try:
                manifest = self._read_and_validate_manifest(child)
                source_skills.add(manifest.name)
                # Always re-install first-party skills so source edits take effect
                await self.install(child)
                logger.info("Loaded first-party skill %s", manifest.name)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to load built-in skill from %s: %s", child, exc)

        # Clean up first-party skills that no longer exist in source
        installed = await self.get_installed()
        for skill in installed:
            manifest = skill.get("manifest", {})
            if manifest.get("is_first_party") and skill["skill_id"] not in source_skills:
                logger.info("Removing stale first-party skill: %s", skill["skill_id"])
                await self.uninstall(skill["skill_id"])

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _read_and_validate_manifest(self, skill_path: Path) -> SkillManifest:
        """Read and validate ``manifest.json`` from *skill_path*."""
        manifest_file = skill_path / "manifest.json"
        if not manifest_file.exists():
            raise FileNotFoundError(f"No manifest.json in {skill_path}")

        with open(manifest_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        self._validate_manifest_data(data)
        return SkillManifest.from_json(data)

    @staticmethod
    def _validate_manifest_data(data: dict) -> None:
        """Raise ``ValueError`` if the manifest data is invalid."""
        if not data.get("name"):
            raise ValueError("Manifest must include a non-empty 'name' field")

        version = data.get("version", "")
        if version and not _SEMVER_RE.match(version):
            raise ValueError(
                f"Version '{version}' is not semver-like (expected N.N.N...)",
            )

        for perm in data.get("permissions", []):
            if not _PERMISSION_RE.match(perm):
                raise ValueError(
                    f"Invalid permission format '{perm}' — expected 'word:word'",
                )

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    async def _upsert_skill_row(
        self,
        skill_id: str,
        manifest: SkillManifest,
        is_update: bool = False,
    ) -> None:
        """Insert or update the skill record in ``installed_skills``."""
        now = datetime.now(timezone.utc).isoformat()
        manifest_json = manifest.to_json()

        await self._db.execute(
            """
            INSERT INTO installed_skills (skill_id, manifest_json, installed_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(skill_id) DO UPDATE SET
                manifest_json = excluded.manifest_json,
                updated_at = excluded.updated_at
            """,
            (skill_id, manifest_json, now, now),
        )
        await self._db.commit()
