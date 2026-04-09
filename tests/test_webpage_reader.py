"""Tests for the webpage_reader skill — URL extraction, pipeline context
fallback, and graceful failure when no URL is available.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "sdk"))
sys.path.insert(0, str(PROJECT_ROOT))

# Import skill functions directly
from skills.webpage_reader.skill import (
    _extract_url,
    _extract_urls_from_pipeline,
    _parse_html,
)


# ── URL extraction ─────────────────────────────────────────────────────


class TestExtractUrl:
    def test_extracts_https_url(self):
        assert _extract_url("Read https://example.com/page") == "https://example.com/page"

    def test_extracts_http_url(self):
        assert _extract_url("Fetch http://example.com") == "http://example.com"

    def test_strips_trailing_punctuation(self):
        assert _extract_url("See https://example.com.") == "https://example.com"
        assert _extract_url("Link: https://example.com)") == "https://example.com"

    def test_returns_none_when_no_url(self):
        assert _extract_url("search for sightseeing spots in Japan") is None

    def test_returns_none_for_empty_string(self):
        assert _extract_url("") is None

    def test_picks_first_url(self):
        text = "Check https://a.com and https://b.com"
        assert _extract_url(text) == "https://a.com"


# ── Pipeline URL extraction ────────────────────────────────────────────


class TestExtractUrlsFromPipeline:
    def _make_ctx(self, pipeline_context):
        ctx = MagicMock()
        ctx.brief = {
            "context": {"pipeline_context": pipeline_context},
        }
        return ctx

    def test_extracts_urls_from_search_results(self):
        ctx = self._make_ctx({
            "task_0_result": (
                "Here are the results:\n"
                "1. [Japan Guide](https://www.japan-guide.com/destinations)\n"
                "2. [Lonely Planet](https://www.lonelyplanet.com/japan)"
            ),
        })
        urls = _extract_urls_from_pipeline(ctx)
        assert len(urls) == 2
        assert "https://www.japan-guide.com/destinations" in urls
        assert "https://www.lonelyplanet.com/japan" in urls

    def test_deduplicates_urls(self):
        ctx = self._make_ctx({
            "task_0_result": "https://example.com and https://example.com again",
        })
        urls = _extract_urls_from_pipeline(ctx)
        assert len(urls) == 1

    def test_empty_pipeline(self):
        ctx = self._make_ctx({})
        assert _extract_urls_from_pipeline(ctx) == []

    def test_ignores_non_string_values(self):
        ctx = self._make_ctx({
            "task_0_result": None,
            "task_0_data": {"key": "value"},
        })
        assert _extract_urls_from_pipeline(ctx) == []

    def test_no_pipeline_context(self):
        ctx = MagicMock()
        ctx.brief = {"context": {}}
        assert _extract_urls_from_pipeline(ctx) == []


# ── _fetch_page behavior ──────────────────────────────────────────────


class TestFetchPage:
    """Test _fetch_page URL resolution and graceful failure."""

    @pytest.mark.asyncio
    async def test_no_url_sets_page_error(self):
        """When no URL is found anywhere, _fetch_page returns None and sets _page_error."""
        from skills.webpage_reader.skill import _fetch_page

        ctx = MagicMock()
        ctx.brief = {"context": {"pipeline_context": {}}}

        result = await _fetch_page(ctx, "find sightseeing spots in Japan")
        assert result is None
        assert hasattr(ctx, "_page_error")
        assert "No URL provided" in ctx._page_error
        assert "Search skill" in ctx._page_error

    @pytest.mark.asyncio
    async def test_url_in_instruction_used_directly(self):
        """When the instruction contains a URL, use it directly."""
        from skills.webpage_reader.skill import _fetch_page

        ctx = MagicMock()
        ctx.brief = {"context": {"pipeline_context": {}}}
        ctx.task = AsyncMock()

        # Mock HTTP to return a simple page
        response = MagicMock()
        response.status_code = 200
        response.text.return_value = "<html><body><p>Hello world</p></body></html>"
        ctx.http = AsyncMock()
        ctx.http.get.return_value = response

        result = await _fetch_page(ctx, "Read https://example.com/page")
        assert result is not None
        assert result["url"] == "https://example.com/page"

    @pytest.mark.asyncio
    async def test_url_from_pipeline_context(self):
        """When instruction has no URL but pipeline context does, use it."""
        from skills.webpage_reader.skill import _fetch_page

        ctx = MagicMock()
        ctx.brief = {
            "context": {
                "pipeline_context": {
                    "task_0_result": "Found: https://japan-guide.com/spots",
                },
            },
        }
        ctx.task = AsyncMock()

        response = MagicMock()
        response.status_code = 200
        response.text.return_value = "<html><body><p>Spots</p></body></html>"
        ctx.http = AsyncMock()
        ctx.http.get.return_value = response

        result = await _fetch_page(ctx, "Read the page about sightseeing")
        assert result is not None
        assert result["url"] == "https://japan-guide.com/spots"

    @pytest.mark.asyncio
    async def test_instruction_url_takes_priority_over_pipeline(self):
        """URL in instruction should be preferred over pipeline URLs."""
        from skills.webpage_reader.skill import _fetch_page

        ctx = MagicMock()
        ctx.brief = {
            "context": {
                "pipeline_context": {
                    "task_0_result": "Found: https://other-site.com/page",
                },
            },
        }
        ctx.task = AsyncMock()

        response = MagicMock()
        response.status_code = 200
        response.text.return_value = "<html><body><p>Content</p></body></html>"
        ctx.http = AsyncMock()
        ctx.http.get.return_value = response

        result = await _fetch_page(ctx, "Read https://explicit-url.com/target")
        assert result is not None
        assert result["url"] == "https://explicit-url.com/target"


# ── HTML parsing ──────────────────────────────────────────────────────


class TestParseHtml:
    def test_extracts_body_text(self):
        html = "<html><body><h1>Title</h1><p>Hello world</p></body></html>"
        title, text = _parse_html(html)
        assert "Hello world" in text

    def test_extracts_title(self):
        html = "<html><head><title>My Page</title></head><body><p>Text</p></body></html>"
        title, text = _parse_html(html)
        assert title == "My Page"

    def test_skips_script_and_style(self):
        html = "<html><body><script>var x=1;</script><style>.a{}</style><p>Visible</p></body></html>"
        _, text = _parse_html(html)
        assert "var x" not in text
        assert ".a{}" not in text
        assert "Visible" in text

    def test_empty_html(self):
        title, text = _parse_html("")
        assert title == ""
        assert text == ""
