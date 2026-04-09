"""Tests for planner_hint — manifest field, classifier wiring,
and planner catalog generation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "sdk"))

from muse.skills.manifest import SkillManifest
from muse.kernel.intent_classifier import SemanticIntentClassifier


# ── SkillManifest.planner_hint ─────────────────────────────────────────


class TestManifestPlannerHint:
    def test_default_is_empty(self):
        m = SkillManifest(name="test", version="1.0", description="d", author="a")
        assert m.planner_hint == ""

    def test_from_json_with_hint(self):
        data = {
            "name": "Webpage Reader",
            "version": "0.1.0",
            "description": "Read web pages",
            "author": "MUSE",
            "planner_hint": "REQUIRES a specific URL",
        }
        m = SkillManifest.from_json(data)
        assert m.planner_hint == "REQUIRES a specific URL"

    def test_from_json_without_hint(self):
        data = {
            "name": "Notify",
            "version": "0.1.0",
            "description": "Send notifications",
            "author": "MUSE",
        }
        m = SkillManifest.from_json(data)
        assert m.planner_hint == ""

    def test_real_manifest_files(self):
        """Verify the actual manifest.json files have planner_hint."""
        skills_dir = PROJECT_ROOT / "skills"
        for skill_id, expected_keyword in [
            ("webpage_reader", "URL"),
            ("search", "FIRST"),
            ("code_runner", "computation"),
        ]:
            manifest_path = skills_dir / skill_id / "manifest.json"
            with open(manifest_path) as f:
                m = SkillManifest.from_json(json.load(f))
            assert m.planner_hint, f"{skill_id} should have a planner_hint"
            assert expected_keyword in m.planner_hint, (
                f"{skill_id} planner_hint should mention '{expected_keyword}'"
            )


# ── Classifier: register_skill with planner_hint ──────────────────────


class TestClassifierPlannerHint:
    @pytest.fixture
    def classifier(self):
        return SemanticIntentClassifier()

    def test_register_with_hint(self, classifier):
        classifier.register_skill(
            "webpage_reader", "Webpage Reader", "Read web pages",
            planner_hint="REQUIRES a URL",
        )
        assert classifier._skills["webpage_reader"]["planner_hint"] == "REQUIRES a URL"

    def test_register_without_hint(self, classifier):
        classifier.register_skill("notify", "Notify", "Send notifications")
        assert classifier._skills["notify"]["planner_hint"] == ""

    def test_planner_catalog_includes_hints(self, classifier):
        classifier.register_skill(
            "search", "Search", "Search the web",
            planner_hint="Use first for web info",
        )
        classifier.register_skill(
            "webpage_reader", "Webpage Reader", "Read a webpage",
            planner_hint="REQUIRES a URL",
        )
        classifier.register_skill("notify", "Notify", "Send alerts")

        catalog = classifier.get_planner_catalog()

        # Skills with hints have CONSTRAINT lines
        assert "CONSTRAINT: Use first for web info" in catalog
        assert "CONSTRAINT: REQUIRES a URL" in catalog
        # Skills without hints don't have CONSTRAINT
        lines = catalog.split("\n")
        notify_lines = [l for l in lines if "notify" in l.lower() or "Send alerts" in l]
        for line in notify_lines:
            assert "CONSTRAINT" not in line

    def test_routing_catalog_excludes_hints(self, classifier):
        """_cached_skill_lines (used for routing) should NOT have hints."""
        classifier.register_skill(
            "search", "Search", "Search the web",
            planner_hint="Use first for web info",
        )
        assert "CONSTRAINT" not in classifier._cached_skill_lines
        assert "Use first" not in classifier._cached_skill_lines

    def test_planner_catalog_vs_routing_catalog(self, classifier):
        """Planner catalog is enriched; routing catalog is plain."""
        classifier.register_skill(
            "code_runner", "Code Runner", "Execute Python",
            planner_hint="ONLY for computation",
        )
        planner = classifier.get_planner_catalog()
        routing = classifier._cached_skill_lines

        assert len(planner) > len(routing)
        assert "CONSTRAINT" in planner
        assert "CONSTRAINT" not in routing
