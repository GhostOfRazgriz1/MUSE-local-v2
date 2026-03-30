"""APIGateway — outbound proxy for all skill HTTP requests.

Enforces domain allowlisting, credential injection, rate limiting,
and request logging.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from muse.gateway.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class APIGateway:
    """Outbound HTTP proxy that every skill request passes through."""

    def __init__(
        self,
        credential_vault: Any,
        audit_repo: Any,
        config: Any,
    ) -> None:
        self._credential_vault = credential_vault
        self._audit_repo = audit_repo
        self._config = config

        self._host: str = getattr(config, "host", "127.0.0.1")
        self._port: int = getattr(config, "port", 8780)
        global_rpm: int = getattr(config, "global_rate_limit_rpm", 600)

        self._rate_limiter = RateLimiter(global_limit_rpm=global_rpm)
        self._domain_allowlists: dict[str, set[str]] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the aiohttp proxy server."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
        )
        self._app = web.Application()
        self._app.router.add_route("*", "/{path_info:.*}", self.handle_request)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("APIGateway listening on %s:%s", self._host, self._port)

    async def stop(self) -> None:
        """Gracefully shut down the proxy."""
        if self._session:
            await self._session.close()
            self._session = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("APIGateway stopped")

    # ------------------------------------------------------------------
    # Domain allowlist management
    # ------------------------------------------------------------------

    def register_skill_domains(self, skill_id: str, allowed_domains: list[str]) -> None:
        """Register the set of domains a skill is allowed to contact."""
        self._domain_allowlists[skill_id] = set(allowed_domains)
        logger.debug("Registered domains for skill %s: %s", skill_id, allowed_domains)

    def unregister_skill(self, skill_id: str) -> None:
        """Remove a skill's domain allowlist."""
        self._domain_allowlists.pop(skill_id, None)
        logger.debug("Unregistered domains for skill %s", skill_id)

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    async def handle_request(self, request: web.Request) -> web.Response:
        """Proxy handler — the core of the gateway.

        1. Extract skill_id / task_id from custom headers.
        2. Check domain against allowlist.
        3. Rate-limit check (per-skill and global).
        4. Inject credentials from vault when the domain matches a known service.
        5. Forward the request to the actual destination.
        6. Log to audit repo.
        7. Return the response to the skill.
        """
        start_time = time.monotonic()

        # 1. Extract identity headers
        skill_id: str = request.headers.get("X-Agent-OS-Skill", "")
        task_id: str = request.headers.get("X-Agent-OS-Task", "")

        if not skill_id:
            return web.Response(status=400, text="Missing X-Agent-OS-Skill header")

        # Determine the real destination URL.  The skill sends the full URL
        # as the path component of its proxy request, e.g.
        #   GET http://proxy:8780/https://api.example.com/v1/foo
        destination_url: str = request.match_info.get("path_info", "")
        if request.query_string:
            destination_url = f"{destination_url}?{request.query_string}"

        # 2. URL validation & domain allowlist -------------------------
        try:
            parsed = urlparse(destination_url)
            domain = parsed.hostname or ""
        except Exception:
            domain = ""

        # Block non-HTTP schemes (file://, gopher://, etc.)
        if parsed.scheme not in ("http", "https"):
            logger.warning("Skill %s blocked: unsupported scheme %s", skill_id, parsed.scheme)
            return web.Response(status=400, text=f"Only http/https URLs are allowed")

        # Block requests to private/loopback/link-local IPs (SSRF protection)
        if domain and await self._resolves_to_private(domain):
            logger.warning("Skill %s blocked: %s resolves to private IP", skill_id, domain)
            await self._log_audit(
                skill_id, task_id, request.method, destination_url,
                403, time.monotonic() - start_time, blocked=True,
            )
            return web.Response(status=403, text="Destination resolves to a private address")

        # Domain allowlist check (includes port when specified)
        domain_with_port = f"{domain}:{parsed.port}" if parsed.port else domain
        allowed = self._domain_allowlists.get(skill_id)
        if allowed is not None and domain not in allowed and domain_with_port not in allowed:
            logger.warning(
                "Skill %s blocked: domain %s not in allowlist", skill_id, domain,
            )
            await self._log_audit(
                skill_id, task_id, request.method, destination_url,
                403, time.monotonic() - start_time, blocked=True,
            )
            return web.Response(
                status=403,
                text=f"Domain {domain} is not in the allowlist for skill {skill_id}",
            )

        # 3. Rate-limit check ------------------------------------------
        if not self._rate_limiter.check("global"):
            await self._log_audit(
                skill_id, task_id, request.method, destination_url,
                429, time.monotonic() - start_time, blocked=True,
            )
            return web.Response(status=429, text="Global rate limit exceeded")

        if not self._rate_limiter.check(skill_id):
            await self._log_audit(
                skill_id, task_id, request.method, destination_url,
                429, time.monotonic() - start_time, blocked=True,
            )
            return web.Response(status=429, text=f"Rate limit exceeded for skill {skill_id}")

        self._rate_limiter.consume("global")
        self._rate_limiter.consume(skill_id)

        # 4. Credential injection --------------------------------------
        headers = dict(request.headers)
        # Remove hop-by-hop / internal headers before forwarding
        for hdr in ("Host", "X-Agent-OS-Skill", "X-Agent-OS-Task"):
            headers.pop(hdr, None)

        headers = await self._inject_credentials(skill_id, domain, headers)

        # 5. Forward request -------------------------------------------
        body = await request.read()
        status = 502
        response_body = b""
        response_headers: dict[str, str] = {}

        try:
            if self._session is None:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30, connect=10),
                )

            async with self._session.request(
                method=request.method,
                url=destination_url,
                headers=headers,
                data=body if body else None,
                allow_redirects=False,
            ) as upstream_resp:
                status = upstream_resp.status
                response_body = await upstream_resp.read()
                response_headers = dict(upstream_resp.headers)
        except aiohttp.ClientError as exc:
            logger.error("Upstream request failed for skill %s: %s", skill_id, exc)
            status = 502
            response_body = str(exc).encode()
        except asyncio.TimeoutError:
            logger.error("Upstream request timed out for skill %s", skill_id)
            status = 504
            response_body = b"Gateway timeout"

        elapsed = time.monotonic() - start_time

        # 6. Audit log -------------------------------------------------
        await self._log_audit(
            skill_id, task_id, request.method, destination_url,
            status, elapsed, blocked=False,
        )

        # 7. Return response to skill ----------------------------------
        # Strip hop-by-hop headers from upstream response
        for hdr in ("Transfer-Encoding", "Content-Encoding", "Connection"):
            response_headers.pop(hdr, None)

        return web.Response(
            status=status,
            body=response_body,
            headers=response_headers,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _resolves_to_private(hostname: str) -> bool:
        """Return True if *hostname* resolves to a private/loopback/link-local IP."""
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            for _family, _type, _proto, _canonname, sockaddr in infos:
                ip = ipaddress.ip_address(sockaddr[0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return True
        except socket.gaierror:
            return True  # unresolvable hostnames are blocked
        return False

    async def _inject_credentials(
        self, skill_id: str, domain: str, headers: dict[str, str],
    ) -> dict[str, str]:
        """Look up credentials from the vault for *domain* and inject them."""
        try:
            cred = await self._credential_vault.get_for_domain(domain)
            if cred:
                # The vault returns a dict like {"header": "Authorization", "value": "Bearer ..."}
                header_name = cred.get("header", "Authorization")
                header_value = cred.get("value", "")
                if header_value:
                    headers[header_name] = header_value
                    logger.debug(
                        "Injected credential header %s for domain %s (skill %s)",
                        header_name, domain, skill_id,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("No credential injection for domain %s: %s", domain, exc)
        return headers

    async def _log_audit(
        self,
        skill_id: str,
        task_id: str,
        method: str,
        url: str,
        status: int,
        elapsed: float,
        blocked: bool = False,
    ) -> None:
        """Emit an audit record for the proxied request."""
        try:
            await self._audit_repo.record(
                event_type="gateway_request",
                data={
                    "skill_id": skill_id,
                    "task_id": task_id,
                    "method": method,
                    "url": url,
                    "status": status,
                    "elapsed_seconds": round(elapsed, 4),
                    "blocked": blocked,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to log audit event: %s", exc)
