"""Email skill — read, search, and send emails via Gmail or Outlook.

Operations
----------
- **inbox** / **list** — show recent inbox messages
- **read**            — read a specific email
- **send** / **compose** / **write** — compose and send an email (high-risk)
- **search**          — search emails by query

Supports two providers:
- Gmail (Google OAuth, credential_id: google_oauth)
- Outlook (Microsoft OAuth via Graph API, credential_id: microsoft_oauth)

The skill auto-detects which provider is configured.
"""

from __future__ import annotations

import base64
import json
import uuid
from email.mime.text import MIMEText

# ── Provider constants ─────────────────────────────────────────────

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
GRAPH_API = "https://graph.microsoft.com/v1.0/me"


# ── Entry point ────────────────────────────────────────────────────


async def run(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    lower = instruction.lower()

    provider, token = await _detect_provider(ctx)
    if provider is None:
        return _needs_setup()

    if any(w in lower for w in ["send", "compose", "write", "draft", "reply"]):
        return await _send_email(ctx, instruction, token, provider)
    if any(w in lower for w in ["read", "open", "show message", "show email"]):
        return await _read_email(ctx, instruction, token, provider)
    if any(w in lower for w in ["search", "find email", "look for"]):
        return await _search_emails(ctx, instruction, token, provider)
    return await _list_inbox(ctx, instruction, token, provider)


# ── Provider detection ─────────────────────────────────────────────


async def _detect_provider(ctx) -> tuple[str | None, str | None]:
    """Try each OAuth provider and return (provider_name, access_token)."""
    for cred_id, name in [("google_oauth", "gmail"), ("microsoft_oauth", "outlook")]:
        token = await _get_access_token(ctx, cred_id)
        if token:
            return name, token
    return None, None


async def _get_access_token(ctx, credential_id: str) -> str | None:
    """Read an OAuth token from the credential vault."""
    from muse_sdk.ipc_client import CredentialReadMsg

    request_id = str(uuid.uuid4())
    await ctx._ipc.send(CredentialReadMsg(
        request_id=request_id,
        credential_id=credential_id,
    ))
    resp = await ctx._ipc.receive()

    if not resp.success or not resp.value:
        return None

    try:
        data = json.loads(resp.value)
        return data.get("access_token")
    except json.JSONDecodeError:
        return resp.value


# ── List inbox ─────────────────────────────────────────────────────


async def _list_inbox(ctx, instruction: str, token: str, provider: str) -> dict:
    await ctx.task.report_status("Fetching inbox...")

    if provider == "gmail":
        resp = await ctx.http.get(
            f"{GMAIL_API}/messages?maxResults=10&labelIds=INBOX",
            headers=_auth(token),
        )
        if resp.status_code == 401:
            return _auth_expired()
        if resp.status_code != 200:
            return _err(f"Gmail API error (HTTP {resp.status_code})")

        data = resp.json()
        message_ids = [m["id"] for m in data.get("messages", [])]
        if not message_ids:
            return {"payload": {"messages": []}, "summary": "Your inbox is empty.", "success": True}

        messages = []
        for msg_id in message_ids[:10]:
            msg = await _fetch_gmail_headers(ctx, token, msg_id)
            if msg:
                messages.append(msg)
    else:
        resp = await ctx.http.get(
            f"{GRAPH_API}/mailFolders/inbox/messages?$top=10&$select=id,subject,from,receivedDateTime,bodyPreview",
            headers=_auth(token),
        )
        if resp.status_code == 401:
            return _auth_expired()
        if resp.status_code != 200:
            return _err(f"Outlook API error (HTTP {resp.status_code})")

        data = resp.json()
        messages = []
        for m in data.get("value", []):
            messages.append({
                "id": m.get("id", ""),
                "from": m.get("from", {}).get("emailAddress", {}).get("address", "Unknown"),
                "subject": m.get("subject", "(no subject)"),
                "date": m.get("receivedDateTime", ""),
                "snippet": (m.get("bodyPreview") or "")[:80],
            })

    if not messages:
        return {"payload": {"messages": []}, "summary": "Your inbox is empty.", "success": True}

    lines = []
    for m in messages:
        lines.append(
            f"- **{m.get('subject', '(no subject)')}**\n"
            f"  From: {m.get('from', 'Unknown')} — {m.get('date', '')}\n"
            f"  {m.get('snippet', '')}"
        )

    return {
        "payload": {"messages": messages},
        "summary": f"**Inbox ({len(messages)} recent):**\n\n" + "\n\n".join(lines),
        "success": True,
    }


# ── Read email ─────────────────────────────────────────────────────


async def _read_email(ctx, instruction: str, token: str, provider: str) -> dict:
    query = await ctx.llm.complete(
        prompt=f"Extract a short email search query from:\n\n{instruction}",
        system="Return only the search keywords.",
    )
    query = query.strip().strip('"\'')

    if provider == "gmail":
        resp = await ctx.http.get(
            f"{GMAIL_API}/messages?q={_url_encode(query)}&maxResults=1",
            headers=_auth(token),
        )
        if resp.status_code == 401:
            return _auth_expired()
        if resp.status_code != 200:
            return _err(f"Gmail API error (HTTP {resp.status_code})")

        data = resp.json()
        ids = [m["id"] for m in data.get("messages", [])]
        if not ids:
            return _err(f'No email found matching "{query}".')

        msg_resp = await ctx.http.get(
            f"{GMAIL_API}/messages/{ids[0]}?format=full",
            headers=_auth(token),
        )
        if msg_resp.status_code != 200:
            return _err(f"Failed to read message (HTTP {msg_resp.status_code})")

        msg_data = msg_resp.json()
        headers = _extract_gmail_headers(msg_data)
        body = _extract_gmail_body(msg_data)
    else:
        resp = await ctx.http.get(
            f"{GRAPH_API}/messages?$search=\"{_url_encode(query)}\"&$top=1&$select=id,subject,from,receivedDateTime,body,toRecipients",
            headers=_auth(token),
        )
        if resp.status_code == 401:
            return _auth_expired()
        if resp.status_code != 200:
            return _err(f"Outlook API error (HTTP {resp.status_code})")

        msgs = resp.json().get("value", [])
        if not msgs:
            return _err(f'No email found matching "{query}".')

        m = msgs[0]
        headers = {
            "from": m.get("from", {}).get("emailAddress", {}).get("address", "Unknown"),
            "subject": m.get("subject", "(no subject)"),
            "date": m.get("receivedDateTime", ""),
            "to": ", ".join(
                r.get("emailAddress", {}).get("address", "")
                for r in m.get("toRecipients", [])
            ),
        }
        body = m.get("body", {}).get("content", "(could not extract body)")

    summary = (
        f"**{headers.get('subject', '(no subject)')}**\n\n"
        f"From: {headers.get('from', 'Unknown')}\n"
        f"Date: {headers.get('date', '')}\n"
        f"To: {headers.get('to', '')}\n\n"
        f"---\n\n{body}"
    )

    return {"payload": {"headers": headers, "body": body}, "summary": summary, "success": True}


# ── Send email ─────────────────────────────────────────────────────


async def _send_email(ctx, instruction: str, token: str, provider: str) -> dict:
    parsed = await ctx.llm.complete(
        prompt=(
            f"Extract email details from this request:\n\n{instruction}\n\n"
            f"Respond with JSON:\n"
            f'{{"to": "recipient@example.com", "subject": "email subject", "body": "email body text"}}'
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

    draft_preview = f"**To:** {to_addr}\n**Subject:** {subject}\n\n---\n{body}\n---"
    confirmed = await ctx.user.confirm(f"Send this email?\n\n{draft_preview}")
    if not confirmed:
        return {"payload": None, "summary": "Email sending cancelled.", "success": True}

    if provider == "gmail":
        mime = MIMEText(body)
        mime["to"] = to_addr
        mime["subject"] = subject
        raw_message = base64.urlsafe_b64encode(mime.as_bytes()).decode()

        await ctx.task.report_status(f"Sending email to {to_addr}...")
        resp = await ctx.http.post(
            f"{GMAIL_API}/messages/send",
            body={"raw": raw_message},
            headers=_auth(token),
        )
    else:
        await ctx.task.report_status(f"Sending email to {to_addr}...")
        resp = await ctx.http.post(
            f"{GRAPH_API}/sendMail",
            body={
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [
                        {"emailAddress": {"address": to_addr}}
                    ],
                }
            },
            headers=_auth(token),
        )

    if resp.status_code == 401:
        return _auth_expired()
    if resp.status_code not in (200, 201, 202):
        return _err(f"Failed to send email (HTTP {resp.status_code})")

    return {
        "payload": {"to": to_addr, "subject": subject},
        "summary": f'Email sent to **{to_addr}**: "{subject}"',
        "success": True,
    }


# ── Search emails ──────────────────────────────────────────────────


async def _search_emails(ctx, instruction: str, token: str, provider: str) -> dict:
    query = await ctx.llm.complete(
        prompt=f"Extract an email search query from:\n\n{instruction}",
        system="Return only the search keywords. Keep it concise.",
    )
    query = query.strip().strip('"\'')

    await ctx.task.report_status(f"Searching emails: {query}")

    if provider == "gmail":
        resp = await ctx.http.get(
            f"{GMAIL_API}/messages?q={_url_encode(query)}&maxResults=10",
            headers=_auth(token),
        )
        if resp.status_code == 401:
            return _auth_expired()
        if resp.status_code != 200:
            return _err(f"Gmail API error (HTTP {resp.status_code})")

        data = resp.json()
        message_ids = [m["id"] for m in data.get("messages", [])]
        if not message_ids:
            return {"payload": {"messages": [], "query": query}, "summary": f'No emails found matching "{query}".', "success": True}

        messages = []
        for msg_id in message_ids[:10]:
            msg = await _fetch_gmail_headers(ctx, token, msg_id)
            if msg:
                messages.append(msg)
    else:
        resp = await ctx.http.get(
            f"{GRAPH_API}/messages?$search=\"{_url_encode(query)}\"&$top=10&$select=id,subject,from,receivedDateTime",
            headers=_auth(token),
        )
        if resp.status_code == 401:
            return _auth_expired()
        if resp.status_code != 200:
            return _err(f"Outlook API error (HTTP {resp.status_code})")

        messages = []
        for m in resp.json().get("value", []):
            messages.append({
                "id": m.get("id", ""),
                "from": m.get("from", {}).get("emailAddress", {}).get("address", "Unknown"),
                "subject": m.get("subject", "(no subject)"),
                "date": m.get("receivedDateTime", ""),
            })

    if not messages:
        return {"payload": {"messages": [], "query": query}, "summary": f'No emails found matching "{query}".', "success": True}

    lines = [
        f"- **{m.get('subject', '(no subject)')}**  —  {m.get('from', 'Unknown')} ({m.get('date', '')})"
        for m in messages
    ]

    return {
        "payload": {"messages": messages, "query": query},
        "summary": f'**Emails matching "{query}" ({len(messages)}):**\n\n' + "\n".join(lines),
        "success": True,
    }


# ── Gmail helpers ──────────────────────────────────────────────────


async def _fetch_gmail_headers(ctx, token: str, msg_id: str) -> dict | None:
    resp = await ctx.http.get(
        f"{GMAIL_API}/messages/{msg_id}?format=metadata"
        "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date&metadataHeaders=To",
        headers=_auth(token),
    )
    if resp.status_code != 200:
        return None

    data = resp.json()
    headers = _extract_gmail_headers(data)
    headers["id"] = msg_id
    headers["snippet"] = data.get("snippet", "")
    return headers


def _extract_gmail_headers(msg_data: dict) -> dict:
    result = {}
    for header in msg_data.get("payload", {}).get("headers", []):
        name = header.get("name", "").lower()
        if name in ("from", "to", "subject", "date"):
            result[name] = header.get("value", "")
    return result


def _extract_gmail_body(msg_data: dict) -> str:
    payload = msg_data.get("payload", {})
    if payload.get("body", {}).get("data"):
        return _decode_b64(payload["body"]["data"])
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode_b64(part["body"]["data"])
    return msg_data.get("snippet", "(could not extract message body)")


def _decode_b64(data: str) -> str:
    padded = data + "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


# ── Shared helpers ─────────────────────────────────────────────────


def _url_encode(text: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(text)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _needs_setup() -> dict:
    return _err(
        "No email account connected. To set it up:\n\n"
        "1. Go to **Settings > Skills > Email**\n"
        "2. Connect your **Gmail** or **Outlook** account\n"
        "3. Authorize access\n\n"
        "Both Gmail and Outlook are supported."
    )


def _auth_expired() -> dict:
    return _err("Email authorization has expired. Please re-authorize in **Settings > Skills > Email**.")


def _err(message: str) -> dict:
    return {"payload": None, "summary": message, "success": False, "error": message}
