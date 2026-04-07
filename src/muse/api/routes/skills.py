"""Skill management REST endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from muse.api.app import get_service, require_orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("")
async def list_skills(orchestrator=Depends(require_orchestrator)):
    """List installed skills with full manifest details and permission status."""

    installed = await get_service("skill_loader").get_installed()

    # Batch-fetch active grants for all skills in one query
    skill_ids = [s["skill_id"] for s in installed]
    grants_by_skill: dict[str, list[dict]] = {}
    try:
        grants_by_skill = await get_service("permissions").permission_repo.get_active_grants_batch(skill_ids)
    except Exception as e:
        logger.debug("Failed to batch-fetch grants: %s", e)

    skills = []
    for skill_data in installed:
        skill_id = skill_data["skill_id"]
        manifest = skill_data.get("manifest", {})
        granted_perms = [g["permission"] for g in grants_by_skill.get(skill_id, [])]

        skills.append({
            "skill_id": skill_id,
            "name": manifest.get("name", skill_id),
            "description": manifest.get("description", ""),
            "version": manifest.get("version", "0.0.0"),
            "author": manifest.get("author", ""),
            "permissions": manifest.get("permissions", []),
            "granted_permissions": granted_perms,
            "memory_namespaces": manifest.get("memory_namespaces", []),
            "allowed_domains": manifest.get("allowed_domains", []),
            "isolation_tier": manifest.get("isolation_tier", "standard"),
            "is_first_party": manifest.get("is_first_party", False),
            "max_tokens": manifest.get("max_tokens", 4000),
            "timeout_seconds": manifest.get("timeout_seconds", 300),
            "actions": manifest.get("actions", []),
            "credentials": manifest.get("credentials", []),
            "installed_at": skill_data.get("installed_at", ""),
            "updated_at": skill_data.get("updated_at", ""),
        })

    return {"skills": skills}


@router.get("/{skill_id}")
async def get_skill(skill_id: str, orchestrator=Depends(require_orchestrator)):
    """Get details of an installed skill."""
    manifest = await get_service("skill_loader").get_manifest(skill_id)
    if not manifest:
        return {"error": "Skill not found"}
    return {"skill": manifest.to_dict()}


@router.get("/{skill_id}/settings")
async def get_skill_settings(skill_id: str, orchestrator=Depends(require_orchestrator)):
    """Get a skill's credential specs and their configured status."""

    manifest = await get_service("skill_loader").get_manifest(skill_id)
    if not manifest:
        return {"error": "Skill not found"}

    credentials = []
    for spec in manifest.credentials:
        # Check if this credential is already stored in the vault
        configured = False
        try:
            secret = await get_service("vault").retrieve(spec.id)
            configured = bool(secret)
        except Exception as e:
            logger.debug("Failed to check credential %s: %s", spec.id, e)

        credentials.append({
            **spec.to_dict(),
            "configured": configured,
        })

    return {"skill_id": skill_id, "credentials": credentials}


@router.post("/{skill_id}/credentials")
async def store_skill_credential(skill_id: str, body: dict, orchestrator=Depends(require_orchestrator)):
    """Store a credential for a skill (via the vault)."""

    manifest = await get_service("skill_loader").get_manifest(skill_id)
    if not manifest:
        return {"error": "Skill not found"}

    credential_id = body.get("id", "")
    secret = body.get("secret", "")
    if not credential_id or not secret:
        return {"error": "Missing id or secret"}

    # Verify the credential is declared by this skill
    valid_ids = {spec.id for spec in manifest.credentials}
    if credential_id not in valid_ids:
        return {"error": f"Credential '{credential_id}' not declared by this skill"}

    await get_service("vault").store(
        credential_id=credential_id,
        secret=secret,
        credential_type=body.get("type", "api_key"),
        service_name=manifest.name,
    )
    return {"status": "stored", "id": credential_id}


@router.delete("/{skill_id}/credentials/{credential_id}")
async def delete_skill_credential(skill_id: str, credential_id: str, orchestrator=Depends(require_orchestrator)):
    """Delete a credential for a skill."""
    await get_service("vault").delete(credential_id)
    return {"status": "deleted", "id": credential_id}


@router.delete("/{skill_id}")
async def uninstall_skill(skill_id: str, orchestrator=Depends(require_orchestrator)):
    """Uninstall a skill."""
    await get_service("skill_loader").uninstall(skill_id)
    await get_service("permissions").permission_repo.revoke_all_for_skill(skill_id)
    await orchestrator._rebuild_skills_catalog()
    return {"status": "uninstalled", "skill_id": skill_id}


# ------------------------------------------------------------------
# Skill defaults per category
# ------------------------------------------------------------------

@router.get("/defaults")
async def get_skill_defaults(orchestrator=Depends(require_orchestrator)):
    """Return the user's preferred skill for each category."""

    # Collect all categories from installed skills
    installed = await get_service("skill_loader").get_installed()
    categories: dict[str, list[dict]] = {}
    for skill in installed:
        m = skill.get("manifest", {})
        cat = m.get("category", "")
        if cat:
            categories.setdefault(cat, []).append({
                "skill_id": skill["skill_id"],
                "name": m.get("name", skill["skill_id"]),
            })

    # Load user preferences
    defaults: dict[str, str] = {}
    for cat in categories:
        try:
            async with get_service("db").execute(
                "SELECT value FROM user_settings WHERE key = ?",
                (f"skill_default.{cat}",),
            ) as cursor:
                row = await cursor.fetchone()
            if row and row[0]:
                defaults[cat] = row[0]
        except Exception:
            pass

    return {"defaults": defaults, "categories": categories}


@router.put("/defaults/{category}")
async def set_skill_default(category: str, body: dict, orchestrator=Depends(require_orchestrator)):
    """Set the preferred skill for a category."""
    from datetime import datetime, timezone

    skill_id = body.get("skill_id", "").strip()
    key = f"skill_default.{category}"
    now = datetime.now(timezone.utc).isoformat()

    if not skill_id:
        # Clear the preference
        await get_service("db").execute(
            "DELETE FROM user_settings WHERE key = ?", (key,)
        )
    else:
        await get_service("db").execute(
            "INSERT OR REPLACE INTO user_settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, skill_id, now),
        )
    await get_service("db").commit()
    return {"category": category, "skill_id": skill_id or None}
