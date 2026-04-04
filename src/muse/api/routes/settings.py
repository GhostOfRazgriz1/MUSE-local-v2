"""Settings REST endpoints."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from muse.api.app import get_orchestrator, get_service
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
async def get_settings():
    """Get all user settings."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"settings": {}}

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
async def get_local_config():
    """Return the stored local server configuration."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"config": None}

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
async def set_local_config(body: dict):
    """Save local server configuration and hot-reload the provider.

    Body: ``{"runtime": "ollama", "address": "localhost", "port": 11434,
             "models": ["llama3.2", "gemma2"], "max_workers": 2}``
    """
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Not ready")

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


@router.get("/models")
async def list_models():
    """List available LLM models from the provider."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"models": []}

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
async def get_model_overrides():
    """Get per-skill model overrides."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"overrides": {}}
    return {"overrides": await get_service("model_router").get_skill_overrides()}


@router.put("/models/overrides/{skill_id}")
async def set_model_override(skill_id: str, body: dict):
    """Set a model override for a skill."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await get_service("model_router").set_skill_override(skill_id, body["model_id"])
    return {"skill_id": skill_id, "model_id": body["model_id"]}


@router.get("/providers")
async def list_providers():
    """Return LLM providers and their status."""
    orchestrator = get_orchestrator()
    registered = set(get_service("provider").providers.keys()) if orchestrator else set()

    providers = []
    for prefix, pdef in BUILTIN_PROVIDERS.items():
        providers.append({
            "id": prefix,
            "name": pdef.name,
            "env_var": pdef.env_var,
            "source": "env" if prefix in registered else None,
            "is_custom": False,
        })

    return {"providers": providers}


# ------------------------------------------------------------------
# Generic credentials (for skills / OAuth)
# ------------------------------------------------------------------

@router.get("/credentials")
async def list_credentials():
    """List stored credentials (metadata only, no secrets)."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"credentials": []}
    return {"credentials": await get_service("vault").list_credentials()}


@router.post("/credentials")
async def store_credential(body: dict):
    """Store a new credential."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
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
async def delete_credential(credential_id: str):
    """Delete a credential."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await get_service("vault").delete(credential_id)
    # Re-evaluate skill routing without the removed credential
    try:
        await orchestrator.refresh_skill_registration()
    except Exception:
        pass
    return {"status": "deleted"}
