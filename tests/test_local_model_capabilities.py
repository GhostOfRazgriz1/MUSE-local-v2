"""Local model capability tests — validates LLM output stability against Ollama.

Runs against a live Ollama instance. Skipped automatically if Ollama isn't
reachable. Tests every model available on the system.

Run with: python -m pytest tests/test_local_model_capabilities.py -v --timeout=120
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import httpx
import pytest
import pytest_asyncio

# ── Skip if Ollama not running ──────────────────────────────────

OLLAMA_BASE = "http://localhost:11434/v1"


def _ollama_available() -> bool:
    try:
        r = httpx.get(f"{OLLAMA_BASE}/models", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _get_models() -> list[str]:
    try:
        r = httpx.get(f"{OLLAMA_BASE}/models", timeout=5)
        data = r.json()
        return [m["id"] for m in data.get("data", [])]
    except Exception:
        return []


pytestmark = pytest.mark.skipif(
    not _ollama_available(), reason="Ollama not running at localhost:11434"
)

# Filter out models too small for reliable classification (<2B params)
_MIN_MODEL_SIZE = {"gemma3:1b"}  # known too-small models
MODELS = [m for m in _get_models() if m not in _MIN_MODEL_SIZE] or ["gemma3:4b"]


# ── Helpers ─────────────────────────────────────────────────────

def parse_json_response(text: str) -> dict | list | None:
    """Strip markdown fences and parse JSON. Returns None on failure."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    # Strip <think> blocks from thinking models
    if "<think>" in text:
        text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


@pytest_asyncio.fixture
async def provider():
    from muse.providers.openai_compat import OpenAICompatibleProvider
    p = OpenAICompatibleProvider(
        name="local", base_url=OLLAMA_BASE, api_key="ollama",
    )
    yield p
    await p.close()


# ── Shared prompts (mirror actual MUSE prompts) ────────────────

SKILL_CATALOG = """  - Search: Search the web for information
  - Files: Create, read, write files
  - Shell: Run shell commands
  - Reminders: Set reminders and alarms
  - Email: Send and read emails
  - Code Runner: Execute Python code, do calculations
  - Calendar: View and create calendar events
  - MCP Install: Install MCP servers from GitHub"""

CLASSIFIER_SYSTEM = (
    "Route the user's request to the right skill. Rules:\n"
    "- 'none': chat, greetings, questions, or unclear intent\n"
    "- 'single': one skill handles it\n"
    "- 'multi': 2-3 skills needed (e.g. search + save)\n"
    "- 'goal': complex 4+ step objective\n"
    "- 'clarify': ambiguous, ask ONE question\n"
    "- Code Runner = run/compute code. Creating files = Files skill.\n"
    "Reply with ONLY valid JSON."
)

VALID_ACTIONS = {"none", "single", "multi", "goal", "clarify"}
VALID_SKILLS = {"Search", "Files", "Shell", "Reminders", "Email",
                "Code Runner", "Calendar", "MCP Install"}


def _build_classifier_prompt(user_message: str) -> str:
    return (
        f'User message: "{user_message}"\n\n'
        f"Available skills:\n{SKILL_CATALOG}\n\n"
        f"Decide how to handle this message. Reply with JSON:\n"
        f'{{"action": "none"}}  — general chat\n'
        f'{{"action": "single", "skill": "<skill_id>"}}  — one skill\n'
        f'{{"action": "multi", "sub_tasks": [...]}}\n'
        f'{{"action": "goal"}}  — complex multi-step\n'
        f'{{"action": "clarify", "question": "..."}}\n\n'
        f"Reply with ONLY valid JSON."
    )


# ═══════════════════════════════════════════════════════════════
# 1. JSON OUTPUT STABILITY
# ═══════════════════════════════════════════════════════════════


