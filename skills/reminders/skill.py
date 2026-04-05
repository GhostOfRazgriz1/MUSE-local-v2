"""Reminders skill — set, list, and manage reminders."""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _friendly_when(when: str) -> str:
    """Convert an ISO 8601 timestamp to a human-friendly string."""
    if not when or when == "unspecified":
        return ""
    try:
        dt = datetime.fromisoformat(when)
        now = datetime.now(timezone.utc)
        diff = dt - now
        # Past
        if diff.total_seconds() < 0:
            return dt.strftime("%b %d at %I:%M %p")
        # Within the next hour
        mins = int(diff.total_seconds() / 60)
        if mins < 60:
            return f"in {mins} minute{'s' if mins != 1 else ''}"
        # Within today
        hours = int(diff.total_seconds() / 3600)
        if hours < 24:
            return f"in {hours} hour{'s' if hours != 1 else ''}"
        # Further out — show the date
        return dt.strftime("%b %d at %I:%M %p")
    except (ValueError, TypeError):
        return when


async def run(ctx) -> dict:
    """Entry point for the Reminders skill."""
    instruction = ctx.brief.get("instruction", "")
    lower = instruction.lower()

    if any(w in lower for w in ["set", "remind", "create", "add", "schedule"]):
        return await _set_reminder(ctx, instruction)
    elif any(w in lower for w in ["list", "show", "all reminders", "upcoming"]):
        return await _list_reminders(ctx)
    elif any(w in lower for w in ["delete", "remove", "cancel", "clear"]):
        return await _delete_reminder(ctx, instruction)
    else:
        return await _set_reminder(ctx, instruction)


def _parse_relative_time(instruction: str) -> tuple[str | None, str | None]:
    """Try to parse relative times like 'in 5 minutes' without LLM.

    Returns (when_iso, what) or (None, None) if not a simple relative time.
    """
    import re
    from datetime import timedelta

    m = re.search(
        r"in\s+(\d+)\s+(minute|min|hour|hr|second|sec|day)s?",
        instruction, re.IGNORECASE,
    )
    if not m:
        return None, None

    amount = int(m.group(1))
    unit = m.group(2).lower()

    if unit in ("minute", "min"):
        delta = timedelta(minutes=amount)
    elif unit in ("hour", "hr"):
        delta = timedelta(hours=amount)
    elif unit in ("second", "sec"):
        delta = timedelta(seconds=amount)
    elif unit == "day":
        delta = timedelta(days=amount)
    else:
        return None, None

    # Use local time so display makes sense to the user
    when = (datetime.now().astimezone() + delta).isoformat()

    # Extract the "what" — strip prefixes and the time suffix
    what = re.sub(
        r"^\s*(?:can\s+you\s+|please\s+|could\s+you\s+)?"
        r"(?:remind\s+me\s+to\s+|remind\s+me\s+|set\s+a\s+reminder\s+to\s+)?",
        "", instruction, flags=re.IGNORECASE,
    ).strip()
    what = re.sub(
        r"\s+in\s+\d+\s+(?:minute|min|hour|hr|second|sec|day)s?\s*[?.!]*$",
        "", what, flags=re.IGNORECASE,
    ).strip()

    return when, what or instruction


async def _set_reminder(ctx, instruction: str) -> dict:
    """Set a new reminder."""
    now_local = datetime.now().astimezone().isoformat()

    # Use regex only for precise time calculation (not for "what" extraction)
    when_from_regex, _ = _parse_relative_time(instruction)
    friendly_override = None

    # Capture the original relative time phrase for display
    import re as _re
    m = _re.search(r"in\s+(\d+)\s+(minute|min|hour|hr|second|sec|day)s?", instruction, _re.IGNORECASE)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        unit_display = {"minute": "minute", "min": "minute", "hour": "hour",
                        "hr": "hour", "second": "second", "sec": "second", "day": "day"}.get(unit, unit)
        friendly_override = f"in {amount} {unit_display}{'s' if amount != 1 else ''}"

    # Always use LLM for "what" extraction — regex is fragile on natural language
    result = await ctx.llm.complete(
        prompt=f"Extract the reminder details. Current time: {now_local}\n\n"
               f"Request: {instruction}\n\n"
               f"JSON: {{\"what\": \"short description of what to remember\", "
               f"\"when\": \"ISO 8601 datetime or 'unspecified'\", "
               f"\"recurring\": false}}",
        system="Extract structured reminder data. Reply with ONLY valid JSON.",
    )

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        parsed = {"what": instruction, "when": "unspecified", "recurring": False}

    what = parsed.get("what", instruction)
    recurring = parsed.get("recurring", False)

    # Use regex-computed time if available (more precise), else LLM's time
    when = when_from_regex or parsed.get("when", "unspecified")

    key = f"reminder.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    reminder = json.dumps({
        "what": what,
        "when": when,
        "recurring": recurring,
        "created_at": now_local,
        "status": "active",
    })

    await ctx.memory.write(key, reminder, value_type="json")

    # Use the original phrase for display, not recalculated relative time
    time_str = f" ({friendly_override})" if friendly_override else ""
    if not time_str:
        friendly = _friendly_when(when)
        time_str = f" ({friendly})" if friendly else ""

    return {
        "payload": {"key": key, "what": what, "when": when},
        "summary": f"Reminder set{time_str}: \"{what}\"",
        "success": True,
    }


async def _list_reminders(ctx) -> dict:
    """List all active reminders."""
    keys = await ctx.memory.list_keys("reminder.")

    if not keys:
        return {
            "payload": {"reminders": []},
            "summary": "You don't have any reminders.",
            "success": True,
        }

    reminders = []
    for key in keys:
        value = await ctx.memory.read(key)
        if value:
            try:
                r = json.loads(value)
                if r.get("status") == "active":
                    reminders.append(r)
            except json.JSONDecodeError:
                reminders.append({"what": value, "when": "unspecified"})

    if not reminders:
        return {
            "payload": {"reminders": []},
            "summary": "No active reminders.",
            "success": True,
        }

    lines = []
    for r in reminders:
        friendly = _friendly_when(r.get("when", ""))
        time_str = f" ({friendly})" if friendly else ""
        lines.append(f"- {r.get('what', 'Unknown')}{time_str}")

    return {
        "payload": {"reminders": reminders},
        "summary": f"Active reminders:\n" + "\n".join(lines),
        "success": True,
    }


async def _delete_reminder(ctx, instruction: str) -> dict:
    """Delete a reminder."""
    results = await ctx.memory.search(instruction, limit=1)
    if results:
        # Mark as cancelled rather than deleting
        try:
            data = json.loads(results[0].value)
            data["status"] = "cancelled"
            await ctx.memory.write(results[0].key, json.dumps(data), value_type="json")
            return {
                "payload": {"cancelled": results[0].key},
                "summary": f"Cancelled reminder: {data.get('what', results[0].key)}",
                "success": True,
            }
        except (json.JSONDecodeError, AttributeError):
            await ctx.memory.delete(results[0].key)
            return {
                "payload": {"deleted": results[0].key},
                "summary": f"Deleted reminder.",
                "success": True,
            }

    return {"payload": None, "summary": "Reminder not found.", "success": True}
