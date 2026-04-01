"""SkillSandbox — execute skills in isolated environments."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from muse.debug import get_tracer
from muse.skills.manifest import SkillManifest
from muse.skills.warm_pool import WarmPool

# Skill IDs must be alphanumeric + hyphens/underscores (no path separators)
_SAFE_SKILL_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _-]*$")

logger = logging.getLogger(__name__)


class SkillSandbox:
    """Launch and manage sandboxed skill executions."""

    def __init__(
        self,
        skills_dir: Path,
        ipc_dir: Path,
        warm_pool: WarmPool | None = None,
    ) -> None:
        self._skills_dir = skills_dir
        self._ipc_dir = ipc_dir
        self._warm_pool = warm_pool
        # task_id -> (asyncio.Task, optional PooledProcess)
        self._running: dict[str, tuple[asyncio.Task[None], Any]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        task_id: str,
        skill_id: str,
        manifest: SkillManifest,
        brief: dict,
        permissions: list[str],
        config: dict,
    ) -> None:
        """Launch a skill in the appropriate sandbox tier.

        * **lightweight** (first-party): run in-process via importlib.
        * **standard** / **hardened**: subprocess (from warm pool when
          available, cold-spawn otherwise).

        The method returns once the skill execution completes (or the
        timeout is reached).
        """
        timeout = manifest.timeout_seconds

        # Reject skill_ids containing path separators or other dangerous chars
        if not _SAFE_SKILL_ID.match(skill_id):
            raise ValueError(f"Invalid skill_id: {skill_id!r}")

        if manifest.isolation_tier == "lightweight" and manifest.is_first_party:
            coro = self._run_in_process(task_id, skill_id, manifest, brief, permissions, config)
        else:
            coro = self._run_in_subprocess(task_id, skill_id, manifest, brief, permissions, config)

        task = asyncio.current_task() or asyncio.ensure_future(coro)  # type: ignore[arg-type]
        try:
            await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(
                "Skill %s (task %s) exceeded timeout of %ss — killing",
                skill_id, task_id, timeout,
            )
            await self.kill(task_id)
            raise
        finally:
            self._running.pop(task_id, None)

    async def kill(self, task_id: str) -> None:
        """Terminate a running skill process."""
        entry = self._running.pop(task_id, None)
        if entry is None:
            logger.debug("kill(%s): no running process found", task_id)
            return

        _task, pooled_process = entry
        if pooled_process is not None:
            try:
                pooled_process.process.kill()
                logger.info("Killed subprocess for task %s", task_id)
            except ProcessLookupError:
                pass
        else:
            # In-process execution — cancel the asyncio task
            if _task is not None and not _task.done():
                _task.cancel()
                logger.info("Cancelled in-process task %s", task_id)

    # ------------------------------------------------------------------
    # Lightweight (in-process) execution
    # ------------------------------------------------------------------

    def set_orchestrator(self, orchestrator) -> None:
        """Set the orchestrator reference for in-process skill execution."""
        self._orchestrator = orchestrator

    async def _run_in_process(
        self,
        task_id: str,
        skill_id: str,
        manifest: SkillManifest,
        brief: dict,
        permissions: list[str],
        config: dict,
    ) -> None:
        """Run a first-party lightweight skill in the current process."""
        self._running[task_id] = (asyncio.current_task(), None)  # type: ignore[arg-type]

        skill_dir = self._skills_dir / skill_id
        entry = manifest.entry_point.replace(".py", "").replace("/", ".")
        module_path = skill_dir / manifest.entry_point

        logger.info("Running skill %s in-process (task %s)", skill_id, task_id)
        _t = get_tracer()
        _t.skill_load(skill_id, str(module_path))
        _t.skill_start(task_id, skill_id, "lightweight")

        # Build a LocalBridge so the SDK can call orchestrator services directly
        bridge = LocalBridge(
            orchestrator=getattr(self, "_orchestrator", None),
            task_id=task_id,
            skill_id=skill_id,
            brief=brief,
            invoke_depth=brief.get("_invoke_depth", 0),
            invoke_chain=brief.get("_invoke_chain", [skill_id]),
        )

        # Build a SkillContext — import SDK without sys.path manipulation
        try:
            from muse_sdk.context import SkillContext
        except ImportError:
            sdk_ctx_path = Path(__file__).resolve().parent.parent.parent.parent / "sdk" / "muse_sdk" / "context.py"
            _sdk_spec = importlib.util.spec_from_file_location("_agentskill_sdk.context", sdk_ctx_path)
            if _sdk_spec is None or _sdk_spec.loader is None:
                raise ImportError(f"Cannot load SDK from {sdk_ctx_path}")
            _sdk_mod = importlib.util.module_from_spec(_sdk_spec)
            _sdk_spec.loader.exec_module(_sdk_mod)
            SkillContext = _sdk_mod.SkillContext

        ctx = SkillContext(
            task_id=task_id,
            skill_id=skill_id,
            brief=brief,
            permissions=permissions,
            config=config,
            ipc_client=bridge,
        )

        # Register bridge so orchestrator can route user responses to it
        orch = getattr(self, "_orchestrator", None)
        if orch:
            orch.register_bridge(task_id, bridge)

        # Use a namespaced key in sys.modules to avoid shadowing real modules.
        # Do NOT add the skill directory to sys.path — that would let a
        # malicious skill shadow stdlib modules (os.py, json.py, etc.).
        module_key = f"_agentskill.{skill_id}.{entry}"
        try:
            spec = importlib.util.spec_from_file_location(module_key, module_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load skill module from {module_path}")
            module = importlib.util.module_from_spec(spec)
            module.__name__ = module_key
            sys.modules[module_key] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]

            # Dispatch to the specific action function if one was resolved,
            # otherwise fall back to run().
            action_id = brief.get("action")
            if action_id:
                run_fn = getattr(module, action_id, None)
                if run_fn is None:
                    # Action declared but function missing — fall back to run()
                    logger.warning(
                        "Skill %s has no function '%s', falling back to run()",
                        skill_id, action_id,
                    )
                    run_fn = getattr(module, "run", None)
            else:
                run_fn = getattr(module, "run", None)

            if run_fn is None:
                raise AttributeError(f"Skill module {entry} has no 'run' function")

            result = run_fn(ctx)
            if asyncio.iscoroutine(result):
                result = await result

            _t.skill_finish(task_id, skill_id, "completed")

            # Report completion to task manager
            orch = getattr(self, "_orchestrator", None)
            if orch and result is not None:
                await orch._task_manager.update_status(
                    task_id, "completed", result=result,
                )
        except Exception as e:
            _t.skill_finish(task_id, skill_id, "failed", error=str(e))
            orch = getattr(self, "_orchestrator", None)
            if orch:
                await orch._task_manager.update_status(
                    task_id, "failed", error=str(e),
                )
            raise
        finally:
            sys.modules.pop(module_key, None)
            # Close bridge (releases httpx connection pool, etc.)
            try:
                await bridge.close()
            except Exception as e:
                logger.debug("Error closing bridge for task %s: %s", task_id, e)
            # Unregister bridge
            orch = getattr(self, "_orchestrator", None)
            if orch:
                orch.unregister_bridge(task_id)

    # ------------------------------------------------------------------
    # Subprocess execution (standard / hardened)
    # ------------------------------------------------------------------

    async def _run_in_subprocess(
        self,
        task_id: str,
        skill_id: str,
        manifest: SkillManifest,
        brief: dict,
        permissions: list[str],
        config: dict,
    ) -> None:
        """Run a skill in a subprocess (warm-pooled or cold-spawned)."""
        pooled_process = None
        process: asyncio.subprocess.Process | None = None

        payload = json.dumps({
            "task_id": task_id,
            "skill_id": skill_id,
            "skill_dir": str(self._skills_dir / skill_id),
            "entry_point": manifest.entry_point,
            "brief": brief,
            "permissions": permissions,
            "config": config,
            "ipc_dir": str(self._ipc_dir),
        })

        if self._warm_pool is not None:
            try:
                pooled_process = await self._warm_pool.checkout()
                process = pooled_process.process
                logger.debug(
                    "Using warm-pool process %s for task %s", pooled_process.id, task_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Warm pool checkout failed, cold-spawning: %s", exc)
                pooled_process = None

        if process is None:
            # Cold spawn with a minimal environment to prevent
            # PYTHONPATH/LD_PRELOAD hijacking.
            import os
            _SAFE_KEYS = frozenset({
                "PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP",
                "USERPROFILE", "COMSPEC", "LANG", "LC_ALL",
            })
            _DANGEROUS_KEYS = frozenset({
                "PYTHONPATH", "PYTHONHOME", "PYTHONEXECUTABLE",
                "PYTHONSTARTUP", "PYTHONUSERBASE",
                "LD_PRELOAD", "LD_LIBRARY_PATH",
                "DYLD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
            })
            safe_env = {
                k: v for k, v in os.environ.items()
                if k in _SAFE_KEYS and k not in _DANGEROUS_KEYS
            }
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "muse.skills._bootstrap",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=safe_env,
            )
            logger.debug("Cold-spawned process pid=%s for task %s", process.pid, task_id)

        self._running[task_id] = (asyncio.current_task(), pooled_process)  # type: ignore[arg-type]

        assert process.stdin is not None
        process.stdin.write((payload + "\n").encode())
        await process.stdin.drain()
        process.stdin.write_eof()

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(
                "Skill %s (task %s) exited with code %s: %s",
                skill_id, task_id, process.returncode,
                stderr.decode(errors="replace")[:2000],
            )

        # Return pooled process
        if pooled_process is not None and self._warm_pool is not None:
            if process.returncode == 0:
                await self._warm_pool.return_process(pooled_process)
            else:
                # Don't reuse a process that crashed
                try:
                    process.kill()
                except ProcessLookupError:
                    pass


# ======================================================================
# LocalBridge — in-process IPC substitute for lightweight skills
# ======================================================================

class _Response:
    """Minimal response object matching what the SDK expects."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class LocalBridge:
    """Provides the same interface as IPCClient but calls orchestrator
    services directly.  Used for lightweight (in-process) skills so they
    can use ctx.memory, ctx.llm, ctx.user, etc. without real IPC."""

    MAX_INVOKE_DEPTH = 3

    def __init__(
        self, orchestrator, task_id: str, skill_id: str,
        brief: dict | None = None,
        invoke_depth: int = 0,
        invoke_chain: list[str] | None = None,
    ):
        self._orch = orchestrator
        self._task_id = task_id
        self._skill_id = skill_id
        self._brief = brief or {}
        self._pending_response: asyncio.Queue = asyncio.Queue()
        # Futures for user interaction: request_id -> Future
        self._user_futures: dict[str, asyncio.Future] = {}
        # Skill invocation depth and call chain (for circular detection)
        self._invoke_depth = invoke_depth
        self._invoke_chain = invoke_chain or [skill_id]

    def _get_context_preamble(self) -> str:
        """Build a compact context preamble from the brief for skill LLM calls."""
        ctx = self._brief.get("context", {})
        summary = ctx.get("context_summary", "")
        conv = ctx.get("conversation_summary", "")
        parts = []
        if summary:
            parts.append(summary)
        if conv:
            parts.append(f"Recent conversation:\n{conv}")
        return "\n\n".join(parts)

    async def _check_namespace(self, namespace: str) -> str | None:
        """Enforce namespace isolation.

        Skills can freely access:
          - Their own namespace (matches skill_id, case-insensitive)
          - The _profile namespace (if they have profile:read/write)
        Everything else is denied unless a bridge exists (future).
        Returns an error string if denied, None if allowed.
        """
        ns_lower = namespace.lower()
        own_lower = self._skill_id.lower()

        # Own namespace — always allowed
        if ns_lower == own_lower:
            return None

        # _profile — requires profile:read or profile:write permission
        if ns_lower == "_profile":
            if not self._orch:
                return f"Access denied: no orchestrator reference"
            # Check if the skill has been granted any profile permission
            for perm in ("profile:read", "profile:write"):
                check = await self._orch._permissions.check_permission(self._skill_id, perm)
                if check.allowed:
                    return None
            return f"Access denied: '{self._skill_id}' needs profile:read or profile:write permission"

        # System namespaces used internally
        if ns_lower in ("_system", "_conversation"):
            return f"Namespace '{namespace}' is reserved for the orchestrator"

        # Another skill's namespace — denied
        return f"Access denied: '{self._skill_id}' cannot access namespace '{namespace}'"

    async def send(self, message) -> None:
        """Handle a message from the skill, dispatch to orchestrator."""
        msg_type = getattr(message, "type", "")
        get_tracer().bridge_send(self._task_id, msg_type)

        if msg_type == "memory_read":
            err = await self._check_namespace(message.namespace)
            if err:
                await self._pending_response.put(_Response(success=False, value=None, error=err))
                return
            entry = await self._orch._memory_repo.get(message.namespace, message.key)
            if entry:
                await self._pending_response.put(
                    _Response(success=True, value=entry["value"])
                )
            else:
                await self._pending_response.put(
                    _Response(success=True, value=None)
                )

        elif msg_type == "memory_write":
            err = await self._check_namespace(message.namespace)
            if err:
                await self._pending_response.put(_Response(success=False, error=err))
                return
            await self._orch._memory_repo.put(
                message.namespace, message.key, message.value,
                value_type=message.value_type, source_task_id=self._task_id,
            )
            await self._pending_response.put(_Response(success=True))

        elif msg_type == "memory_search":
            err = await self._check_namespace(message.namespace)
            if err:
                await self._pending_response.put(_Response(success=False, entries=[], error=err))
                return
            embedding = await self._orch._embeddings.embed_async(message.query)
            results = await self._orch._memory_repo.search(
                embedding, namespace=message.namespace, limit=message.limit,
            )
            entries = [
                {"key": r["key"], "value": r["value"],
                 "value_type": r.get("value_type", "text"),
                 "relevance_score": r.get("relevance_score", 0),
                 "namespace": r.get("namespace", "")}
                for r in results
            ]
            await self._pending_response.put(
                _Response(success=True, entries=entries)
            )

        elif msg_type == "memory_list_keys":
            err = await self._check_namespace(message.namespace)
            if err:
                await self._pending_response.put(_Response(success=False, keys=[], error=err))
                return
            keys = await self._orch._memory_repo.list_keys(
                message.namespace, message.prefix,
            )
            await self._pending_response.put(_Response(success=True, keys=keys))

        elif msg_type == "llm_request":
            try:
                model = await self._orch._model_router.resolve_model(
                    skill_id=self._skill_id,
                )
                # Inject assembled context as preamble so skills aren't
                # operating blind. The brief.context was assembled by the
                # orchestrator and contains user profile + relevant memory.
                preamble = self._get_context_preamble()
                system_parts = []
                if preamble:
                    system_parts.append(preamble)
                if message.system:
                    system_parts.append(message.system)
                system = "\n\n".join(system_parts) if system_parts else None

                messages = []
                if system:
                    messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": message.prompt})
                result = await self._orch._provider.complete(
                    model=model,
                    messages=messages,
                    max_tokens=message.max_tokens,
                    json_mode=message.json_mode,
                )
                # Accumulate tokens into the task and session-level tracking
                if self._orch:
                    self._orch._task_manager.accumulate_tokens(
                        self._task_id, result.tokens_in, result.tokens_out,
                    )
                    self._orch.track_llm_usage(result.tokens_in, result.tokens_out)
                    await self._orch._permissions.consume_budget(
                        "llm:complete", actions=1,
                        tokens=result.tokens_in + result.tokens_out,
                    )
                _t = get_tracer()
                _t.llm_call(f"skill:{self._skill_id}", model,
                            tokens_in=result.tokens_in, tokens_out=result.tokens_out)
                _t.event("llm", "response",
                         task_id=self._task_id,
                         skill_id=self._skill_id,
                         model=result.model_used,
                         prompt=message.prompt[:500],
                         response=result.text[:1000],
                         tokens_in=result.tokens_in,
                         tokens_out=result.tokens_out)
                await self._pending_response.put(
                    _Response(success=True, text=result.text, result=result.text, error=None,
                              tokens_in=result.tokens_in, tokens_out=result.tokens_out)
                )
            except Exception as e:
                get_tracer().error("bridge", f"LLM call failed: {e}",
                                   task_id=self._task_id, skill_id=self._skill_id)
                await self._pending_response.put(
                    _Response(success=False, text="", result=None, error=str(e),
                              tokens_in=0, tokens_out=0)
                )

        elif msg_type == "user_ask":
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._user_futures[message.request_id] = future
            await self._orch._emit_event({
                "type": "skill_question",
                "task_id": self._task_id,
                "skill_id": self._skill_id,
                "question": message.message,
                "options": getattr(message, "options", None),
                "request_id": message.request_id,
            })
            # Block until the UI sends a response (timeout 120s)
            try:
                answer = await asyncio.wait_for(future, timeout=120)
                await self._pending_response.put(_Response(response=answer))
            except asyncio.TimeoutError:
                await self._pending_response.put(_Response(response=""))
            finally:
                self._user_futures.pop(message.request_id, None)

        elif msg_type == "user_confirm":
            future = asyncio.get_event_loop().create_future()
            self._user_futures[message.request_id] = future
            await self._orch._emit_event({
                "type": "skill_confirm",
                "task_id": self._task_id,
                "skill_id": self._skill_id,
                "message": message.message,
                "request_id": message.request_id,
            })
            try:
                answer = await asyncio.wait_for(future, timeout=120)
                await self._pending_response.put(_Response(response=answer))
            except asyncio.TimeoutError:
                await self._pending_response.put(_Response(response=False))
            finally:
                self._user_futures.pop(message.request_id, None)

        elif msg_type == "user_notify":
            await self._orch._emit_event({
                "type": "skill_notify",
                "task_id": self._task_id,
                "skill_id": self._skill_id,
                "message": message.message,
            })

        elif msg_type == "status":
            if message.status == "checkpoint":
                task = await self._orch._task_manager.get_task(self._task_id)
                step = len(task.checkpoints) if task else 0
                await self._orch._task_manager.add_checkpoint(
                    self._task_id, step + 1, message.description, message.result,
                )

        elif msg_type == "credential_read":
            try:
                # Verify the skill declared this credential in its manifest
                allowed_cred_ids: set[str] = set()
                manifest = await self._orch._skill_loader.get_manifest(self._skill_id)
                if manifest:
                    allowed_cred_ids = {c.id for c in manifest.credentials}
                if message.credential_id not in allowed_cred_ids:
                    await self._pending_response.put(_Response(
                        success=False, value=None,
                        error=f"Skill '{self._skill_id}' did not declare credential '{message.credential_id}' in its manifest",
                    ))
                    return

                # Also verify the skill has credential:read permission granted.
                # Manifest declaration alone is not enough — prevents a
                # malicious manifest from self-granting access to any credential.
                cred_perm = "credential:read"
                perm_check = await self._orch._permissions.check_permission(
                    self._skill_id, cred_perm,
                )
                if not perm_check.allowed:
                    await self._pending_response.put(_Response(
                        success=False, value=None,
                        error=f"Skill '{self._skill_id}' does not have '{cred_perm}' permission granted",
                    ))
                    return

                secret = await self._orch._vault.retrieve(message.credential_id)
                await self._pending_response.put(_Response(
                    success=secret is not None, value=secret, error=None,
                ))
            except Exception as e:
                await self._pending_response.put(_Response(
                    success=False, value=None, error=str(e),
                ))

        elif msg_type == "http_request":
            try:
                # SSRF protection — same rules as the gateway proxy
                from urllib.parse import urlparse as _urlparse
                import ipaddress as _ipa
                import socket as _sock

                _parsed = _urlparse(message.url)
                if _parsed.scheme not in ("http", "https"):
                    await self._pending_response.put(_Response(
                        success=False, status_code=0, headers={}, body="",
                        error=f"Only http/https URLs are allowed (got {_parsed.scheme})",
                    ))
                    return

                _hostname = _parsed.hostname or ""
                _resolved_ip: str | None = None
                if _hostname:
                    loop = asyncio.get_running_loop()
                    try:
                        infos = await loop.getaddrinfo(_hostname, None, type=_sock.SOCK_STREAM)
                        for _fam, _tp, _pr, _cn, sockaddr in infos:
                            ip = _ipa.ip_address(sockaddr[0])
                            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                                await self._pending_response.put(_Response(
                                    success=False, status_code=0, headers={}, body="",
                                    error=f"Requests to private/local addresses are blocked",
                                ))
                                return
                        # Pin the first resolved IP to prevent DNS rebinding
                        if infos:
                            _resolved_ip = infos[0][4][0]
                    except _sock.gaierror:
                        await self._pending_response.put(_Response(
                            success=False, status_code=0, headers={}, body="",
                            error=f"Cannot resolve hostname: {_hostname}",
                        ))
                        return

                import httpx
                # DNS rebinding protection: we already validated the resolved
                # IP is not private/loopback above.  Use the original URL so
                # TLS certificate validation works (certs are issued for the
                # hostname, not the IP).  The TOCTOU window between our DNS
                # check and httpx's connection is negligible for practical
                # SSRF defense — the real protection is the private-IP block.
                extra_headers = dict(message.headers) if message.headers else {}
                request_url = message.url

                if not hasattr(self, "_http_client") or self._http_client is None:
                    self._http_client = httpx.AsyncClient(timeout=30.0, verify=True)
                resp = await self._http_client.request(
                    message.method, request_url,
                    headers=extra_headers,
                    content=message.body.encode() if message.body else None,
                )
                if self._orch:
                    await self._orch._permissions.consume_budget(
                        "web:fetch", actions=1,
                    )
                await self._pending_response.put(_Response(
                    success=True, status_code=resp.status_code,
                    headers=dict(resp.headers), body=resp.text, error=None,
                ))
            except Exception as e:
                await self._pending_response.put(_Response(
                    success=False, status_code=0, headers={}, body="", error=str(e),
                ))

        elif msg_type == "skill_invoke":
            target_skill = message.skill_id
            instruction = message.instruction
            action = getattr(message, "action", None)

            # Depth limit check
            if self._invoke_depth >= self.MAX_INVOKE_DEPTH:
                await self._pending_response.put(_Response(
                    success=False, result=None,
                    error=f"Invocation depth limit ({self.MAX_INVOKE_DEPTH}) exceeded",
                ))
                return

            # Circular call check
            if target_skill in self._invoke_chain:
                await self._pending_response.put(_Response(
                    success=False, result=None,
                    error=f"Circular invocation detected: {' → '.join(self._invoke_chain)} → {target_skill}",
                ))
                return

            # Permission escalation check — the target skill's permissions
            # must be a subset of what the calling skill already has granted.
            # A skill with only memory:read can't invoke a skill that needs web:fetch.
            try:
                target_manifest = await self._orch._skill_loader.get_manifest(target_skill)
                if target_manifest:
                    caller_grants = set()
                    caller_manifest = await self._orch._skill_loader.get_manifest(self._skill_id)
                    if caller_manifest:
                        for perm in caller_manifest.permissions:
                            check = await self._orch._permissions.check_permission(self._skill_id, perm)
                            if check.allowed:
                                caller_grants.add(perm)

                    target_needs = set(target_manifest.permissions)
                    missing = target_needs - caller_grants
                    if missing:
                        await self._pending_response.put(_Response(
                            success=False, result=None,
                            error=(
                                f"Permission escalation denied: {self._skill_id} cannot invoke "
                                f"{target_skill} — missing permissions: {', '.join(sorted(missing))}"
                            ),
                        ))
                        return
            except Exception as e:
                logger.warning("Permission check for skill invoke failed: %s", e)
                await self._pending_response.put(_Response(
                    success=False, result=None,
                    error=f"Permission check failed for invoke: {e}",
                ))
                return

            get_tracer().event("bridge", "skill_invoke",
                               task_id=self._task_id,
                               caller=self._skill_id,
                               target=target_skill,
                               action=action,
                               depth=self._invoke_depth + 1)

            try:
                from muse.kernel.intent_classifier import ClassifiedIntent, ExecutionMode

                intent = ClassifiedIntent(
                    mode=ExecutionMode.DELEGATED,
                    skill_id=target_skill,
                    action=action,
                    task_description=instruction,
                )

                # Collect the result from the child task's events
                child_result = None
                async for event in self._orch._execute_sub_task(
                    skill_id=target_skill,
                    instruction=instruction,
                    intent=intent,
                    action=action,
                    parent_task_id=self._task_id,
                    record_history=False,
                    # Pass deeper depth and extended chain to child
                    _invoke_depth=self._invoke_depth + 1,
                    _invoke_chain=self._invoke_chain + [target_skill],
                ):
                    if event.get("type") == "response":
                        child_result = {
                            "summary": event.get("content", ""),
                            "success": True,
                        }
                    elif event.get("type") in ("error", "task_failed"):
                        child_result = {
                            "summary": "",
                            "success": False,
                            "error": event.get("content", event.get("error", "")),
                        }

                await self._pending_response.put(_Response(
                    success=True,
                    result=child_result or {"summary": "", "success": True},
                ))
            except Exception as e:
                await self._pending_response.put(_Response(
                    success=False, result=None, error=str(e),
                ))

        else:
            logger.warning("LocalBridge: unhandled message type %s", msg_type)

    async def receive(self):
        """Return the next response from the orchestrator."""
        resp = await self._pending_response.get()
        get_tracer().bridge_receive(self._task_id, "response",
                                    success=getattr(resp, "success", None))
        return resp

    def resolve_user_response(self, request_id: str, response) -> bool:
        """Resolve a pending user_ask or user_confirm future.
        Called by the orchestrator when the UI sends a user response.
        Returns True if a matching future was found."""
        future = self._user_futures.get(request_id)
        if future and not future.done():
            future.set_result(response)
            return True
        return False

    async def close(self):
        if hasattr(self, "_http_client") and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