class TestIntentClassificationJSON:
    """Intent classifier produces valid, parseable JSON."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    @pytest.mark.parametrize("message,expected_action", [
        ("hello!", "none"),
        ("What's the weather in Tokyo?", "single"),
        ("Write me a Python script that sorts a list", "single"),
        ("Search for Python tutorials and save a summary to a file", "multi"),
        ("Plan a trip to Japan with flights, hotels, and activities", "goal"),
    ])
    async def test_valid_json(self, provider, model, message, expected_action):
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": _build_classifier_prompt(message)}],
            system=CLASSIFIER_SYSTEM,
            max_tokens=300,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None, f"Failed to parse JSON: {result.text[:200]}"
        assert "action" in parsed, f"Missing 'action' key: {parsed}"
        assert parsed["action"] in VALID_ACTIONS, f"Invalid action: {parsed['action']}"


class TestIntentRoutingAccuracy:
    """Classifier routes to the correct skill."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    @pytest.mark.parametrize("message,expected_skill", [
        ("What is quantum computing?", "Search"),
        ("Create a file called notes.md with my meeting notes", "Files"),
        ("List the files in my home directory", "Shell"),
        ("Remind me to call mom tomorrow at 3pm", "Reminders"),
        ("Check my inbox for new emails", "Email"),
        ("Calculate 15% tip on $47.50", "Code Runner"),
        ("What's on my schedule today?", "Calendar"),
    ])
    async def test_routing(self, provider, model, message, expected_skill):
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": _build_classifier_prompt(message)}],
            system=CLASSIFIER_SYSTEM,
            max_tokens=300,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None, f"Failed to parse JSON: {result.text[:200]}"
        if parsed.get("action") == "single":
            skill = parsed.get("skill", "")
            assert skill == expected_skill, (
                f"Expected {expected_skill}, got {skill} for: {message}"
            )


