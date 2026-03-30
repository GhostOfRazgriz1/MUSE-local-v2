"""Calendar skill — view and manage Google Calendar events.

Operations
----------
- **list** / **upcoming** — show upcoming events
- **create** — create a new calendar event
- **search** — find events matching a query
- **delete** / **cancel** — remove a calendar event

OAuth credentials are read from the vault (credential_id: google_oauth).
If Google OAuth is not configured, the skill prompts the user to set it up.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlencode

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
CREDENTIAL_ID = "google_oauth"


# ── Entry point ─────────────────────────────────────────────────────


async def run(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    lower = instruction.lower()

    token = await _get_access_token(ctx)
    if token is None:
        return _needs_setup()

    if any(w in lower for w in ["create", "add", "schedule", "new event", "book"]):
        return await _create_event(ctx, instruction, token)
    if any(w in lower for w in ["delete", "remove", "cancel"]):
        return await _delete_event(ctx, instruction, token)
    if any(w in lower for w in ["search", "find event", "look for"]):
        return await _search_events(ctx, instruction, token)
    # Default: list upcoming
    return await _list_events(ctx, instruction, token)


# ── Operations ──────────────────────────────────────────────────────


async def _list_events(ctx, instruction: str, token: str) -> dict:
    """List upcoming calendar events."""
    now = datetime.now(timezone.utc).isoformat()
    params = urlencode({
        "timeMin": now,
        "maxResults": 10,
        "orderBy": "startTime",
        "singleEvents": "true",
    })

    await ctx.task.report_status("Fetching upcoming events…")
    resp = await ctx.http.get(
        f"{CALENDAR_API}/calendars/primary/events?{params}",
        headers=_auth_headers(token),
    )

    if resp.status_code == 401:
        return _auth_expired()
    if resp.status_code != 200:
        return _err(f"Calendar API error (HTTP {resp.status_code})")

    data = resp.json()
    events = data.get("items", [])

    if not events:
        return {
            "payload": {"events": []},
            "summary": "You have no upcoming events.",
            "success": True,
        }

    lines = _format_events(events)
    summary = f"**Upcoming events ({len(events)}):**\n" + "\n".join(lines)

    return {
        "payload": {"events": events},
        "summary": summary,
        "success": True,
    }


async def _create_event(ctx, instruction: str, token: str) -> dict:
    """Create a new calendar event using LLM to parse the request."""
    now = datetime.now(timezone.utc).isoformat()
    parsed = await ctx.llm.complete(
        prompt=(
            f"Extract event details from this request. Current time: {now}\n\n"
            f"Request: {instruction}\n\n"
            f"Respond with JSON:\n"
            f'{{"summary": "event title", '
            f'"start": "ISO 8601 datetime", '
            f'"end": "ISO 8601 datetime (1h after start if not specified)", '
            f'"description": "optional details", '
            f'"location": "optional location"}}'
        ),
        system="Extract structured calendar event data. Respond only with valid JSON.",
    )

    try:
        event_data = json.loads(parsed)
    except json.JSONDecodeError:
        return _err("Could not parse event details from your request.")

    body = {
        "summary": event_data.get("summary", "New Event"),
        "start": {"dateTime": event_data.get("start"), "timeZone": "UTC"},
        "end": {"dateTime": event_data.get("end"), "timeZone": "UTC"},
    }
    if event_data.get("description"):
        body["description"] = event_data["description"]
    if event_data.get("location"):
        body["location"] = event_data["location"]

    confirmed = await ctx.user.confirm(
        f"Create event **{body['summary']}** on "
        f"{event_data.get('start', '?')}?"
    )
    if not confirmed:
        return {"payload": None, "summary": "Event creation cancelled.", "success": True}

    await ctx.task.report_status(f"Creating event: {body['summary']}")
    resp = await ctx.http.post(
        f"{CALENDAR_API}/calendars/primary/events",
        body=body,
        headers=_auth_headers(token),
    )

    if resp.status_code == 401:
        return _auth_expired()
    if resp.status_code not in (200, 201):
        return _err(f"Failed to create event (HTTP {resp.status_code})")

    created = resp.json()
    link = created.get("htmlLink", "")
    summary_text = (
        f"Event created: **{created.get('summary', body['summary'])}**"
    )
    if link:
        summary_text += f"\n[Open in Google Calendar]({link})"

    return {
        "payload": created,
        "summary": summary_text,
        "success": True,
        "facts": [{
            "key": f"event.{created.get('id', 'new')}",
            "value": json.dumps({
                "summary": created.get("summary"),
                "start": event_data.get("start"),
                "end": event_data.get("end"),
            }),
            "namespace": "calendar",
        }],
    }


async def _search_events(ctx, instruction: str, token: str) -> dict:
    """Search calendar events by keyword."""
    query = await ctx.llm.complete(
        prompt=f"Extract a short search query from this request:\n\n{instruction}",
        system="Return only the search keywords, nothing else.",
    )
    query = query.strip().strip('"\'')

    now = datetime.now(timezone.utc)
    params = urlencode({
        "q": query,
        "timeMin": (now - timedelta(days=90)).isoformat(),
        "timeMax": (now + timedelta(days=365)).isoformat(),
        "maxResults": 10,
        "singleEvents": "true",
        "orderBy": "startTime",
    })

    await ctx.task.report_status(f"Searching events: {query}")
    resp = await ctx.http.get(
        f"{CALENDAR_API}/calendars/primary/events?{params}",
        headers=_auth_headers(token),
    )

    if resp.status_code == 401:
        return _auth_expired()
    if resp.status_code != 200:
        return _err(f"Calendar API error (HTTP {resp.status_code})")

    data = resp.json()
    events = data.get("items", [])

    if not events:
        return {
            "payload": {"events": [], "query": query},
            "summary": f"No events found matching \"{query}\".",
            "success": True,
        }

    lines = _format_events(events)
    return {
        "payload": {"events": events, "query": query},
        "summary": f"**Events matching \"{query}\":**\n" + "\n".join(lines),
        "success": True,
    }


async def _delete_event(ctx, instruction: str, token: str) -> dict:
    """Delete/cancel a calendar event."""
    # First search for the event
    query = await ctx.llm.complete(
        prompt=f"Extract a short search query from this request:\n\n{instruction}",
        system="Return only the search keywords, nothing else.",
    )
    query = query.strip().strip('"\'')

    now = datetime.now(timezone.utc)
    params = urlencode({
        "q": query,
        "timeMin": (now - timedelta(days=30)).isoformat(),
        "timeMax": (now + timedelta(days=365)).isoformat(),
        "maxResults": 5,
        "singleEvents": "true",
        "orderBy": "startTime",
    })

    resp = await ctx.http.get(
        f"{CALENDAR_API}/calendars/primary/events?{params}",
        headers=_auth_headers(token),
    )

    if resp.status_code == 401:
        return _auth_expired()
    if resp.status_code != 200:
        return _err(f"Calendar API error (HTTP {resp.status_code})")

    events = resp.json().get("items", [])
    if not events:
        return _err(f"No events found matching \"{query}\".")

    event = events[0]
    event_name = event.get("summary", "Untitled")

    confirmed = await ctx.user.confirm(
        f"Delete event **{event_name}**?"
    )
    if not confirmed:
        return {"payload": None, "summary": "Deletion cancelled.", "success": True}

    await ctx.task.report_status(f"Deleting event: {event_name}")
    del_resp = await ctx.http.delete(
        f"{CALENDAR_API}/calendars/primary/events/{event['id']}",
        headers=_auth_headers(token),
    )

    if del_resp.status_code == 401:
        return _auth_expired()
    if del_resp.status_code not in (200, 204):
        return _err(f"Failed to delete event (HTTP {del_resp.status_code})")

    return {
        "payload": {"deleted": event["id"], "summary": event_name},
        "summary": f"Deleted event: **{event_name}**",
        "success": True,
    }


# ── Helpers ─────────────────────────────────────────────────────────


async def _get_access_token(ctx) -> str | None:
    """Read the Google OAuth token from the credential vault."""
    from muse_sdk.ipc_client import CredentialReadMsg

    request_id = str(uuid.uuid4())
    await ctx._ipc.send(CredentialReadMsg(
        request_id=request_id,
        credential_id=CREDENTIAL_ID,
    ))
    resp = await ctx._ipc.receive()

    if not resp.success or not resp.value:
        return None

    try:
        data = json.loads(resp.value)
        return data.get("access_token")
    except json.JSONDecodeError:
        return resp.value


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _format_events(events: list) -> list[str]:
    lines = []
    for e in events:
        start = e.get("start", {})
        dt = start.get("dateTime", start.get("date", ""))
        if "T" in dt:
            try:
                parsed = datetime.fromisoformat(dt)
                dt = parsed.strftime("%b %d, %Y %I:%M %p")
            except ValueError:
                pass
        summary = e.get("summary", "Untitled")
        location = e.get("location", "")
        loc_str = f" — {location}" if location else ""
        lines.append(f"- **{summary}** ({dt}){loc_str}")
    return lines


def _needs_setup() -> dict:
    return _err(
        "Google Calendar is not connected. To set it up:\n\n"
        "1. Go to **Settings > Credentials**\n"
        "2. Add your Google OAuth client ID and secret\n"
        "3. Start the OAuth flow from Settings\n\n"
        "This connects MUSE to your Google Calendar securely."
    )


def _auth_expired() -> dict:
    return _err(
        "Google Calendar authorization has expired. "
        "Please re-authorize in **Settings > Credentials**."
    )


def _err(message: str) -> dict:
    return {"payload": None, "summary": message, "success": False, "error": message}
