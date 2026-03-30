"""muse.user — Interact with the user through the orchestrator."""

from __future__ import annotations

import uuid


class UserClient:
    """User interaction client. Calls pause skill execution until the user responds."""

    def __init__(self, ipc_client):
        self._ipc = ipc_client

    async def ask(self, question: str, options: list[str] | None = None) -> str:
        """Ask the user a question. Displayed in the chat stream.

        If options are provided, shown as buttons. Pauses execution until answered.
        """
        from muse_sdk.ipc_client import UserAskMsg

        request_id = str(uuid.uuid4())
        await self._ipc.send(UserAskMsg(
            request_id=request_id,
            message=question,
            options=options,
        ))
        resp = await self._ipc.receive()
        return str(resp.response)

    async def notify(self, message: str) -> None:
        """Send a non-blocking notification. Does not pause execution."""
        from muse_sdk.ipc_client import UserNotifyMsg

        await self._ipc.send(UserNotifyMsg(message=message))

    async def confirm(self, action_description: str) -> bool:
        """Request explicit confirmation for a sensitive action.

        Returns True if the user approves.
        """
        from muse_sdk.ipc_client import UserConfirmMsg

        request_id = str(uuid.uuid4())
        await self._ipc.send(UserConfirmMsg(
            request_id=request_id,
            message=action_description,
        ))
        resp = await self._ipc.receive()
        if resp.response is False:
            from muse_sdk.errors import UserCancelled
            raise UserCancelled(f"User denied: {action_description}")
        return True
