"""Tests for SkillLoader (installation & validation)."""
from __future__ import annotations

import json
import pytest
import pytest_asyncio
from pathlib import Path

from muse.skills.loader import SkillLoader


@pytest_asyncio.fixture
async def skill_loader(agent_db, temp_dir):
    skills_dir = temp_dir / "installed_skills"
    skills_dir.mkdir()
    return SkillLoader(agent_db, skills_dir)


def _make_skill(tmp_dir: Path, name: str, **overrides) -> Path:
    """Create a minimal skill directory with manifest.json and skill.py."""
    skill_dir = tmp_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": name,
        "version": "1.0.0",
        "description": f"Test skill {name}",
        "author": "test",
        "permissions": ["memory:read"],
        "entry_point": "skill.py",
        "is_first_party": False,
        **overrides,
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "skill.py").write_text(
        "async def run(ctx):\n    return {'payload': None, 'summary': 'ok', 'success': True}\n",
        encoding="utf-8",
    )
    return skill_dir


# ── Install valid skill ────────────────────────────────────────

@pytest.mark.asyncio
async def test_install_valid_skill(skill_loader, temp_dir):
    skill_dir = _make_skill(temp_dir, "TestSkill")
    manifest = await skill_loader.install(skill_dir)
    assert manifest.name == "TestSkill"
    assert manifest.version == "1.0.0"

    installed = await skill_loader.get_installed()
    assert any(s["skill_id"] == "TestSkill" for s in installed)


@pytest.mark.asyncio
async def test_get_manifest_cached(skill_loader, temp_dir):
    skill_dir = _make_skill(temp_dir, "CachedSkill")
    await skill_loader.install(skill_dir)

    m1 = await skill_loader.get_manifest("CachedSkill")
    m2 = await skill_loader.get_manifest("CachedSkill")
    assert m1 is m2  # Same object from cache


# ── Validation failures ───────────────────────────────────────

@pytest.mark.asyncio
async def test_install_missing_manifest_raises(skill_loader, temp_dir):
    no_manifest = temp_dir / "empty_skill"
    no_manifest.mkdir()
    with pytest.raises(FileNotFoundError):
        await skill_loader.install(no_manifest)


@pytest.mark.asyncio
async def test_install_invalid_permission_format(skill_loader, temp_dir):
    skill_dir = _make_skill(temp_dir, "BadPerm", permissions=["badpermission"])
    with pytest.raises(ValueError, match="permission format"):
        await skill_loader.install(skill_dir)


@pytest.mark.asyncio
async def test_install_path_traversal_entry_point(skill_loader, temp_dir):
    skill_dir = _make_skill(temp_dir, "EvilSkill", entry_point="../evil.py")
    with pytest.raises(ValueError, match="entry_point"):
        await skill_loader.install(skill_dir)


@pytest.mark.asyncio
async def test_install_directory_separator_entry_point(skill_loader, temp_dir):
    skill_dir = _make_skill(temp_dir, "EvilSkill2", entry_point="sub/evil.py")
    with pytest.raises(ValueError, match="entry_point"):
        await skill_loader.install(skill_dir)


@pytest.mark.asyncio
async def test_install_invalid_version(skill_loader, temp_dir):
    skill_dir = _make_skill(temp_dir, "BadVer", version="not_semver")
    with pytest.raises(ValueError, match="semver"):
        await skill_loader.install(skill_dir)


# ── Uninstall ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_uninstall_removes_files_and_db(skill_loader, temp_dir):
    skill_dir = _make_skill(temp_dir, "UninstallMe")
    await skill_loader.install(skill_dir)
    await skill_loader.uninstall("UninstallMe")

    installed = await skill_loader.get_installed()
    assert not any(s["skill_id"] == "UninstallMe" for s in installed)
    assert not (skill_loader._skills_dir / "UninstallMe").exists()


# ── Update ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_skill_replaces_files(skill_loader, temp_dir):
    skill_dir = _make_skill(temp_dir, "Updatable")
    await skill_loader.install(skill_dir)

    # Update with v2
    skill_dir_v2 = _make_skill(temp_dir / "v2", "Updatable", version="2.0.0")
    manifest = await skill_loader.update_skill("Updatable", skill_dir_v2)
    assert manifest.version == "2.0.0"


@pytest.mark.asyncio
async def test_update_wrong_name_raises(skill_loader, temp_dir):
    skill_dir = _make_skill(temp_dir, "OriginalName")
    await skill_loader.install(skill_dir)

    skill_dir_wrong = _make_skill(temp_dir / "wrong", "WrongName")
    with pytest.raises(ValueError, match="does not match"):
        await skill_loader.update_skill("OriginalName", skill_dir_wrong)
