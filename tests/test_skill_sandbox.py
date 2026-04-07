"""Tests for SkillSandbox validation logic."""
from __future__ import annotations

import re
import pytest
from pathlib import Path

from muse.skills.sandbox import _validate_entry_point, _SAFE_SKILL_ID


# ── Entry point validation ─────────────────────────────────────

def test_valid_entry_point(tmp_path):
    (tmp_path / "skill.py").touch()
    result = _validate_entry_point(tmp_path, "skill.py")
    assert result.name == "skill.py"


def test_entry_point_rejects_directory_separator(tmp_path):
    with pytest.raises(ValueError, match="simple .py filename"):
        _validate_entry_point(tmp_path, "sub/evil.py")


def test_entry_point_rejects_dotdot(tmp_path):
    with pytest.raises(ValueError, match="simple .py filename"):
        _validate_entry_point(tmp_path, "../evil.py")


def test_entry_point_rejects_backslash(tmp_path):
    with pytest.raises(ValueError, match="simple .py filename"):
        _validate_entry_point(tmp_path, "..\\evil.py")


def test_entry_point_rejects_hidden_file(tmp_path):
    with pytest.raises(ValueError, match="simple .py filename"):
        _validate_entry_point(tmp_path, ".hidden.py")


def test_entry_point_accepts_underscored_name(tmp_path):
    (tmp_path / "my_skill.py").touch()
    result = _validate_entry_point(tmp_path, "my_skill.py")
    assert result.name == "my_skill.py"


# ── Skill ID validation ───────────────────────────────────────

def test_skill_id_valid():
    assert _SAFE_SKILL_ID.match("Search")
    assert _SAFE_SKILL_ID.match("Code Runner")
    assert _SAFE_SKILL_ID.match("my-skill_v2")


def test_skill_id_rejects_path_separator():
    assert _SAFE_SKILL_ID.match("../../etc") is None


def test_skill_id_rejects_slash():
    assert _SAFE_SKILL_ID.match("path/to/skill") is None


def test_skill_id_rejects_empty():
    assert _SAFE_SKILL_ID.match("") is None


def test_skill_id_rejects_leading_space():
    assert _SAFE_SKILL_ID.match(" LeadingSpace") is None


def test_skill_id_rejects_dot_start():
    assert _SAFE_SKILL_ID.match(".hidden") is None
