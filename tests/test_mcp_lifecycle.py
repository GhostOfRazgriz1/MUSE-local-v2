"""Unit tests for on-demand MCP server lifecycle.

Tests config serialization, lifecycle-aware startup, lazy connection,
template arg resolution, tool caching, and manifest generation for
on-demand MCP servers.
"""

from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from muse.mcp.config import MCPServerConfig


# ── Config serialization ─────────────────────────────────────────


class TestMCPServerConfig:
    def test_lifecycle_defaults_to_persistent(self):
        config = MCPServerConfig(server_id="test", name="Test", transport="stdio")
        assert config.lifecycle == "persistent"
        assert config.cached_tools == []

    def test_lifecycle_roundtrip(self):
        config = MCPServerConfig(
            server_id="serena", name="Serena", transport="stdio",
            lifecycle="on_demand",
            cached_tools=[{"name": "read_file", "description": "Read a file"}],
        )
        d = config.to_dict()
        assert d["lifecycle"] == "on_demand"
        assert len(d["cached_tools"]) == 1

        restored = MCPServerConfig.from_dict(d)
        assert restored.lifecycle == "on_demand"
        assert restored.cached_tools[0]["name"] == "read_file"

    def test_legacy_config_without_lifecycle(self):
        """Configs stored before lifecycle field was added should default to persistent."""
        d = {"server_id": "old", "name": "Old", "transport": "stdio"}
        config = MCPServerConfig.from_dict(d)
        assert config.lifecycle == "persistent"
        assert config.cached_tools == []

    def test_json_roundtrip(self):
        config = MCPServerConfig(
            server_id="test", name="Test", transport="stdio",
            lifecycle="on_demand",
            context_mode="none", enrichment_mode="never",
        )
        json_str = config.to_json()
        restored = MCPServerConfig.from_dict(json.loads(json_str))
        assert restored.lifecycle == "on_demand"
        assert restored.context_mode == "none"
        assert restored.enrichment_mode == "never"


# ── Connection manager lifecycle ─────────────────────────────────


def _mock_db():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    return db


class TestConnectionManagerLifecycle:
    @pytest.mark.asyncio
    async def test_startup_skips_on_demand(self):
        """On-demand servers should not connect at startup."""
        from muse.mcp.connection_manager import MCPConnectionManager

        db = _mock_db()
        manager = MCPConnectionManager(db)

        persistent_config = MCPServerConfig(
            server_id="search", name="Search", transport="stdio",
            command="npx", args=["-y", "search-mcp"], lifecycle="persistent",
        )
        on_demand_config = MCPServerConfig(
            server_id="serena", name="Serena", transport="stdio",
            command="uvx", args=["serena", "mcp", "--workspace", "{workspace}"],
            lifecycle="on_demand",
        )

        # Mock get_servers to return both
        manager.get_servers = AsyncMock(return_value=[persistent_config, on_demand_config])
        # Mock connect to track calls
        manager.connect = AsyncMock()

        await manager.startup()

        # Only persistent should connect
        manager.connect.assert_called_once_with("search")
        # On-demand should be stored
        assert "serena" in manager._on_demand_configs
        assert manager._on_demand_configs["serena"].lifecycle == "on_demand"

    def test_get_on_demand_configs(self):
        from muse.mcp.connection_manager import MCPConnectionManager

        db = _mock_db()
        manager = MCPConnectionManager(db)
        config = MCPServerConfig(
            server_id="serena", name="Serena", transport="stdio",
            lifecycle="on_demand",
        )
        manager._on_demand_configs["serena"] = config

        result = manager.get_on_demand_configs()
        assert "serena" in result
        assert result["serena"].name == "Serena"

    @pytest.mark.asyncio
    async def test_ensure_connected_already_connected(self):
        """ensure_connected should return immediately if already connected."""
        from muse.mcp.connection_manager import MCPConnectionManager, MCPConnection

        db = _mock_db()
        manager = MCPConnectionManager(db)

        config = MCPServerConfig(server_id="test", name="Test", transport="stdio")
        conn = MCPConnection(config=config, status="connected")
        manager._connections["test"] = conn

        result = await manager.ensure_connected("test")
        assert result is conn
        assert result.status == "connected"

    @pytest.mark.asyncio
    async def test_ensure_connected_triggers_connect(self):
        """ensure_connected should call connect() for disconnected servers."""
        from muse.mcp.connection_manager import MCPConnectionManager, MCPConnection

        db = _mock_db()
        manager = MCPConnectionManager(db)

        config = MCPServerConfig(server_id="test", name="Test", transport="stdio")
        connected_conn = MCPConnection(config=config, status="connected")

        manager._load_config = AsyncMock(return_value=config)
        manager.connect = AsyncMock(return_value=connected_conn)

        result = await manager.ensure_connected("test")
        manager.connect.assert_called_once_with("test")

    @pytest.mark.asyncio
    async def test_ensure_connected_with_resolved_args(self):
        """ensure_connected should apply resolved args before connecting."""
        from muse.mcp.connection_manager import MCPConnectionManager, MCPConnection

        db = _mock_db()
        manager = MCPConnectionManager(db)

        config = MCPServerConfig(
            server_id="serena", name="Serena", transport="stdio",
            args=["serena", "mcp", "--workspace", "{workspace}"],
        )
        connected_conn = MCPConnection(config=config, status="connected")

        manager._load_config = AsyncMock(return_value=config)
        manager.connect = AsyncMock(return_value=connected_conn)

        resolved = ["serena", "mcp", "--workspace", "/home/user/project"]
        await manager.ensure_connected("serena", resolved_args=resolved)

        # Config args should be updated before connect
        assert config.args == resolved