class TestIntentNoneForChat:
    """Conversational messages should route to 'none'."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    @pytest.mark.parametrize("message", [
        "hello",
        "thanks!",
        "how are you?",
        "tell me a joke",
        "what can you do?",
    ])
    async def test_chat_routes_none(self, provider, model, message):
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": _build_classifier_prompt(message)}],
            system=CLASSIFIER_SYSTEM,
            max_tokens=300,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None
        assert parsed.get("action") == "none", (
            f"Expected 'none' for chat message '{message}', got: {parsed}"
        )


class TestPlanGenerationJSON:
    """Plan generation produces valid JSON arrays with correct structure."""

    PLAN_SYSTEM = (
        "You are a task planner. Break the goal into steps using the available skills.\n"
        f"Available skills:\n{SKILL_CATALOG}\n\n"
        "Output a JSON array. Each step: "
        '{"skill_id": "...", "instruction": "...", "depends_on": []}.\n'
        "Maximum 8 steps. Reply with ONLY a JSON array."
    )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    @pytest.mark.parametrize("goal", [
        "Research the top 5 programming languages and create a comparison document",
        "Find out what the weather is in Tokyo and set a reminder about it",
        "Search for recipe ideas, pick one, and save the ingredients to a file",
    ])
    async def test_plan_json_array(self, provider, model, goal):
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": goal}],
            system=self.PLAN_SYSTEM,
            max_tokens=1000,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None, f"Failed to parse plan JSON: {result.text[:300]}"
        assert isinstance(parsed, list), f"Expected list, got {type(parsed)}"
        assert len(parsed) >= 1, "Plan has no steps"
        assert len(parsed) <= 10, f"Plan has too many steps: {len(parsed)}"

        for i, step in enumerate(parsed):
            assert "skill_id" in step, f"Step {i} missing skill_id"
            assert "instruction" in step, f"Step {i} missing instruction"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_plan_dependencies_valid(self, provider, model):
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content":
                "Search for Python tutorials, read the best one, then save notes to a file"}],
            system=self.PLAN_SYSTEM,
            max_tokens=1000,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None and isinstance(parsed, list)
        for i, step in enumerate(parsed):
            deps = step.get("depends_on", [])
            for d in deps:
                assert isinstance(d, int), f"Step {i} has non-int dependency: {d}"
                assert 0 <= d < len(parsed), (
                    f"Step {i} depends on step {d}, but only {len(parsed)} steps exist"
                )
                assert d != i, f"Step {i} depends on itself"


class TestMCPArgumentExtractionJSON:
    """MCP argument extraction produces valid JSON matching schemas."""

    MCP_SYSTEM = (
        "Extract tool arguments from the user's request. "
        "Reply with ONLY valid JSON matching the schema."
    )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_no_params(self, provider, model):
        prompt = (
            'User: "What time is it?"\n'
            'Tool: get_time\n'
            'Schema: {"type": "object", "properties": {}}\n\n'
            "Extract arguments as JSON. Reply with ONLY valid JSON."
        )
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            system=self.MCP_SYSTEM,
            max_tokens=100,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None, f"Failed to parse: {result.text[:200]}"
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_required_params(self, provider, model):
        prompt = (
            'User: "Search for Python tutorials"\n'
            'Tool: search\n'
            'Schema: {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}\n\n'
            "Extract arguments as JSON. Reply with ONLY valid JSON."
        )
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            system=self.MCP_SYSTEM,
            max_tokens=200,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None, f"Failed to parse: {result.text[:200]}"
        assert "query" in parsed, f"Missing required field 'query': {parsed}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_multiple_params(self, provider, model):
        schema = {
            "type": "object",
            "properties": {
                "location": {"type": "string"},
                "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                "days": {"type": "integer"},
            },
            "required": ["location"],
        }
        prompt = (
            'User: "What\'s the weather in Tokyo for the next 3 days in celsius?"\n'
            f'Tool: get_weather\n'
            f'Schema: {json.dumps(schema)}\n\n'
            "Extract arguments as JSON. Reply with ONLY valid JSON."
        )
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            system=self.MCP_SYSTEM,
            max_tokens=200,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None
        assert "location" in parsed, f"Missing 'location': {parsed}"


class TestMemoryExtractionJSON:
    """Memory consolidation produces valid JSON with correct namespaces."""

    DREAM_SYSTEM = (
        "Review this conversation and extract durable knowledge.\n"
        "Output a JSON array of memory entries. Each entry:\n"
        '{"namespace": "_profile"|"_facts"|"_project"|"_emotions", '
        '"key": "short-slug", "value": "the fact"}\n'
        "Reply with ONLY a JSON array."
    )

    CONVERSATION = (
        "user: Hi, I'm Alex and I'm a data scientist at Google.\n"
        "assistant: Nice to meet you, Alex!\n"
        "user: I prefer dark mode and I use Python mainly.\n"
        "assistant: I'll remember your preferences.\n"
        "user: I'm working on a machine learning project with TensorFlow.\n"
        "assistant: Sounds interesting! What kind of model are you building?\n"
        "user: A recommendation system. The deadline is next Friday.\n"
        "assistant: Good luck with the deadline!\n"
        "user: Thanks, I'm a bit stressed about it honestly.\n"
        "assistant: That's understandable. Let me know if I can help."
    )

    VALID_NAMESPACES = {"_profile", "_facts", "_project", "_emotions"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_valid_memory_json(self, provider, model):
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": self.CONVERSATION}],
            system=self.DREAM_SYSTEM,
            max_tokens=1500,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None, f"Failed to parse: {result.text[:300]}"
        assert isinstance(parsed, list), f"Expected list, got {type(parsed)}"
        assert len(parsed) >= 1, "No memories extracted"

        for i, entry in enumerate(parsed):
            assert "namespace" in entry, f"Entry {i} missing namespace"
            assert "key" in entry, f"Entry {i} missing key"
            assert "value" in entry, f"Entry {i} missing value"
            assert entry["namespace"] in self.VALID_NAMESPACES, (
                f"Entry {i} invalid namespace: {entry['namespace']}"
            )


# ═══════════════════════════════════════════════════════════════
# 2. RESPONSE FORMAT STABILITY
# ═══════════════════════════════════════════════════════════════


class TestRelevanceCheckFormat:
    """Relevance check returns RELEVANT/IRRELEVANT format."""

    SYSTEM = "Check if the step result matches the instruction. Reply RELEVANT, RELEVANT-ADJUST: reason, or IRRELEVANT: reason."

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    @pytest.mark.parametrize("goal,step,result_text,expected_prefix", [
        (
            "Plan a trip to Japan",
            "Search for sightseeing spots in Japan",
            "Top 5 spots: Fushimi Inari, Mount Fuji, Shibuya, Kyoto temples, Osaka castle",
            "RELEVANT",
        ),
        (
            "Plan a trip to Japan",
            "Search for sightseeing spots in Japan",
            "How to cook traditional ramen at home: ingredients and step-by-step recipe",
            "IRRELEVANT",
        ),
    ])
    async def test_format(self, provider, model, goal, step, result_text, expected_prefix):
        prompt = (
            f"Goal: {goal}\n"
            f"Step: {step}\n"
            f"Result: {result_text}\n\n"
            "Reply: RELEVANT / RELEVANT-ADJUST: reason / IRRELEVANT: reason"
        )
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            system=self.SYSTEM,
            max_tokens=50,
        )
        text = result.text.strip().upper()
        assert text.startswith("RELEVANT") or text.startswith("IRRELEVANT"), (
            f"Expected RELEVANT or IRRELEVANT, got: {result.text[:100]}"
        )


class TestSessionSummaryFormat:
    """Session summary is brief and non-empty."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_brief_summary(self, provider, model):
        conversation = (
            "user: Can you search for the latest Python release?\n"
            "assistant: Python 3.13 was released with several new features.\n"
            "user: What are the key features?\n"
            "assistant: Key features include improved error messages and performance.\n"
            "user: Save that to a file called python_notes.md\n"
            "assistant: Saved python_notes.md with the Python 3.13 summary."
        )
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": conversation}],
            system=(
                "Write a brief summary of this conversation (2-3 sentences). "
                "Focus on what was accomplished."
            ),
            max_tokens=200,
        )
        text = result.text.strip()
        assert len(text) > 10, f"Summary too short: {text}"
        assert len(text) < 1000, f"Summary too long ({len(text)} chars)"


