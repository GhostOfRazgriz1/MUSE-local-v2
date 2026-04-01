"""Settings REST endpoints."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from muse.api.app import get_orchestrator
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
async def get_settings():
    """Get all user settings."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"settings": {}}

    async with orchestrator._db.execute("SELECT key, value FROM user_settings") as cursor:
        rows = await cursor.fetchall()
    return {"settings": {row[0]: row[1] for row in rows}}


@router.put("/{key}")
async def set_setting(key: str, body: dict):
    """Set a user setting (restricted to whitelisted keys)."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}

    if not _is_allowed_key(key):
        raise HTTPException(400, f"Setting key not allowed: {key}")

    value = body.get("value", "")

    # Route OAuth client secrets to the vault instead of plaintext DB.
    if _OAUTH_CLIENT_SECRET_RE.match(key):
        await orchestrator._vault.store(
            credential_id=key,
            secret=str(value),
            credential_type="oauth_client_secret",
            service_name=key.split(".")[1],
        )
        return {"key": key, "value": "(stored in vault)"}

    now = datetime.now(timezone.utc).isoformat()
    await orchestrator._db.execute(
        "INSERT OR REPLACE INTO user_settings (key, value, updated_at) VALUES (?, ?, ?)",
        (key, str(value), now),
    )
    await orchestrator._db.commit()
    return {"key": key, "value": value}


@router.get("/models")
async def list_models():
    """List available LLM models from the provider."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"models": []}

    try:
        models = await orchestrator._provider.list_models()
        return {
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "provider": m.id.split("/")[0] if "/" in m.id else "other",
                    "context_window": m.context_window,
                    "input_price": m.input_price_per_token,
                    "output_price": m.output_price_per_token,
                }
                for m in models
            ]
        }
    except Exception as e:
        return {"models": [], "error": str(e)}


@router.get("/models/overrides")
async def get_model_overrides():
    """Get per-skill model overrides."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"overrides": {}}
    return {"overrides": await orchestrator._model_router.get_skill_overrides()}


@router.put("/models/overrides/{skill_id}")
async def set_model_override(skill_id: str, body: dict):
    """Set a model override for a skill."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await orchestrator._model_router.set_skill_override(skill_id, body["model_id"])
    return {"skill_id": skill_id, "model_id": body["model_id"]}


# ------------------------------------------------------------------
# LLM Provider API-key management
# ------------------------------------------------------------------

async def _load_custom_providers(orchestrator) -> list[dict]:
    """Load custom provider definitions from user_settings."""
    try:
        async with orchestrator._db.execute(
            "SELECT value FROM user_settings WHERE key = 'custom_providers'"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception:
        pass
    return []


async def _save_custom_providers(orchestrator, providers: list[dict]) -> None:
    """Persist custom provider definitions to user_settings."""
    now = datetime.now(timezone.utc).isoformat()
    await orchestrator._db.execute(
        "INSERT INTO user_settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        ("custom_providers", json.dumps(providers), now),
    )
    await orchestrator._db.commit()


@router.get("/providers")
async def list_providers():
    """Return all LLM providers (built-in + custom) and whether a key is configured."""
    orchestrator = get_orchestrator()
    registered = set(orchestrator._provider.providers.keys()) if orchestrator else set()

    providers = []
    for prefix, pdef in BUILTIN_PROVIDERS.items():
        has_env = bool(os.environ.get(pdef.env_var))
        is_registered = prefix in registered
        has_vault = False
        if orchestrator:
            stored = await orchestrator._vault.retrieve_raw(f"{prefix}_api_key")
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
    if orchestrator:
        custom = await _load_custom_providers(orchestrator)
        for cp in custom:
            cp_id = cp["id"]
            has_vault = False
            stored = await orchestrator._vault.retrieve_raw(f"{cp_id}_api_key")
            has_vault = stored is not None
            providers.append({
                "id": cp_id,
                "name": cp.get("name", cp_id),
                "env_var": "",
                "source": "vault" if has_vault else None,
                "is_custom": True,
                "base_url": cp.get("base_url", ""),
                "api_style": cp.get("api_style", "openai"),
            })

    return {"providers": providers}


@router.put("/providers/{provider_id}/key")
async def set_provider_key(provider_id: str, body: dict):
    """Store an API key for *provider_id* and hot-register it."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Not ready")

    secret = body.get("key", "").strip()
    if not secret:
        raise HTTPException(400, "key is required")

    # Look up provider definition — built-in or custom.
    pdef = BUILTIN_PROVIDERS.get(provider_id)
    custom_def = None
    if pdef is None:
        custom = await _load_custom_providers(orchestrator)
        custom_def = next((c for c in custom if c["id"] == provider_id), None)
        if custom_def is None:
            raise HTTPException(404, f"Unknown provider '{provider_id}'")

    name = pdef.name if pdef else custom_def["name"]
    base_url = pdef.base_url if pdef else custom_def["base_url"]
    api_style = pdef.api_style if pdef else custom_def.get("api_style", "openai")

    # Persist in the credential vault (OS keychain).
    credential_id = f"{provider_id}_api_key"
    await orchestrator._vault.store(
        credential_id=credential_id,
        secret=secret,
        credential_type="api_key",
        service_name=name,
    )

    # Hot-register (or replace) so the provider is usable immediately.
    from muse.providers.registry import ProviderRegistry

    registry: ProviderRegistry = orchestrator._provider

    # OpenRouter is the fallback provider — update its key directly.
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
                OpenAICompatibleProvider(
                    name=name, api_key=secret,
                    base_url=base_url,
                ),
            )

    return {"status": "stored", "provider": provider_id}


@router.delete("/providers/{provider_id}/key")
async def delete_provider_key(provider_id: str):
    """Remove a stored API key and unregister the provider."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Not ready")

    # Allow deleting keys for both built-in and custom providers.
    pdef = BUILTIN_PROVIDERS.get(provider_id)
    if pdef is None:
        custom = await _load_custom_providers(orchestrator)
        if not any(c["id"] == provider_id for c in custom):
            raise HTTPException(404, f"Unknown provider '{provider_id}'")

    credential_id = f"{provider_id}_api_key"
    await orchestrator._vault.delete(credential_id)

    from muse.providers.registry import ProviderRegistry

    registry: ProviderRegistry = orchestrator._provider
    if provider_id == "openrouter" and registry._fallback is not None:
        registry._fallback._api_key = ""
    elif provider_id in registry.providers:
        provider = registry.providers[provider_id]
        if hasattr(provider, "close"):
            await provider.close()
        registry.unregister(provider_id)

    return {"status": "deleted", "provider": provider_id}


