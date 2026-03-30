"""Settings REST endpoints."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter

from muse.api.app import get_orchestrator
from muse.config import BUILTIN_PROVIDERS

router = APIRouter(prefix="/settings", tags=["settings"])


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
    """Set a user setting."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}

    value = body.get("value", "")
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

@router.get("/providers")
async def list_providers():
    """Return every built-in LLM provider and whether a key is configured."""
    orchestrator = get_orchestrator()
    registered = set(orchestrator._provider.providers.keys()) if orchestrator else set()

    providers = []
    for prefix, pdef in BUILTIN_PROVIDERS.items():
        has_env = bool(os.environ.get(pdef.env_var))
        is_registered = prefix in registered
        # Check if key is in vault (env keys are synced to vault on startup)
        has_vault = False
        if orchestrator:
            stored = await orchestrator._vault.retrieve_raw(f"{prefix}_api_key")
            has_vault = stored is not None
        # Report "vault" if the key is manageable from the UI,
        # even if it originally came from an env var.
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
        })
    return {"providers": providers}


@router.put("/providers/{provider_id}/key")
async def set_provider_key(provider_id: str, body: dict):
    """Store an API key for *provider_id* and hot-register it."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}

    pdef = BUILTIN_PROVIDERS.get(provider_id)
    if pdef is None:
        return {"error": f"Unknown provider '{provider_id}'"}

    secret = body.get("key", "").strip()
    if not secret:
        return {"error": "key is required"}

    # Persist in the credential vault (OS keychain).
    credential_id = f"{provider_id}_api_key"
    await orchestrator._vault.store(
        credential_id=credential_id,
        secret=secret,
        credential_type="api_key",
        service_name=pdef.name,
    )

    # Hot-register (or replace) so the provider is usable immediately.
    from muse.providers.registry import ProviderRegistry

    registry: ProviderRegistry = orchestrator._provider

    # OpenRouter is the fallback provider — update its key directly
    # rather than registering a new named provider.
    if provider_id == "openrouter" and registry._fallback is not None:
        registry._fallback._api_key = secret
    else:
        # Close the old instance if we're replacing it.
        old = registry.providers.get(provider_id)
        if old is not None and hasattr(old, "close"):
            await old.close()

        if pdef.api_style == "anthropic":
            from muse.providers.anthropic import AnthropicProvider
            registry.register(provider_id, AnthropicProvider(api_key=secret))
        else:
            from muse.providers.openai_compat import OpenAICompatibleProvider
            registry.register(
                provider_id,
                OpenAICompatibleProvider(
                    name=pdef.name, api_key=secret,
                    base_url=pdef.base_url, env_var=pdef.env_var,
                ),
            )

    return {"status": "stored", "provider": provider_id}


@router.delete("/providers/{provider_id}/key")
async def delete_provider_key(provider_id: str):
    """Remove a stored API key and unregister the provider."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}

    pdef = BUILTIN_PROVIDERS.get(provider_id)
    if pdef is None:
        return {"error": f"Unknown provider '{provider_id}'"}

    credential_id = f"{provider_id}_api_key"
    await orchestrator._vault.delete(credential_id)

    # Unregister so requests fall back to OpenRouter.
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
    return {"status": "stored", "id": body["id"]}


@router.delete("/credentials/{credential_id}")
async def delete_credential(credential_id: str):
    """Delete a credential."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await orchestrator._vault.delete(credential_id)
    return {"status": "deleted"}