class TestEnrichmentFormat:
    """Response enrichment produces natural language, not raw data."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_enriches_json(self, provider, model):
        raw_json = json.dumps({
            "results": [
                {"title": "Python Tutorial", "url": "https://example.com/python", "score": 0.95},
                {"title": "Java Basics", "url": "https://example.com/java", "score": 0.82},
            ],
            "total": 2,
            "query": "programming tutorials",
        })
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": (
                f'The user asked: "Find programming tutorials"\n'
                f"The tool returned:\n{raw_json}\n\n"
                f"Write a concise, helpful response. Don't repeat raw JSON."
            )}],
            system="Summarise tool output concisely.",
            max_tokens=300,
        )
        text = result.text.strip()
        assert not text.startswith("{"), f"Response is raw JSON: {text[:100]}"
        assert not text.startswith("["), f"Response is raw JSON: {text[:100]}"
        assert len(text) > 20, f"Response too short: {text}"


# ═══════════════════════════════════════════════════════════════
# 3. THINKING MODEL HANDLING
# ═══════════════════════════════════════════════════════════════


class TestThinkingModelHandling:
    """Provider correctly handles <think> tags and reasoning fields."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_response_not_empty(self, provider, model):
        """Model should produce non-empty text after think-stripping."""
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": "Say hello in one sentence."}],
            max_tokens=200,
        )
        text = result.text.strip()
        assert len(text) > 0, "Response is empty after processing"
        # Should not contain raw think tags
        assert "<think>" not in text, f"Think tags not stripped: {text[:200]}"

    def test_reasoning_field_fallback(self):
        """When content is empty but reasoning exists, reasoning is used."""
        # Simulate the provider's parsing logic
        from muse.providers.openai_compat import OpenAICompatibleProvider
        import re as _re

        # Mock the response data
        message = {"content": "", "reasoning": "The answer is 42."}
        text = message.get("content", "")
        if not text and message.get("reasoning"):
            text = message["reasoning"]
        if "<think>" in text:
            cleaned = _re.sub(r"<think>.*?</think>\s*", "", text, flags=_re.DOTALL).strip()
            if cleaned:
                text = cleaned

        assert text == "The answer is 42."