# ------------------------------------------------------------------
# Custom providers
# ------------------------------------------------------------------

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")


@router.post("/providers/custom")
async def add_custom_provider(body: dict):
    """Register a custom OpenAI-compatible provider.

    Body: ``{"name": "My Ollama", "base_url": "http://localhost:11434/v1",
             "api_key": "...", "api_style": "openai"}``
    """
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Not ready")

    name = (body.get("name") or "").strip()
    base_url = (body.get("base_url") or "").strip().rstrip("/")
    api_key = (body.get("api_key") or "").strip()
    api_style = body.get("api_style", "openai")

    if not name:
        raise HTTPException(400, "name is required")
    if not base_url:
        raise HTTPException(400, "base_url is required")
    if api_style not in ("openai", "anthropic"):
        raise HTTPException(400, "api_style must be 'openai' or 'anthropic'")

    # Generate a stable ID from the name
    provider_id = "custom_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:25]
    if not _SAFE_ID_RE.match(provider_id):
        provider_id = "custom_provider"

    # Ensure uniqueness
    custom = await _load_custom_providers(orchestrator)
    existing_ids = {c["id"] for c in custom}
    base_id = provider_id
    counter = 1
    while provider_id in existing_ids or provider_id in BUILTIN_PROVIDERS:
        provider_id = f"{base_id}_{counter}"
        counter += 1

    # Store definition
    custom.append({
        "id": provider_id,
        "name": name,
        "base_url": base_url,
        "api_style": api_style,
    })
    await _save_custom_providers(orchestrator, custom)

    # Store API key if provided
    if api_key:
        await orchestrator._vault.store(
            credential_id=f"{provider_id}_api_key",
            secret=api_key,
            credential_type="api_key",
            service_name=name,
        )

        # Hot-register
        from muse.providers.registry import ProviderRegistry
        registry: ProviderRegistry = orchestrator._provider

        if api_style == "anthropic":
            from muse.providers.anthropic import AnthropicProvider
            registry.register(provider_id, AnthropicProvider(api_key=api_key))
        else:
            from muse.providers.openai_compat import OpenAICompatibleProvider
            registry.register(
                provider_id,
                OpenAICompatibleProvider(name=name, api_key=api_key, base_url=base_url),
            )

    return {"status": "created", "provider_id": provider_id, "name": name}


@router.delete("/providers/custom/{provider_id}")
async def delete_custom_provider(provider_id: str):
    """Remove a custom provider and its stored key."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Not ready")

    custom = await _load_custom_providers(orchestrator)
    found = [c for c in custom if c["id"] == provider_id]
    if not found:
        raise HTTPException(404, f"Custom provider '{provider_id}' not found")

    # Remove definition
    custom = [c for c in custom if c["id"] != provider_id]
    await _save_custom_providers(orchestrator, custom)

    # Remove key
    await orchestrator._vault.delete(f"{provider_id}_api_key")

    # Unregister
    from muse.providers.registry import ProviderRegistry
    registry: ProviderRegistry = orchestrator._provider
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
async def list_credentials():
    """List stored credentials (metadata only, no secrets)."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"credentials": []}
    return {"credentials": await orchestrator._vault.list_credentials()}


@router.post("/credentials")
async def store_credential(body: dict):
    """Store a new credential."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await orchestrator._vault.store(
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
async def delete_credential(credential_id: str):
    """Delete a credential."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await orchestrator._vault.delete(credential_id)
    # Re-evaluate skill routing without the removed credential
    try:
        await orchestrator.refresh_skill_registration()
    except Exception:
        pass
    return {"status": "deleted"}
