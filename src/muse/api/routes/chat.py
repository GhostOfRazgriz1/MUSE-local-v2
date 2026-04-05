"""Chat WebSocket and REST endpoints."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from muse.api.app import get_orchestrator, get_service
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
        get_service("session").user_tz = tz

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
    #
    # The generator yields a fast ``greeting_placeholder`` first, then
    # the full LLM ``greeting``.  We send the placeholder immediately
    # (progressive enhancement) and queue the rest.
    onboarding_active = (
        orchestrator._onboarding is not None
        and orchestrator._onboarding.is_active
    )
    greeting_queue: asyncio.Queue = asyncio.Queue()
    greeting_task = None
    if not resumed or onboarding_active:
        async def _stream_greeting():
            try:
                async for event in orchestrator.get_greeting():
                    await greeting_queue.put(event)
            except Exception as e:
                logger.error(f"Greeting error: {e}")
            await greeting_queue.put(None)  # sentinel
        greeting_task = asyncio.create_task(_stream_greeting())

    if resumed:
        await websocket.send_json({
            "type": "session_started",
            "session_id": session_id,
            "branch_head_id": get_service("session").branch_head_id,
        })

        messages = await orchestrator.session_repo.get_messages(
            session_id, branch_head_id=get_service("session").branch_head_id,
        )
        if messages:
            await websocket.send_json({
                "type": "history",
                "session_id": session_id,
                "messages": messages,
            })

        # Re-emit any pending permission requests that were left
        # unanswered when the user switched away.
        for perm_event in orchestrator.get_pending_permissions_for_session(session_id):
            await websocket.send_json(perm_event)

        # Re-emit active task_started events so the frontend restores
        # the task counter and activity indicator.
        for task_event in orchestrator.get_active_tasks_for_session(session_id):
            await websocket.send_json(task_event)

    # Subscribe to orchestrator events
    event_queue = orchestrator.subscribe()

    async def forward_events():
        try:
            while True:
                event = await event_queue.get()
                # Filter: only forward events for this session (or untagged events
                # like mood_changed which are global).
                event_sid = event.get("_session_id") if isinstance(event, dict) else None
                if event_sid and event_sid != session_id:
                    continue  # Not for this session — skip
                # Strip the internal tag before sending to the client
                if isinstance(event, dict):
                    event.pop("_session_id", None)
                await websocket.send_json(event)
        except Exception as e:
            logger.debug("Event forward loop ended: %s", e)

    forward_task = asyncio.create_task(forward_events())

    # Stream greeting events — placeholder arrives fast, full greeting follows.
    if greeting_task is not None:
        while True:
            event = await greeting_queue.get()
            if event is None:
                break
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
                elif msg_type == "screen_start":
                    mode_str = data.get("mode", "passive")
                    if hasattr(orchestrator, "screen_manager"):
                        from muse.screen.manager import ScreenMode
                        try:
                            mode = ScreenMode(mode_str)
                            await orchestrator.screen_manager.start(mode=mode)
                            status = await orchestrator.screen_manager.check_readiness()
                            await websocket.send_json({
                                "type": "screen_status", **status,
                            })
                        except (ValueError, RuntimeError) as exc:
                            await websocket.send_json({
                                "type": "screen_error",
                                "content": str(exc),
                            })
                elif msg_type == "screen_stop":
                    if hasattr(orchestrator, "screen_manager"):
                        await orchestrator.screen_manager.stop()
                        status = await orchestrator.screen_manager.check_readiness()
                        await websocket.send_json({
                            "type": "screen_status", **status,
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

    # Tasks run on the orchestrator (via run_in_background), NOT on the
    # WS handler.  This means they survive WebSocket disconnects — if the
    # user switches sessions, the task keeps running and persists results.
    # The WS handler just forwards events from the event queue.

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
                # Snapshot conversation history NOW — before the async task
                # starts.  The generator is lazy; by the time it executes,
                # the user may have switched sessions and the orchestrator's
                # _conversation_history points to a different session.
                history_snap = list(get_service("session").conversation_history)
                orchestrator.run_in_background(
                    orchestrator.handle_message(
                        content,
                        session_id=session_id,
                        history_snapshot=history_snap,
                    ),
                    session_id=session_id,
                )

            elif msg_type == "regenerate":
                last_user_msg = orchestrator.get_last_user_message()
                if last_user_msg:
                    await _ensure_session()
                    history_snap = list(get_service("session").conversation_history)
                    orchestrator.run_in_background(
                        orchestrator.handle_message(
                            last_user_msg,
                            session_id=session_id,
                            history_snapshot=history_snap,
                        ),
                        session_id=session_id,
                    )

            elif msg_type == "approve_permission":
                await _ensure_session()
                await websocket.send_json({
                    "type": "permission_approved",
                    "request_id": data["request_id"],
                })
                orchestrator.run_in_background(
                    orchestrator.approve_permission(
                        data["request_id"],
                        data.get("approval_mode", "once"),
                    ),
                    session_id=session_id,
                )

            elif msg_type == "deny_permission":
                await _ensure_session()
                await websocket.send_json({
                    "type": "permission_denied",
                    "request_id": data["request_id"],
                })
                orchestrator.run_in_background(
                    orchestrator.deny_permission(data["request_id"]),
                    session_id=session_id,
                )

    except WebSocketDisconnect:
        logger.info("Chat WebSocket disconnected")
        get_tracer().ws_disconnect(session_id or "(none)")
    except Exception as e:
        logger.error(f"Chat WebSocket error: {e}")
        get_tracer().error("ws", str(e), session_id=session_id or "(none)")
    finally:
        # Tasks run on the orchestrator — they keep running after the WS
        # closes.  We only need to clean up the WS-specific resources.
        # Cancel pending user interaction futures so skills don't hang
        # waiting for input that will never arrive.
        await orchestrator.cancel_pending_user_interactions(session_id=session_id)
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
