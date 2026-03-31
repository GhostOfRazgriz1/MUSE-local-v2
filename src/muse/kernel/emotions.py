"""Emotion tracking — lightweight real-time detection + relationship scoring.

Detects emotional signals from user messages via keyword/pattern matching
(no LLM call) and maintains a running session mood.  Significant emotional
events are persisted to the ``_emotions`` namespace in memory.

The relationship progression score is computed from four signals:
  breadth   – how many memory namespaces contain entries
  depth     – presence of personal / emotional content
  consistency – return frequency (active days in the last 30)
  trust     – permissions granted at session or always level

Four relationship levels gate agent behavior:
  1  Just getting started — basic assistant
  2  Getting to know you  — personalized greetings, suggestions
  3  Building trust       — gentle follow-ups on life events
  4  Close companion      — emotional tone adjustment, proactive check-ins
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Emotion categories & keyword patterns ─────────────────────────
# Each tuple: (compiled regex, emotion label, valence -1..+1)

_EMOTION_PATTERNS: list[tuple[re.Pattern, str, float]] = [
    # Frustration / anger
    (re.compile(
        r"\b(?:frustrat(?:ed|ing)|annoying|annoyed|infuriat|ugh+|argh|"
        r"sick\s+of|fed\s+up|can'?t\s+(?:figure|stand|believe)|hate\s+(?:this|it|that)|"
        r"pissed|furious|angry|mad\s+(?:about|at|that))\b", re.I),
     "frustration", -0.7),

    # Stress / overwhelm
    (re.compile(
        r"\b(?:stress(?:ed|ful)?|overwhelm(?:ed|ing)|swamp(?:ed)?|"
        r"too\s+much|pressure|deadline|crunch|burn(?:ed|t)\s*out|"
        r"exhausted|drained|can'?t\s+keep\s+up)\b", re.I),
     "stress", -0.6),

    # Anxiety / worry
    (re.compile(
        r"\b(?:worr(?:ied|y)|anxi(?:ous|ety)|nervous|scared|"
        r"freaking\s+out|panic|dread|uncertain|on\s+edge)\b", re.I),
     "anxiety", -0.5),

    # Sadness / disappointment
    (re.compile(
        r"\b(?:sad(?:ly)?|depress(?:ed|ing)|down\s+(?:about|today|lately)|"
        r"disappoint(?:ed|ing)|heartbr(?:oken|eaking)|miss(?:ing)?|"
        r"upset|bummed|feeling\s+(?:low|bad|terrible|awful))\b", re.I),
     "sadness", -0.6),

    # Excitement / joy
    (re.compile(
        r"\b(?:excit(?:ed|ing)|amazing|awesome|fantastic|incredible|"
        r"can'?t\s+wait|thrilled|love\s+(?:this|it|that)|pumped|"
        r"stoked|hyped|so\s+(?:happy|glad|cool)|wonderful|brilliant)\b", re.I),
     "excitement", 0.8),

    # Accomplishment / pride
    (re.compile(
        r"\b(?:finally\s+(?:done|finished|got|made|fixed)|shipped|"
        r"launch(?:ed)?|completed|nailed\s+it|crushed\s+it|"
        r"proud|made\s+it|pulled\s+(?:it\s+)?off|milestone|breakthrough)\b", re.I),
     "accomplishment", 0.9),

    # Gratitude / appreciation
    (re.compile(
        r"\b(?:thank(?:s|ful)?|grateful|appreciat(?:e|ed)|"
        r"means\s+a\s+lot|couldn'?t\s+have\s+done|you'?re\s+the\s+best|"
        r"really\s+help(?:ed|ful))\b", re.I),
     "gratitude", 0.6),

    # Curiosity / enthusiasm (mild positive)
    (re.compile(
        r"\b(?:curious|fascinated|interested|intrigued|"
        r"wonder(?:ing)?|want\s+to\s+(?:learn|explore|try)|"
        r"what\s+if|how\s+(?:does|would|could))\b", re.I),
     "curiosity", 0.3),
]

# Life-event patterns — things worth following up on.
_LIFE_EVENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:job\s+)?interview", re.I), "interview"),
    (re.compile(r"\b(?:new\s+job|got\s+(?:hired|the\s+job|an?\s+offer)|starting\s+(?:a\s+)?new\s+(?:role|position))\b", re.I), "new_job"),
    (re.compile(r"\b(?:present(?:ation|ing)|demo|pitch)\b", re.I), "presentation"),
    (re.compile(r"\b(?:exam|test|final|midterm)\b", re.I), "exam"),
    (re.compile(r"\b(?:wedding|engaged|getting\s+married)\b", re.I), "wedding"),
    (re.compile(r"\b(?:birthday|anniversary)\b", re.I), "celebration"),
    (re.compile(r"\b(?:moving|new\s+(?:house|apartment|place)|relocat)\b", re.I), "moving"),
    (re.compile(r"\b(?:vacation|holiday|trip|travel(?:ing|ling)?)\b", re.I), "travel"),
    (re.compile(r"\b(?:deadline|launch|release|ship(?:ping)?|go(?:ing)?\s+live)\b", re.I), "deadline"),
    (re.compile(r"\b(?:sick|ill|hospital|doctor|surgery|recover)\b", re.I), "health"),
]


class EmotionalSignal:
    """A detected emotional signal from a user message."""

    __slots__ = ("emotion", "valence", "intensity", "life_event", "context_snippet")

    def __init__(
        self,
        emotion: str,
        valence: float,
        intensity: float,
        life_event: str | None = None,
        context_snippet: str = "",
    ):
        self.emotion = emotion
        self.valence = valence        # -1.0 (negative) to +1.0 (positive)
        self.intensity = intensity    # 0.0 (mild) to 1.0 (strong)
        self.life_event = life_event
        self.context_snippet = context_snippet

    def to_dict(self) -> dict:
        d = {
            "emotion": self.emotion,
            "valence": self.valence,
            "intensity": self.intensity,
            "context": self.context_snippet,
        }
        if self.life_event:
            d["life_event"] = self.life_event
        return d


# ── Relationship levels ───────────────────────────────────────────

RELATIONSHIP_LEVELS = [
    {"level": 1, "label": "Just getting started", "threshold": 0.0},
    {"level": 2, "label": "Getting to know you",  "threshold": 0.20},
    {"level": 3, "label": "Building trust",        "threshold": 0.50},
    {"level": 4, "label": "Close companion",       "threshold": 0.80},
]

# Capabilities unlocked at each level (for display in Memory panel).
LEVEL_CAPABILITIES = {
    1: ["Basic assistance"],
    2: ["Personalized greetings", "Smart suggestions"],
    3: ["Follows up on your life events", "Remembers what matters"],
    4: ["Adapts tone to how you're feeling", "Proactive check-ins"],
}


class EmotionTracker:
    """Tracks emotional signals and computes relationship progression."""

    def __init__(self, memory_repo, session_repo):
        self._repo = memory_repo
        self._session_repo = session_repo
        # Running session state
        self._session_signals: list[EmotionalSignal] = []
        self._session_valence: float = 0.0  # running average

    def reset_session(self) -> None:
        """Clear session-level emotional state (called on new session)."""
        self._session_signals = []
        self._session_valence = 0.0

    # ── Real-time detection ───────────────────────────────────────

    def analyze_message(self, text: str) -> Optional[EmotionalSignal]:
        """Scan a user message for emotional signals.

        Returns the strongest signal found, or None if the message
        is emotionally neutral.  This is intentionally lightweight —
        no LLM call, just pattern matching.
        """
        best_signal: Optional[EmotionalSignal] = None
        best_intensity = 0.0

        for pattern, emotion, valence in _EMOTION_PATTERNS:
            matches = pattern.findall(text)
            if not matches:
                continue

            # More matches = higher intensity (capped at 1.0)
            intensity = min(1.0, len(matches) * 0.5)

            # Exclamation marks boost intensity
            excl_count = text.count("!")
            if excl_count > 0:
                intensity = min(1.0, intensity + excl_count * 0.1)

            # ALL CAPS boost intensity
            words = text.split()
            caps_ratio = sum(1 for w in words if w.isupper() and len(w) > 1) / max(len(words), 1)
            if caps_ratio > 0.3:
                intensity = min(1.0, intensity + 0.2)

            if intensity > best_intensity:
                best_intensity = intensity
                # Grab context snippet — the sentence containing the match
                snippet = text[:120].strip()
                best_signal = EmotionalSignal(
                    emotion=emotion,
                    valence=valence,
                    intensity=intensity,
                    context_snippet=snippet,
                )

        # Check for life events
        life_event = None
        for pattern, event_type in _LIFE_EVENT_PATTERNS:
            if pattern.search(text):
                life_event = event_type
                break

        if best_signal:
            best_signal.life_event = life_event
            self._update_session_mood(best_signal)
        elif life_event:
            # Life event without strong emotion — still worth tracking
            best_signal = EmotionalSignal(
                emotion="neutral",
                valence=0.0,
                intensity=0.3,
                life_event=life_event,
                context_snippet=text[:120].strip(),
            )
            self._update_session_mood(best_signal)

        return best_signal

    def _update_session_mood(self, signal: EmotionalSignal) -> None:
        """Update the running session mood with a new signal."""
        self._session_signals.append(signal)
        # Exponential moving average — recent signals weigh more
        alpha = 0.4
        self._session_valence = (
            alpha * signal.valence + (1 - alpha) * self._session_valence
        )

    def get_session_mood(self) -> dict:
        """Return the current session's emotional summary."""
        if not self._session_signals:
            return {"mood": "neutral", "valence": 0.0, "signals": 0}

        # Dominant emotion = most frequent
        from collections import Counter
        emotions = Counter(s.emotion for s in self._session_signals)
        dominant = emotions.most_common(1)[0][0]

        # Life events mentioned this session
        life_events = [
            s.life_event for s in self._session_signals if s.life_event
        ]

        return {
            "mood": dominant,
            "valence": round(self._session_valence, 2),
            "signals": len(self._session_signals),
            "life_events": list(set(life_events)),
        }

    # ── Persistence ───────────────────────────────────────────────

    async def persist_signal(self, signal: EmotionalSignal) -> None:
        """Store a significant emotional event to _emotions namespace.

        Only persists signals with intensity >= 0.5 or that have a
        life event attached.  This avoids cluttering memory with every
        mild signal.
        """
        if signal.intensity < 0.5 and not signal.life_event:
            return

        now = datetime.now(timezone.utc)
        key_parts = [signal.emotion, now.strftime("%Y%m%d_%H%M")]
        if signal.life_event:
            key_parts.insert(0, signal.life_event)
        key = "-".join(key_parts)

        # Build a natural-language value for display
        if signal.life_event:
            value = f"User mentioned {signal.life_event.replace('_', ' ')}"
            if signal.emotion != "neutral":
                value += f" (seemed {signal.emotion})"
            value += f": {signal.context_snippet}"
        else:
            value = f"User expressed {signal.emotion}: {signal.context_snippet}"

        await self._repo.put(
            namespace="_emotions",
            key=key,
            value=value,
            value_type="text",
        )

    # ── Relationship score ────────────────────────────────────────

    async def compute_relationship_score(self) -> dict:
        """Compute the relationship depth score from multiple signals.

        Returns:
            level: int (1-4)
            label: str
            progress: float (0.0-1.0) — progress within current level
            score: float (0.0-1.0) — raw composite score
            capabilities: list[str] — unlocked at current level
        """
        # Signal 1: Breadth — how many namespaces have content
        breadth_ns = ["_profile", "_facts", "_project", "_emotions",
                       "_conversation", "_patterns"]
        ns_with_content = 0
        for ns in breadth_ns:
            keys = await self._repo.list_keys(ns)
            if keys:
                ns_with_content += 1
        breadth = ns_with_content / len(breadth_ns)  # 0.0 - 1.0

        # Signal 2: Depth — personal/emotional content
        profile_keys = await self._repo.list_keys("_profile")
        emotion_keys = await self._repo.list_keys("_emotions")
        # More personal info = deeper relationship
        depth_score = min(1.0, (len(profile_keys) * 0.05) + (len(emotion_keys) * 0.1))

        # Signal 3: Consistency — active days in last 30
        try:
            session_stats = await self._session_repo.get_session_stats()
            total_sessions = session_stats.get("session_count", 0)
            first_at = session_stats.get("first_session_at")
            if first_at:
                first = datetime.fromisoformat(first_at)
                days_active = max(1, (datetime.now(timezone.utc) - first).days)
                # Ratio of sessions to days, capped at 1.0
                consistency = min(1.0, total_sessions / max(days_active, 1))
            else:
                consistency = 0.0
        except Exception:
            consistency = 0.0

        # Signal 4: Trust — memory count as proxy
        # (permissions are session-scoped; total memories is a better
        #  long-term trust indicator)
        total_memories = await self._repo.count_entries()
        trust = min(1.0, total_memories / 50)  # 50 memories = full trust signal

        # Composite score (weighted)
        score = (
            0.25 * breadth
            + 0.30 * depth
            + 0.20 * consistency
            + 0.25 * trust
        )
        score = min(1.0, max(0.0, score))

        # Determine level
        current_level = RELATIONSHIP_LEVELS[0]
        for lvl in RELATIONSHIP_LEVELS:
            if score >= lvl["threshold"]:
                current_level = lvl

        # Progress within level → next level
        next_levels = [l for l in RELATIONSHIP_LEVELS if l["threshold"] > current_level["threshold"]]
        if next_levels:
            next_lvl = next_levels[0]
            range_size = next_lvl["threshold"] - current_level["threshold"]
            progress = (score - current_level["threshold"]) / range_size if range_size > 0 else 1.0
        else:
            progress = 1.0  # max level

        caps = []
        for lvl in RELATIONSHIP_LEVELS:
            if lvl["level"] <= current_level["level"]:
                caps.extend(LEVEL_CAPABILITIES.get(lvl["level"], []))

        # Next unlocks
        next_caps = []
        if next_levels:
            next_caps = LEVEL_CAPABILITIES.get(next_levels[0]["level"], [])

        return {
            "level": current_level["level"],
            "label": current_level["label"],
            "progress": round(min(1.0, max(0.0, progress)), 2),
            "score": round(score, 3),
            "capabilities": caps,
            "next_capabilities": next_caps,
        }

    # ── Context for LLM ──────────────────────────────────────────

    async def get_emotional_context(self, level: int) -> str:
        """Build an emotional context string for LLM injection.

        The depth of context depends on the relationship level:
          Level 1-2: No emotional context
          Level 3: Recent life events only
          Level 4: Full emotional awareness
        """
        if level < 3:
            return ""

        parts = []

        # Recent emotional memories (last 10)
        try:
            entries = await self._repo.get_by_relevance(
                namespace="_emotions", limit=10, min_score=0.0
            )
        except Exception:
            entries = []

        if not entries:
            return ""

        if level >= 4:
            # Full emotional awareness
            mood = self.get_session_mood()
            if mood["signals"] > 0:
                parts.append(
                    f"Current session mood: {mood['mood']} "
                    f"(valence: {mood['valence']})"
                )

        # Life events to follow up on (level 3+)
        life_event_entries = [
            e for e in entries
            if any(kw in e["key"] for kw in [
                "interview", "job", "presentation", "exam",
                "wedding", "celebration", "moving", "travel",
                "deadline", "health",
            ])
        ]

        if life_event_entries:
            parts.append("Recent life events the user mentioned:")
            for e in life_event_entries[:5]:
                parts.append(f"  - {e['value']}")

        if level >= 4 and entries:
            recent_emotions = [e for e in entries if e not in life_event_entries][:5]
            if recent_emotions:
                parts.append("Recent emotional signals:")
                for e in recent_emotions:
                    parts.append(f"  - {e['value']}")

        if not parts:
            return ""

        header = (
            "Emotional context (use this to be attentive — follow up on "
            "life events naturally, don't label emotions directly):"
        )
        return header + "\n" + "\n".join(parts)
