"""Chat WebSocket and REST endpoints."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from muse.api.app import get_orchestrator
from muse.api.auth import require_ws_token
from muse.debug import get_tracer

logger = logging.getLogger(__name__)

# Allowed WebSocket origins — prevents cross-site WebSocket hijacking.
_ALLOWED_WS_ORIGINS = {
    "http://localhost:3000", "http://127.0.0.1:3000",
    "https://localhost:3000", "https://127.0.0.1:3000",
    "http://localhost:8080", "http://127.0.0.1:8080",
    "https://localhost:8080", "https://127.0.0.1:8080",
}

# Maximum user message length (characters).  Prevents token explosion
# and excessive LLM costs from a single message.
MAX_MESSAGE_LENGTH = 32_000

# REST endpoints — bearer-token auth applied via router dependencies in app.py
router = APIRouter(tags=["chat"])

# WebSocket endpoint on a separate router so the header-based auth
# dependency from app.py doesn't apply (browsers can't send custom
# headers on WS upgrades).  Auth is handled via query-param token
# inside the handler itself.
ws_router = APIRouter(tags=["chat"])


@ws_router.websocket("/ws/chat")
async def chat_websocket(
    websocket: WebSocket,
    session_id: str | None = Query(None),
    token: str | None = Query(None),
    tz: str | None = Query(None),
):
    """WebSocket endpoint for the chat stream.

    Query params:
        session_id — resume an existing session. If omitted, session
                     creation is deferred until the user sends a message.
        token      — bearer token for authentication.
        tz         — IANA timezone (e.g. "America/New_York") from the browser.

    Client sends: {"type": "message", "content": "..."}
    Server sends: stream of event dicts from the orchestrator
    """
    # Validate Origin header — prevents cross-site WebSocket hijacking.
    # Browsers always send Origin on WS upgrades; its absence means a
    # non-browser client (curl, Python), which is fine if the token is valid.
    origin = (websocket.headers.get("origin") or "").rstrip("/").lower()
    if origin and origin not in _ALLOWED_WS_ORIGINS:
        logger.warning("Rejected WebSocket from disallowed origin: %s", origin)
        await websocket.close(code=1008)
        return

    # Authenticate before accepting the connection
    try:
        await require_ws_token(websocket, token)
    except Exception:
        return

    await websocket.accept()
    orchestrator = get_orchestrator()

    if not orchestrator:
        await websocket.send_json({"type": "error", "content": "Orchestrator not ready"})
        await websocket.close()
        return

    # Store the user's timezone for time-aware features
    if tz:
        orchestrator._user_tz = tz

    # ------------------------------------------------------------------
    # Session bootstrap
    # ------------------------------------------------------------------
    # For resumed sessions: load immediately and send history.
    # For new sessions: defer creation until the user actually sends a
    # message — this prevents phantom empty sessions from accumulating
    # when the user opens the app but never types.
    # ------------------------------------------------------------------
    resumed = False
    if session_id:
        loaded = await orchestrator.set_session(session_id)
        if loaded:
            resumed = True
        else:
            session_id = None

    get_tracer().ws_connect(session_id or "(deferred)")

    # Pre-start greeting computation in the background so the LLM call
    # runs in parallel with history loading.  This shaves 100-300ms off
    # T2FM because the greeting is ready (or nearly ready) by the time
    # session bootstrap finishes.
    onboarding_active = (
        orchestrator._onboarding is not None
        and orchestrator._onboarding.is_active
    )
    greeting_task = None
    if not resumed or onboarding_active:
        async def _precompute_greeting():
            events = []
            try:
                async for event in orchestrator.get_greeting():
                    events.append(event)
            except Exception as e:
                logger.error(f"Greeting error: {e}")
            return events
        greeting_task = asyncio.create_task(_precompute_greeting())

    if resumed:
        await websocket.send_json({
            "type": "session_started",
            "session_id": session_id,
            "branch_head_id": orchestrator._branch_head_id,
        })

        messages = await orchestrator.session_repo.get_messages(
            session_id, branch_head_id=orchestrator._branch_head_id,
        )
        if messages:
            await websocket.send_json({
                "type": "history",
                "session_id": session_id,
                "messages": messages,
            })

    # Subscribe to orchestrator events
    event_queue = orchestrator.subscribe()

    async def forward_events():
        try:
            while True:
                event = await event_queue.get()
                await websocket.send_json(event)
        except Exception as e:
            logger.debug("Event forward loop ended: %s", e)

    forward_task = asyncio.create_task(forward_events())

    # Send the pre-computed greeting (awaits if still in progress)
    if greeting_task is not None:
        greeting_events = await greeting_task
        for event in greeting_events:
            await websocket.send_json(event)

    # ------------------------------------------------------------------
    # Helper: ensure a session exists before processing user-initiated
    # events (messages, permission approvals).  On the first call this
    # creates the session and notifies the frontend.
    # ------------------------------------------------------------------
    async def _ensure_session() -> str:
        nonlocal session_id
        if session_id:
            return session_id
        session = await orchestrator.create_session()
        session_id = session["id"]
        await websocket.send_json({
            "type": "session_started",
            "session_id": session_id,
            "branch_head_id": None,
        })
        return session_id

    # ------------------------------------------------------------------
    # Incoming message queue — decouples WebSocket reads from processing
    # so user_response / kill_task messages can be handled while a skill
    # is actively running (otherwise we'd deadlock: the main loop waits
    # for the skill generator, but the skill waits for user_response
    # which can't be read because the main loop is blocked).
    # ------------------------------------------------------------------
    incoming: asyncio.Queue = asyncio.Queue()

    async def ws_reader():
        """Read from the WebSocket and dispatch immediately or enqueue."""
        _t = get_tracer()
        try:
            while True:
                data = await websocket.receive_json()
                msg_type = data.get("type")
                _t.ws_receive(msg_type or "unknown", data)

                # These can be handled instantly without blocking the
                # processing loop — they just resolve a Future or cancel
                # a task, so dispatch them immediately.
                if msg_type == "user_response":
                    orchestrator.respond_to_skill(
                        data["request_id"],
                        data.get("response", ""),
                    )
                elif msg_type == "kill_task":
                    await orchestrator.kill_task(data["task_id"])
                    await websocket.send_json({
                        "type": "task_killed",
                        "task_id": data["task_id"],
                    })
                elif msg_type == "steer":
                    content = data.get("content", "").strip()
                    if content:
                        orchestrator.inject_steering(content)
                        await websocket.send_json({
                            "type": "steering_received",
                            "content": content,
                        })
                elif msg_type == "suggestion_feedback":
                    sid = data.get("suggestion_id", "")
                    accepted = data.get("accepted", False)
                    if sid:
                        await orchestrator.proactivity.record_feedback(sid, accepted)
                else:
                    # Messages, permission approvals/denials — queue for
                    # sequential processing (they yield event streams).
                    await incoming.put(data)
        except WebSocketDisconnect:
            await incoming.put(None)  # Sentinel to stop the processor
        except Exception:
            await incoming.put(None)

    reader_task = asyncio.create_task(ws_reader())

    # Track background message tasks so we can cancel on disconnect
    active_msg_tasks: set[asyncio.Task] = set()

    async def _stream_to_ws(gen):
        """Consume an async generator and send events to the WebSocket."""
        _t = get_tracer()
        try:
            async for event in gen:
                _t.ws_send(event)
                await websocket.send_json(event)
        except Exception as e:
            logger.debug("Stream to WS ended: %s", e)

    try:
        while True:
            data = await incoming.get()
            if data is None:
                break  # WebSocket closed

            msg_type = data.get("type")

            if msg_type == "message":
                content = data.get("content", "").strip()
                if not content:
                    continue
                if len(content) > MAX_MESSAGE_LENGTH:
                    await websocket.send_json({
                        "type": "error",
                        "content": f"Message too long ({len(content):,} chars). "
                                   f"Maximum is {MAX_MESSAGE_LENGTH:,} characters.",
                    })
                    continue
                await _ensure_session()
                # Run each message as an independent task so the user
                # can send new messages while skills are still running.
                task = asyncio.create_task(
                    _stream_to_ws(orchestrator.handle_message(content))
                )
                active_msg_tasks.add(task)
                task.add_done_callback(active_msg_tasks.discard)

            elif msg_type == "regenerate":
                # Re-process the last user message for a fresh response
                last_user_msg = orchestrator.get_last_user_message()
                if last_user_msg:
                    await _ensure_session()
                    task = asyncio.create_task(
                        _stream_to_ws(orchestrator.handle_message(last_user_msg))
                    )
                    active_msg_tasks.add(task)
                    task.add_done_callback(active_msg_tasks.discard)

            elif msg_type == "approve_permission":
                await _ensure_session()
                await websocket.send_json({
                    "type": "permission_approved",
                    "request_id": data["request_id"],
                })
                task = asyncio.create_task(
                    _stream_to_ws(orchestrator.approve_permission(
                        data["request_id"],
                        data.get("approval_mode", "once"),
                    ))
                )
                active_msg_tasks.add(task)
                task.add_done_callback(active_msg_tasks.discard)

            elif msg_type == "deny_permission":
                await _ensure_session()
                await websocket.send_json({
                    "type": "permission_denied",
                    "request_id": data["request_id"],
                })
                task = asyncio.create_task(
                    _stream_to_ws(orchestrator.deny_permission(data["request_id"]))
                )
                active_msg_tasks.add(task)
                task.add_done_callback(active_msg_tasks.discard)

    except WebSocketDisconnect:
        logger.info("Chat WebSocket disconnected")
        get_tracer().ws_disconnect(session_id or "(none)")
    except Exception as e:
        logger.error(f"Chat WebSocket error: {e}")
        get_tracer().error("ws", str(e), session_id=session_id or "(none)")
    finally:
        # Cancel any still-running message tasks
        for t in active_msg_tasks:
            t.cancel()
        reader_task.cancel()
        forward_task.cancel()
        orchestrator.unsubscribe(event_queue)


@router.post("/chat")
async def chat_rest(message: dict):
    """REST fallback for non-WebSocket clients."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Orchestrator not ready"}

    content = message.get("content", "").strip()
    if not content:
        return {"error": "Empty message"}
    if len(content) > MAX_MESSAGE_LENGTH:
        return {"error": f"Message too long ({len(content):,} chars). Maximum is {MAX_MESSAGE_LENGTH:,} characters."}

    events = []
    async for event in orchestrator.handle_message(content):
        events.append(event)

    return {"events": events}
