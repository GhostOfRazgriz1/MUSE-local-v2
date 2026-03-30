"""Webpage Reader skill — fetch and extract readable text from URLs.

Every action LLM-cleans the raw HTML extraction before returning.
The raw parser output is never user-facing — it's intermediate data.

Uses stdlib html.parser for HTML extraction (no external dependencies).
Routes through the gateway which enforces SSRF protection.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser


# ── HTML text extraction ────────────────────────────────────────

_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "svg", "math", "head",
    "nav", "footer", "header", "aside", "form", "button",
    "iframe", "object", "embed",
})

_BLOCK_TAGS = frozenset({
    "p", "div", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "tr", "dt", "dd", "blockquote", "pre", "section", "article",
    "figcaption", "summary", "details",
})


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._text: list[str] = []
        self._skip_depth = 0
        self._title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower in _SKIP_TAGS:
            self._skip_depth += 1
        if tag_lower == "title":
            self._in_title = True
        if tag_lower in _BLOCK_TAGS:
            self._text.append("\n")

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag_lower == "title":
            self._in_title = False
        if tag_lower in _BLOCK_TAGS:
            self._text.append("\n")

    def handle_data(self, data):
        if self._in_title and not self._title:
            self._title = data.strip()
        if self._skip_depth == 0:
            self._text.append(data)

    def get_text(self) -> str:
        raw = "".join(self._text)
        lines = []
        for line in raw.split("\n"):
            cleaned = " ".join(line.split())
            if cleaned:
                lines.append(cleaned)
        return "\n".join(lines)

    def get_title(self) -> str:
        return self._title


def _parse_html(html: str) -> tuple[str, str]:
    """Extract (title, body_text) from HTML."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.get_title(), parser.get_text()


# ── URL extraction ──────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
MAX_RAW_CHARS = 10_000  # raw extraction limit fed to LLM


def _extract_url(text: str) -> str | None:
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,;:)]}") if m else None


def _err(msg: str) -> dict:
    return {"payload": None, "summary": msg, "success": False}


# ── Core: fetch + parse + LLM-clean ────────────────────────────


async def _fetch_page(ctx, instruction: str) -> dict | None:
    """Fetch a URL, parse HTML, and return raw data.

    Returns ``{url, title, raw_text, word_count}`` or None on failure
    (in which case an error dict is stored in ``ctx._page_error``).
    """
    url = _extract_url(instruction)

    if not url:
        extracted = await ctx.llm.complete(
            prompt=f"Extract the URL from this request. Reply with ONLY the URL.\n\n{instruction}",
            system="Output only a URL. Nothing else.",
            max_tokens=100,
        )
        url = _extract_url(extracted.strip())

    if not url:
        return None

    await ctx.task.report_status(f"Fetching {url}")

    try:
        resp = await ctx.http.get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; AgentOS/1.0)",
            "Accept": "text/html,application/xhtml+xml,*/*",
        })
    except Exception as e:
        return None

    if resp.status_code != 200:
        return None

    html = resp.text()
    title, text = _parse_html(html)

    if not text.strip():
        return None

    return {
        "url": url,
        "title": title,
        "raw_text": text[:MAX_RAW_CHARS],
        "word_count": len(text.split()),
    }


async def _clean_content(ctx, page: dict) -> str:
    """LLM pass to strip boilerplate and extract main content."""
    await ctx.task.report_status("Extracting main content...")

    cleaned = await ctx.llm.complete(
        prompt=(
            f"URL: {page['url']}\n"
            f"Page title: {page['title']}\n\n"
            f"Raw extracted text:\n{page['raw_text']}\n\n"
            f"Extract ONLY the main content of this page. Remove:\n"
            f"- Navigation menus and links\n"
            f"- Headers/footers (site-wide, not article headers)\n"
            f"- Marketing copy, CTAs, testimonials\n"
            f"- Repeated/carousel content\n"
            f"- Cookie notices, legal boilerplate\n\n"
            f"Keep: the actual article, product description, documentation, "
            f"or primary information the page is about. "
            f"Preserve the structure (headings, lists, paragraphs)."
        ),
        system=(
            "You extract the main content from a webpage. "
            "Output ONLY the cleaned content in markdown. "
            "No commentary, no 'Here is the content:' prefix."
        ),
        max_tokens=2000,
    )
    return cleaned.strip()


# ── Entry points ────────────────────────────────────────────────


async def read(ctx) -> dict:
    """Fetch a URL, clean the content, return readable text."""
    instruction = ctx.brief.get("instruction", "")
    page = await _fetch_page(ctx, instruction)

    if not page:
        url = _extract_url(instruction)
        return _err(f"Could not fetch or extract content from {url or 'the URL'}.")

    content = await _clean_content(ctx, page)

    # Cache for follow-up questions
    cache_key = f"page.{page['url'][:80]}"
    await ctx.memory.write(cache_key, content[:4000], value_type="text")

    title = page["title"]
    summary = f"**{title}**\n\n{content}" if title else content

    return {
        "payload": {
            "url": page["url"],
            "title": title,
            "content": content,
            "word_count": page["word_count"],
        },
        "summary": summary,
        "success": True,
    }


async def summarize(ctx) -> dict:
    """Fetch a URL and return a concise summary."""
    instruction = ctx.brief.get("instruction", "")
    page = await _fetch_page(ctx, instruction)

    if not page:
        url = _extract_url(instruction)
        return _err(f"Could not fetch or extract content from {url or 'the URL'}.")

    await ctx.task.report_status("Summarizing...")

    summary = await ctx.llm.complete(
        prompt=(
            f"URL: {page['url']}\n"
            f"Page title: {page['title']}\n\n"
            f"Page content:\n{page['raw_text']}\n\n"
            f"Provide a concise summary of what this page is about. "
            f"Focus on the key information, purpose, and notable details."
        ),
        system=(
            "Summarize the webpage concisely. Use markdown. "
            "Highlight key points. 3-5 paragraphs max."
        ),
        max_tokens=800,
    )

    # Cache for follow-ups
    cache_key = f"page.{page['url'][:80]}"
    await ctx.memory.write(cache_key, summary[:4000], value_type="text")

    return {
        "payload": {
            "url": page["url"],
            "title": page["title"],
            "summary": summary,
            "word_count": page["word_count"],
        },
        "summary": summary,
        "success": True,
    }


async def run(ctx) -> dict:
    """Default entry — detect intent and route."""
    instruction = ctx.brief.get("instruction", "").lower()

    # Explicit summarize intent
    if any(w in instruction for w in ["summarize", "summary", "tldr", "brief", "overview"]):
        return await summarize(ctx)

    # "Check this out" / "look at this" — conversational take
    if any(w in instruction for w in ["check", "look at", "what is", "what does", "tell me about"]):
        return await summarize(ctx)

    # Default: clean read
    return await read(ctx)
