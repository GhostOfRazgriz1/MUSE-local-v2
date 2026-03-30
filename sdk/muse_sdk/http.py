"""muse.http — Make outbound HTTP requests through the API gateway."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json as _json


@dataclass
class Response:
    """HTTP response from the API gateway."""

    status_code: int
    headers: dict[str, str]
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.body)


class HttpClient:
    """HTTP client that routes all requests through the orchestrator's API gateway.

    Skills never see raw credentials; the gateway injects them.
    URLs must match allowed domains declared in the skill manifest.
    """

    def __init__(self, ipc_client, skill_id: str, config: dict):
        self._ipc = ipc_client
        self._skill_id = skill_id
        self._gateway_url = config.get("gateway_url", "http://127.0.0.1:8100")

    async def get(self, url: str, headers: dict | None = None) -> Response:
        return await self._request("GET", url, headers=headers)

    async def post(self, url: str, body: Any = None, headers: dict | None = None) -> Response:
        return await self._request("POST", url, body=body, headers=headers)

    async def put(self, url: str, body: Any = None, headers: dict | None = None) -> Response:
        return await self._request("PUT", url, body=body, headers=headers)

    async def delete(self, url: str, headers: dict | None = None) -> Response:
        return await self._request("DELETE", url, headers=headers)

    async def _request(
        self, method: str, url: str, body: Any = None, headers: dict | None = None
    ) -> Response:
        import uuid
        from muse_sdk.ipc_client import HttpRequestMsg

        request_id = str(uuid.uuid4())
        await self._ipc.send(HttpRequestMsg(
            request_id=request_id,
            method=method,
            url=url,
            headers=headers or {},
            body=_json.dumps(body) if body is not None else None,
        ))
        resp = await self._ipc.receive()
        if hasattr(resp, "error") and resp.error:
            from muse_sdk.errors import ExternalServiceError
            raise ExternalServiceError(url, message=resp.error)

        return Response(
            status_code=resp.status_code,
            headers=resp.headers or {},
            body=(resp.body or "").encode("utf-8"),
        )
