"""Email skill — read, search, and send emails via Gmail.

Operations
----------
- **inbox** / **list** — show recent inbox messages
- **read**            — read a specific email
- **send** / **compose** / **write** — compose and send an email (high-risk)
- **search**          — search emails by query

OAuth credentials are read from the vault (credential_id: google_oauth).
The ``email:send`` permission is classified as high-risk by the permission
system, requiring explicit user consent each time.
"""

from __future__ import annotations

import base64
import json
import uuid
from email.mime.text import MIMEText

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
CREDENTIAL_ID = "google_oauth"


# ── Entry point ─────────────────────────────────────────────────────


async def run(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    lower = instruction.lower()

    token = await _get_access_token(ctx)
    if token is None:
        return _needs_setup()

    if any(w in lower for w in ["send", "compose", "write", "draft", "reply"]):
        return await _send_email(ctx, instruction, token)
    if any(w in lower for w in ["read", "open", "show message", "show email"]):
        return await _read_email(ctx, instruction, token)
    if any(w in lower for w in ["search", "find email", "look for"]):
        return await _search_emails(ctx, instruction, token)
    # Default: list inbox
    return await _list_inbox(ctx, instruction, token)


# ── Operations ──────────────────────────────────────────────────────


async def _list_inbox(ctx, instruction: str, token: str) -> dict:
    """List recent inbox messages."""
    await ctx.task.report_status("Fetching inbox…")
    resp = await ctx.http.get(
        f"{GMAIL_API}/messages?maxResults=10&labelIds=INBOX",
        headers=_auth_headers(token),
    )

    if resp.status_code == 401:
        return _auth_expired()
    if resp.status_code != 200:
        return _err(f"Gmail API error (HTTP {resp.status_code})")

    data = resp.json()
    message_ids = [m["id"] for m in data.get("messages", [])]

    if not message_ids:
        return {
            "payload": {"messages": []},
            "summary": "Your inbox is empty.",
            "success": True,
        }

    # Fetch headers for each message
    messages = []
    for msg_id in message_ids[:10]:
        msg = await _fetch_message_headers(ctx, token, msg_id)
        if msg:
            messages.append(msg)

    lines = []
    for m in messages:
        sender = m.get("from", "Unknown")
        subject = m.get("subject", "(no subject)")
        date = m.get("date", "")
        snippet = m.get("snippet", "")[:80]
        lines.append(f"- **{subject}**\n  From: {sender} — {date}\n  {snippet}")

    return {
        "payload": {"messages": messages},
        "summary": f"**Inbox ({len(messages)} recent):**\n\n" + "\n\n".join(lines),
        "success": True,
    }


async def _read_email(ctx, instruction: str, token: str) -> dict:
    """Read a specific email by searching for it."""
    query = await ctx.llm.complete(
        prompt=f"Extract a short email search query from:\n\n{instruction}",
        system="Return only the search keywords. If a sender or subject is mentioned, include that.",
    )
    query = query.strip().strip('"\'')

    resp = await ctx.http.get(
        f"{GMAIL_API}/messages?q={_url_encode(query)}&maxResults=1",
        headers=_auth_headers(token),
    )

    if resp.status_code == 401:
        return _auth_expired()
    if resp.status_code != 200:
        return _err(f"Gmail API error (HTTP {resp.status_code})")

    data = resp.json()
    ids = [m["id"] for m in data.get("messages", [])]
    if not ids:
        return _err(f"No email found matching \"{query}\".")

    # Fetch full message
    msg_resp = await ctx.http.get(
        f"{GMAIL_API}/messages/{ids[0]}?format=full",
        headers=_auth_headers(token),
    )
    if msg_resp.status_code != 200:
        return _err(f"Failed to read message (HTTP {msg_resp.status_code})")

    msg_data = msg_resp.json()
    headers = _extract_headers(msg_data)
    body = _extract_body(msg_data)

    summary_text = (
        f"**{headers.get('subject', '(no subject)')}**\n\n"
        f"From: {headers.get('from', 'Unknown')}\n"
        f"Date: {headers.get('date', '')}\n"
        f"To: {headers.get('to', '')}\n\n"
        f"---\n\n{body}"
    )

    return {
        "payload": {"id": ids[0], "headers": headers, "body": body},
        "summary": summary_text,
        "success": True,
    }


async def _send_email(ctx, instruction: str, token: str) -> dict:
    """Compose and send an email (requires email:send permission)."""
    # Use LLM to extract email components
    parsed = await ctx.llm.complete(
        prompt=(
            f"Extract email details from this request:\n\n{instruction}\n\n"
            f"Respond with JSON:\n"
            f'{{"to": "recipient@example.com", '
            f'"subject": "email subject", '
            f'"body": "email body text"}}'
        ),
        system=(
            "Extract structured email data. Respond only with valid JSON. "
            "If the body isn't specified, draft a professional message based on context."
        ),
    )

    try:
        email_data = json.loads(parsed)
    except json.JSONDecodeError:
        return _err("Could not parse email details from your request.")

    to_addr = email_data.get("to", "")
    subject = email_data.get("subject", "")
    body = email_data.get("body", "")

    if not to_addr:
        to_addr = await ctx.user.ask("Who should I send this email to? (email address)")
        if not to_addr or not to_addr.strip():
            return _err("No recipient provided.")
        to_addr = to_addr.strip()

    # Show draft and confirm (email:send is high-risk)
    draft_preview = (
        f"**To:** {to_addr}\n"
        f"**Subject:** {subject}\n\n"
        f"---\n{body}\n---"
    )

    confirmed = await ctx.user.confirm(
        f"Send this email?\n\n{draft_preview}"
    )
    if not confirmed:
        return {"payload": None, "summary": "Email sending cancelled.", "success": True}

    # Build MIME message
    mime = MIMEText(body)
    mime["to"] = to_addr
    mime["subject"] = subject
    raw_message = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    await ctx.task.report_status(f"Sending email to {to_addr}…")
    resp = await ctx.http.post(
        f"{GMAIL_API}/messages/send",
        body={"raw": raw_message},
        headers=_auth_headers(token),
    )

    if resp.status_code == 401:
        return _auth_expired()
    if resp.status_code not in (200, 201):
        return _err(f"Failed to send email (HTTP {resp.status_code})")

    sent = resp.json()
    return {
        "payload": {"id": sent.get("id"), "to": to_addr, "subject": subject},
        "summary": f"Email sent to **{to_addr}**: \"{subject}\"",
        "success": True,
        "facts": [{
            "key": f"email.sent.{sent.get('id', 'new')}",
            "value": json.dumps({"to": to_addr, "subject": subject}),
            "namespace": "email",
        }],
    }


async def _search_emails(ctx, instruction: str, token: str) -> dict:
    """Search emails by query."""
    query = await ctx.llm.complete(
        prompt=f"Extract a Gmail search query from:\n\n{instruction}",
        system=(
            "Return only the Gmail search query. Use Gmail syntax if appropriate "
            "(e.g. from:, subject:, after:). Keep it concise."
        ),
    )
    query = query.strip().strip('"\'')

    await ctx.task.report_status(f"Searching emails: {query}")
    resp = await ctx.http.get(
        f"{GMAIL_API}/messages?q={_url_encode(query)}&maxResults=10",
        headers=_auth_headers(token),
    )

    if resp.status_code == 401:
        return _auth_expired()
    if resp.status_code != 200:
        return _err(f"Gmail API error (HTTP {resp.status_code})")

    data = resp.json()
    message_ids = [m["id"] for m in data.get("messages", [])]

    if not message_ids:
        return {
            "payload": {"messages": [], "query": query},
            "summary": f"No emails found matching \"{query}\".",
            "success": True,
        }

    messages = []
    for msg_id in message_ids[:10]:
        msg = await _fetch_message_headers(ctx, token, msg_id)
        if msg:
            messages.append(msg)

    lines = []
    for m in messages:
        sender = m.get("from", "Unknown")
        subject = m.get("subject", "(no subject)")
        date = m.get("date", "")
        lines.append(f"- **{subject}**  —  {sender} ({date})")

    return {
        "payload": {"messages": messages, "query": query},
        "summary": (
            f"**Emails matching \"{query}\" ({len(messages)}):**\n\n"
            + "\n".join(lines)
        ),
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


async def _fetch_message_headers(ctx, token: str, msg_id: str) -> dict | None:
    """Fetch a message's metadata (headers + snippet)."""
    resp = await ctx.http.get(
        f"{GMAIL_API}/messages/{msg_id}?format=metadata"
        "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date&metadataHeaders=To",
        headers=_auth_headers(token),
    )
    if resp.status_code != 200:
        return None

    data = resp.json()
    headers = _extract_headers(data)
    headers["id"] = msg_id
    headers["snippet"] = data.get("snippet", "")
    return headers


def _extract_headers(msg_data: dict) -> dict:
    """Pull common headers into a flat dict."""
    result = {}
    for header in msg_data.get("payload", {}).get("headers", []):
        name = header.get("name", "").lower()
        if name in ("from", "to", "subject", "date"):
            result[name] = header.get("value", "")
    return result


def _extract_body(msg_data: dict) -> str:
    """Extract the plain-text body from a Gmail message."""
    payload = msg_data.get("payload", {})

    # Simple single-part message
    if payload.get("body", {}).get("data"):
        return _decode_body(payload["body"]["data"])

    # Multipart — look for text/plain
    for part in payload.get("parts", []):
        mime = part.get("mimeType", "")
        if mime == "text/plain" and part.get("body", {}).get("data"):
            return _decode_body(part["body"]["data"])

    # Fallback to snippet
    return msg_data.get("snippet", "(could not extract message body)")


def _decode_body(data: str) -> str:
    """Decode base64url-encoded Gmail body data."""
    padded = data + "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _url_encode(text: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(text)


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _needs_setup() -> dict:
    return _err(
        "Gmail is not connected. To set it up:\n\n"
        "1. Go to **Settings > Credentials**\n"
        "2. Add your Google OAuth client ID and secret\n"
        "3. Start the OAuth flow from Settings\n\n"
        "This connects MUSE to your Gmail securely."
    )


def _auth_expired() -> dict:
    return _err(
        "Gmail authorization has expired. "
        "Please re-authorize in **Settings > Credentials**."
    )


def _err(message: str) -> dict:
    return {"payload": None, "summary": message, "success": False, "error": message}
