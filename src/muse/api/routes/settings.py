"""Settings REST endpoints."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from muse.api.app import get_service, require_orchestrator
from muse.config import BUILTIN_PROVIDERS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

# Allowed setting keys.  Only keys matching one of these prefixes
# can be written via the generic PUT /{key} endpoint.  This prevents
# an authenticated caller from overwriting internal keys or storing
# arbitrary data.
_ALLOWED_KEY_PREFIXES = (
    "workspace.",
    "proactivity.",
    "default_model",
    "daily_budget",
    "autonomy_level",
    "response_style",
    "auto_grant_first_party",
    "font.",
    "user_name",
    "user_city",
    "language",
    "local_server",
    "max_concurrent_tasks",
)

# OAuth client IDs are safe to store in user_settings (not secret).
# OAuth client *secrets* are routed to the vault instead.
_OAUTH_CLIENT_ID_RE = re.compile(r"^oauth\.[a-z_]+\.client_id$")
_OAUTH_CLIENT_SECRET_RE = re.compile(r"^oauth\.[a-z_]+\.client_secret$")


def _is_allowed_key(key: str) -> bool:
    """Check if a setting key is in the whitelist."""
    if any(key.startswith(p) for p in _ALLOWED_KEY_PREFIXES):
        return True
    if _OAUTH_CLIENT_ID_RE.match(key):
        return True
    if _OAUTH_CLIENT_SECRET_RE.match(key):
        return True  # handled specially — routed to vault
    return False


@router.get("")
async def get_settings(orchestrator=Depends(require_orchestrator)):
    """Get all user settings."""

    async with get_service("db").execute("SELECT key, value FROM user_settings") as cursor:
        rows = await cursor.fetchall()
    return {"settings": {row[0]: row[1] for row in rows}}


# ------------------------------------------------------------------
# Local server configuration (MUST be before the /{key} catch-all)
# ------------------------------------------------------------------

_DEFAULT_PORTS = {
    "ollama": 11434,
    "vllm": 8000,
    "llama.cpp": 8080,
    "other": 8000,
}


@router.get("/local")
async def get_local_config(orchestrator=Depends(require_orchestrator)):
    """Return the stored local server configuration."""

    try:
        async with get_service("db").execute(
            "SELECT value FROM user_settings WHERE key = 'local_server'"
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            return {"config": json.loads(row[0])}
    except Exception:
        pass
    return {"config": None}


@router.put("/local")
async def set_local_config(body: dict, orchestrator=Depends(require_orchestrator)):
    """Save local server configuration and hot-reload the provider.

    Body: ``{"runtime": "ollama", "address": "localhost", "port": 11434,
             "models": ["llama3.2", "gemma2"], "max_workers": 2}``
    """

    runtime = body.get("runtime", "ollama")
    address = body.get("address", "localhost").strip()
    port = int(body.get("port", _DEFAULT_PORTS.get(runtime, 8000)))
    model_names = body.get("models", [])
    max_workers = int(body.get("max_workers", 2))

    if not address:
        raise HTTPException(400, "address is required")
    if not model_names:
        raise HTTPException(400, "At least one model name is required")
    if max_workers < 1:
        max_workers = 1
    if max_workers > 16:
        max_workers = 16

    config_data = {
        "runtime": runtime,
        "address": address,
        "port": port,
        "models": model_names,
        "max_workers": max_workers,
    }

    # Persist
    now = datetime.now(timezone.utc).isoformat()
    await get_service("db").execute(
        "INSERT INTO user_settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        ("local_server", json.dumps(config_data), now),
    )
    await get_service("db").commit()

    # Hot-reload the local provider with the new URL
    base_url = f"http://{address}:{port}/v1"

    from muse.providers.local import LocalProvider
    from muse.providers.registry import ProviderRegistry

    registry: ProviderRegistry = get_service("provider")
    old = registry.providers.get("local")
    if old is not None and hasattr(old, "close"):
        await old.close()

    new_prov = LocalProvider(base_url=base_url, name="local")
    registry.register("local", new_prov)

    # Update max concurrent tasks
    get_service("task_manager")._max_concurrent = max_workers

    # Auto-select first model as default
    if model_names:
        default_model = f"local/{model_names[0]}"
        get_service("model_router").default_model = default_model
        get_service("classifier").set_provider(get_service("provider"), default_model)

        # Persist default model
        await get_service("db").execute(
            "INSERT INTO user_settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            ("default_model", default_model, now),
        )
        await get_service("db").commit()

    logger.info("Local server reconfigured: %s (%s) with %d models, %d workers",
                runtime, base_url, len(model_names), max_workers)

    return {"status": "configured", "base_url": base_url, "models": model_names}


@router.post("/local/test")
async def test_local_connection(body: dict):
    """Test connectivity to a local LLM server without saving config."""
    address = body.get("address", "localhost").strip()
    port = int(body.get("port", 11434))
    base_url = f"http://{address}:{port}/v1"

    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            resp = await client.get(f"{base_url}/models")
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("id", "") for m in data.get("data", [])]
                return {"status": "ok", "models": models}
            return {"status": "error", "message": f"Server returned {resp.status_code}"}
    except httpx.ConnectError:
        return {"status": "error", "message": f"Cannot connect to {base_url}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ------------------------------------------------------------------
# Generic settings
# ------------------------------------------------------------------

@router.put("/{key}")
async def set_setting(key: str, body: dict, orchestrator=Depends(require_orchestrator)):
    """Set a user setting (restricted to whitelisted keys)."""

    if not _is_allowed_key(key):
        raise HTTPException(400, f"Setting key not allowed: {key}")

    value = body.get("value", "")

    # Route OAuth client secrets to the vault instead of plaintext DB.
    if _OAUTH_CLIENT_SECRET_RE.match(key):
        await get_service("vault").store(
            credential_id=key,
            secret=str(value),
            credential_type="oauth_client_secret",
            service_name=key.split(".")[1],
        )
        return {"key": key, "value": "(stored in vault)"}

    now = datetime.now(timezone.utc).isoformat()
    await get_service("db").execute(
        "INSERT OR REPLACE INTO user_settings (key, value, updated_at) VALUES (?, ?, ?)",
        (key, str(value), now),
    )
    await get_service("db").commit()

    # Hot-reload language preference so it takes effect immediately.
    if key == "language":
        get_service("session").user_language = str(value).strip()

    # Hot-reload default model so the change takes effect immediately.
    if key == "default_model":
        model_id = str(value).strip()
        get_service("model_router").default_model = model_id
        get_service("classifier").set_provider(get_service("provider"), model_id)

    return {"key": key, "value": value}


@router.get("/mcp-server")
async def mcp_server_status(orchestrator=Depends(require_orchestrator)):
    """Get MCP server status and connection token."""
    db = get_service("db")
    async with db.execute(
        "SELECT value FROM user_settings WHERE key = 'mcp_server_enabled'"
    ) as cursor:
        row = await cursor.fetchone()
    enabled = bool(row and row[0] == "true")

    token = None
    if enabled:
        from muse.mcp.server import get_or_create_mcp_token
        token = await get_or_create_mcp_token(db)

    return {"enabled": enabled, "token": token, "endpoint": "/mcp"}


@router.put("/mcp-server")
async def set_mcp_server(body: dict, orchestrator=Depends(require_orchestrator)):
    """Enable or disable the MCP server.

    Body: ``{"enabled": true}``
    Returns the auth token when enabling (needed by external agents).
    Requires server restart to take effect.
    """
    enabled = bool(body.get("enabled", False))
    db = get_service("db")
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        "INSERT OR REPLACE INTO user_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("mcp_server_enabled", "true" if enabled else "false", now),
    )
    await db.commit()

    token = None
    if enabled:
        from muse.mcp.server import get_or_create_mcp_token
        token = await get_or_create_mcp_token(db)

        # Hot-start the server if not already running
        from muse.mcp import server_route
        if server_route._server_instance is None:
            from muse.mcp.server import MuseMCPServer
            mcp_srv = MuseMCPServer(
                memory_repo=get_service("memory_repo"),
                embedding_service=get_service("embeddings"),
            )
            server_route.configure(mcp_srv, db)
            logger.info("MCP server hot-started via settings")

    return {"enabled": enabled, "token": token, "endpoint": "/mcp"}


@router.get("/models")
async def list_models(orchestrator=Depends(require_orchestrator)):
    """List available LLM models from the provider."""

    try:
        models = await get_service("provider").list_models()
        result = []
        for m in models:
            prefix = m.id.split("/")[0] if "/" in m.id else "local"
            result.append({
                "id": m.id,
                "name": m.name,
                "provider": prefix,
                "served_by": "local",
                "context_window": m.context_window,
                "input_price": m.input_price_per_token,
                "output_price": m.output_price_per_token,
            })
        return {"models": result}
    except Exception as e:
        return {"models": [], "error": str(e)}


@router.get("/models/overrides")
async def get_model_overrides(orchestrator=Depends(require_orchestrator)):
    """Get per-skill model overrides."""
    return {"overrides": await get_service("model_router").get_skill_overrides()}


@router.put("/models/overrides/{skill_id}")
async def set_model_override(skill_id: str, body: dict, orchestrator=Depends(require_orchestrator)):
    """Set a model override for a skill."""
    await get_service("model_router").set_skill_override(skill_id, body["model_id"])
    return {"skill_id": skill_id, "model_id": body["model_id"]}


@router.get("/providers")
async def list_providers(orchestrator=Depends(require_orchestrator)):
    """Return all LLM providers (built-in + custom) and whether a key is configured."""
    import os
    registered = set(get_service("provider").providers.keys())

    providers = []
    for prefix, pdef in BUILTIN_PROVIDERS.items():
        is_registered = prefix in registered
        # Local provider: no key needed
        if not pdef.env_var:
            source = "env" if is_registered else None
        else:
            has_env = bool(os.environ.get(pdef.env_var))
            stored = await get_service("vault").retrieve_raw(f"{prefix}_api_key")
            has_vault = stored is not None
            if has_vault:
                source = "vault"
            elif has_env or is_registered:
                source = "env"
            else:
                source = None
        providers.append({
            "id": prefix,
            "name": pdef.name,
            "env_var": pdef.env_var,
            "source": source,
            "is_custom": False,
        })

    # Append custom providers
    custom = await _load_custom_providers()
    for cp in custom:
        cp_id = cp["id"]
        stored = await get_service("vault").retrieve_raw(f"{cp_id}_api_key")
        providers.append({
            "id": cp_id,
            "name": cp.get("name", cp_id),
            "env_var": "",
            "source": "vault" if stored else None,
            "is_custom": True,
            "base_url": cp.get("base_url", ""),
            "api_style": cp.get("api_style", "openai"),
        })

    return {"providers": providers}


# ------------------------------------------------------------------
# Provider API key management
# ------------------------------------------------------------------

async def _load_custom_providers() -> list[dict]:
    try:
        async with get_service("db").execute(
            "SELECT value FROM user_settings WHERE key = 'custom_providers'"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception:
        pass
    return []


async def _save_custom_providers(providers: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await get_service("db").execute(
        "INSERT INTO user_settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        ("custom_providers", json.dumps(providers), now),
    )
    await get_service("db").commit()


@router.put("/providers/{provider_id}/key")
async def set_provider_key(provider_id: str, body: dict, orchestrator=Depends(require_orchestrator)):
    """Store an API key for a provider and hot-register it."""

    secret = body.get("key", "").strip()
    if not secret:
        raise HTTPException(400, "key is required")

    pdef = BUILTIN_PROVIDERS.get(provider_id)
    custom_def = None
    if pdef is None:
        custom = await _load_custom_providers()
        custom_def = next((c for c in custom if c["id"] == provider_id), None)
        if custom_def is None:
            raise HTTPException(404, f"Unknown provider '{provider_id}'")

    name = pdef.name if pdef else custom_def["name"]
    base_url = pdef.base_url if pdef else custom_def["base_url"]
    api_style = pdef.api_style if pdef else custom_def.get("api_style", "openai")

    await get_service("vault").store(
        credential_id=f"{provider_id}_api_key",
        secret=secret,
        credential_type="api_key",
        service_name=name,
    )

    # Hot-register the provider
    from muse.providers.registry import ProviderRegistry
    registry: ProviderRegistry = get_service("provider")

    if provider_id == "openrouter" and registry._fallback is not None:
        registry._fallback._api_key = secret
    else:
        old = registry.providers.get(provider_id)
        if old is not None and hasattr(old, "close"):
            await old.close()

        if api_style == "anthropic":
            from muse.providers.anthropic import AnthropicProvider
            registry.register(provider_id, AnthropicProvider(api_key=secret))
        else:
            from muse.providers.openai_compat import OpenAICompatibleProvider
            registry.register(
                provider_id,
                OpenAICompatibleProvider(name=name, api_key=secret, base_url=base_url),
            )

    return {"status": "stored", "provider": provider_id}


@router.delete("/providers/{provider_id}/key")
async def delete_provider_key(provider_id: str, orchestrator=Depends(require_orchestrator)):
    """Remove a stored API key and unregister the provider."""

    await get_service("vault").delete(f"{provider_id}_api_key")

    from muse.providers.registry import ProviderRegistry
    registry: ProviderRegistry = get_service("provider")
    if provider_id == "openrouter" and registry._fallback is not None:
        registry._fallback._api_key = ""
    elif provider_id in registry.providers:
        provider = registry.providers[provider_id]
        if hasattr(provider, "close"):
            await provider.close()
        registry.unregister(provider_id)

    return {"status": "deleted", "provider": provider_id}


@router.post("/providers/custom")
async def add_custom_provider(body: dict, orchestrator=Depends(require_orchestrator)):
    """Register a custom OpenAI-compatible provider."""

    name = (body.get("name") or "").strip()
    base_url = (body.get("base_url") or "").strip().rstrip("/")
    api_key = (body.get("api_key") or "").strip()
    api_style = body.get("api_style", "openai")

    if not name:
        raise HTTPException(400, "name is required")
    if not base_url:
        raise HTTPException(400, "base_url is required")

    provider_id = "custom_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:25]
    custom = await _load_custom_providers()
    existing_ids = {c["id"] for c in custom}
    base_id = provider_id
    counter = 1
    while provider_id in existing_ids or provider_id in BUILTIN_PROVIDERS:
        provider_id = f"{base_id}_{counter}"
        counter += 1

    custom.append({"id": provider_id, "name": name, "base_url": base_url, "api_style": api_style})
    await _save_custom_providers(custom)

    if api_key:
        await get_service("vault").store(
            credential_id=f"{provider_id}_api_key",
            secret=api_key,
            credential_type="api_key",
            service_name=name,
        )
        from muse.providers.registry import ProviderRegistry
        registry: ProviderRegistry = get_service("provider")
        if api_style == "anthropic":
            from muse.providers.anthropic import AnthropicProvider
            registry.register(provider_id, AnthropicProvider(api_key=api_key))
        else:
            from muse.providers.openai_compat import OpenAICompatibleProvider
            registry.register(provider_id, OpenAICompatibleProvider(name=name, api_key=api_key, base_url=base_url))

    return {"status": "created", "provider_id": provider_id, "name": name}


@router.delete("/providers/custom/{provider_id}")
async def delete_custom_provider(provider_id: str, orchestrator=Depends(require_orchestrator)):
    """Remove a custom provider and its key."""

    custom = await _load_custom_providers()
    if not any(c["id"] == provider_id for c in custom):
        raise HTTPException(404, f"Custom provider '{provider_id}' not found")

    custom = [c for c in custom if c["id"] != provider_id]
    await _save_custom_providers(custom)
    await get_service("vault").delete(f"{provider_id}_api_key")

    from muse.providers.registry import ProviderRegistry
    registry: ProviderRegistry = get_service("provider")
    if provider_id in registry.providers:
        provider = registry.providers[provider_id]
        if hasattr(provider, "close"):
            await provider.close()
        registry.unregister(provider_id)

    return {"status": "deleted", "provider_id": provider_id}


# ------------------------------------------------------------------
# Generic credentials (for skills / OAuth)
# ------------------------------------------------------------------

@router.get("/credentials")
async def list_credentials(orchestrator=Depends(require_orchestrator)):
    """List stored credentials (metadata only, no secrets)."""
    return {"credentials": await get_service("vault").list_credentials()}


@router.post("/credentials")
async def store_credential(body: dict, orchestrator=Depends(require_orchestrator)):
    """Store a new credential."""
    await get_service("vault").store(
        credential_id=body["id"],
        secret=body["secret"],
        credential_type=body.get("type", "api_key"),
        service_name=body.get("service_name", ""),
        linked_permission=body.get("linked_permission"),
    )
    # Re-evaluate skill routing with the new credential
    try:
        await orchestrator.refresh_skill_registration()
    except Exception:
        pass
    return {"status": "stored", "id": body["id"]}


@router.delete("/credentials/{credential_id}")
async def delete_credential(credential_id: str, orchestrator=Depends(require_orchestrator)):
    """Delete a credential."""
    await get_service("vault").delete(credential_id)
    # Re-evaluate skill routing without the removed credential
    try:
        await orchestrator.refresh_skill_registration()
    except Exception:
        pass
    return {"status": "deleted"}