# ═══════════════════════════════════════════════════════════════
# 4. LATENCY BASELINES
# ═══════════════════════════════════════════════════════════════


class TestLatency:
    """Responses arrive within acceptable time for local inference."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_classification_latency(self, provider, model):
        t0 = time.monotonic()
        await provider.complete(
            model=model,
            messages=[{"role": "user", "content": _build_classifier_prompt("hello")}],
            system=CLASSIFIER_SYSTEM,
            max_tokens=100,
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 30, f"Classification took {elapsed:.1f}s (limit: 30s)"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_short_response_latency(self, provider, model):
        t0 = time.monotonic()
        await provider.complete(
            model=model,
            messages=[{"role": "user", "content": "Reply: RELEVANT"}],
            system="Reply with one word.",
            max_tokens=10,
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 15, f"Short response took {elapsed:.1f}s (limit: 15s)"


# ═══════════════════════════════════════════════════════════════
# 5. EDGE CASES & ROBUSTNESS
# ═══════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Model handles unusual inputs without crashing."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_empty_input(self, provider, model):
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": _build_classifier_prompt("")}],
            system=CLASSIFIER_SYSTEM,
            max_tokens=100,
        )
        parsed = parse_json_response(result.text)
        # Should return valid JSON even for empty input
        assert parsed is not None, f"Empty input broke JSON: {result.text[:200]}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_long_input(self, provider, model):
        long_msg = "Tell me about " + "the history of " * 100 + "computers"
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": _build_classifier_prompt(long_msg)}],
            system=CLASSIFIER_SYSTEM,
            max_tokens=100,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None, f"Long input broke JSON: {result.text[:200]}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    @pytest.mark.parametrize("message", [
        "Busca información sobre Python",           # Spanish
        "Pythonについて調べて",                        # Japanese
        "搜索Python教程",                              # Chinese
    ])
    async def test_non_english(self, provider, model, message):
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": _build_classifier_prompt(message)}],
            system=CLASSIFIER_SYSTEM,
            max_tokens=300,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None, f"Non-English broke JSON: {result.text[:200]}"
        assert "action" in parsed

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_special_characters(self, provider, model):
        msg = 'Search for "python\'s best practices" in C:\\Users\\test & more'
        result = await provider.complete(
            model=model,
            messages=[{"role": "user", "content": _build_classifier_prompt(msg)}],
            system=CLASSIFIER_SYSTEM,
            max_tokens=300,
        )
        parsed = parse_json_response(result.text)
        assert parsed is not None, f"Special chars broke JSON: {result.text[:200]}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_repeated_calls_consistency(self, provider, model):
        """Same prompt should produce consistent routing."""
        prompt = _build_classifier_prompt("What's the weather in Tokyo?")
        actions = []
        for _ in range(3):
            result = await provider.complete(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                system=CLASSIFIER_SYSTEM,
                max_tokens=300,
            )
            parsed = parse_json_response(result.text)
            if parsed:
                actions.append(parsed.get("action"))

        assert len(actions) == 3, "Some calls failed to parse"
        # All should be the same action (or at least same skill)
        assert len(set(actions)) == 1, (
            f"Inconsistent routing across 3 calls: {actions}"
        )