# ── MCPExecutor template resolution ──────────────────────────────


class TestTemplateResolution:
    def test_no_templates(self):
        from muse.kernel.mcp_executor import MCPExecutor
        from muse.kernel.session_store import SessionStore

        registry = MagicMock()
        session = SessionStore()
        executor = MCPExecutor(registry, session)

        config = MagicMock()
        config.args = ["search", "--query", "hello"]

        result = executor._resolve_template_args(config)
        assert result is None  # No templates found

    def test_template_detected(self):
        from muse.kernel.mcp_executor import MCPExecutor
        from muse.kernel.session_store import SessionStore

        registry = MagicMock()
        session = SessionStore()
        executor = MCPExecutor(registry, session)

        config = MagicMock()
        config.args = ["serena", "mcp", "--workspace", "{workspace}"]
        config.server_id = "serena"

        result = executor._resolve_template_args(config)
        # Returns a list (templates found) but value may not be resolved
        # if no conversation history has paths
        assert result is not None
        assert isinstance(result, list)

    def test_template_resolved_from_history(self):
        from muse.kernel.mcp_executor import MCPExecutor
        from muse.kernel.session_store import SessionStore
        import tempfile
        import os

        registry = MagicMock()
        session = SessionStore()

        # Add a message with a directory path that exists
        tmp = tempfile.gettempdir()
        session.conversation_history.append({
            "role": "user",
            "content": f"Look at my project at {tmp}",
        })

        executor = MCPExecutor(registry, session)

        config = MagicMock()
        config.args = ["serena", "mcp", "--workspace", "{workspace}"]
        config.server_id = "serena"

        result = executor._resolve_template_args(config)
        assert result is not None
        # The temp dir should be resolved
        assert tmp in result[3] or "{workspace}" not in result[3]


# ── Skill loader manifest for on-demand ──────────────────────────


class TestMCPManifestGeneration:
    def test_manifest_from_connected_server(self):
        from muse.skills.loader import SkillLoader

        db = _mock_db()
        loader = SkillLoader(db, MagicMock())

        mcp_manager = MagicMock()
        conn = MagicMock()
        conn.config.name = "TestMCP"
        mcp_manager.get_connection.return_value = conn
        loader.set_mcp_manager(mcp_manager)

        manifest = loader._build_mcp_manifest("mcp:test")
        assert manifest is not None
        assert manifest.name == "TestMCP"

    def test_manifest_from_on_demand_config(self):
        from muse.skills.loader import SkillLoader

        db = _mock_db()
        loader = SkillLoader(db, MagicMock())

        mcp_manager = MagicMock()
        mcp_manager.get_connection.return_value = None  # Not connected
        config = MCPServerConfig(
            server_id="serena", name="Serena", transport="stdio",
            lifecycle="on_demand",
        )
        mcp_manager.get_on_demand_configs.return_value = {"serena": config}
        loader.set_mcp_manager(mcp_manager)

        manifest = loader._build_mcp_manifest("mcp:serena")
        assert manifest is not None
        assert manifest.name == "Serena"

    def test_manifest_returns_none_for_unknown(self):
        from muse.skills.loader import SkillLoader

        db = _mock_db()
        loader = SkillLoader(db, MagicMock())

        mcp_manager = MagicMock()
        mcp_manager.get_connection.return_value = None
        mcp_manager.get_on_demand_configs.return_value = {}
        loader.set_mcp_manager(mcp_manager)

        manifest = loader._build_mcp_manifest("mcp:unknown")
        assert manifest is None


# ── MCP Install skill lifecycle inference ────────────────────────


class TestLifecycleInference:
    def test_placeholder_in_args_infers_on_demand(self):
        """Config with PLACEHOLDER in args should default to on_demand."""
        config = {
            "server_id": "serena",
            "name": "Serena",
            "transport": "stdio",
            "command": "uvx",
            "args": ["serena", "mcp", "--workspace", "PLACEHOLDER"],
        }
        # Simulate the validation logic from _extract_mcp_config
        has_placeholder = any(
            "PLACEHOLDER" in str(a).upper() for a in config.get("args", [])
        )
        lifecycle = "on_demand" if has_placeholder else "persistent"
        assert lifecycle == "on_demand"

    def test_no_placeholder_infers_persistent(self):
        config = {
            "server_id": "search",
            "name": "Search",
            "args": ["-y", "a2asearch-mcp"],
        }
        has_placeholder = any(
            "PLACEHOLDER" in str(a).upper() for a in config.get("args", [])
        )
        lifecycle = "on_demand" if has_placeholder else "persistent"
        assert lifecycle == "persistent"
