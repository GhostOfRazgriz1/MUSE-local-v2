"""Skill management REST endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from muse.api.app import get_orchestrator

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("")
async def list_skills():
    """List installed skills with full manifest details and permission status."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"skills": []}

    installed = await orchestrator._skill_loader.get_installed()

    # Enrich each skill with active permission grants
    skills = []
    for skill_data in installed:
        skill_id = skill_data["skill_id"]
        manifest = skill_data.get("manifest", {})

        # Collect active grants for this skill
        granted_perms: list[str] = []
        try:
            grants = await orchestrator._permissions.permission_repo.get_active_grants(
                skill_id,
            )
            granted_perms = [g["permission"] for g in grants]
        except Exception:
            pass

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
async def get_skill(skill_id: str):
    """Get details of an installed skill."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    manifest = await orchestrator._skill_loader.get_manifest(skill_id)
    if not manifest:
        return {"error": "Skill not found"}
    return {"skill": manifest.to_dict()}


@router.get("/{skill_id}/settings")
async def get_skill_settings(skill_id: str):
    """Get a skill's credential specs and their configured status."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}

    manifest = await orchestrator._skill_loader.get_manifest(skill_id)
    if not manifest:
        return {"error": "Skill not found"}

    credentials = []
    for spec in manifest.credentials:
        # Check if this credential is already stored in the vault
        configured = False
        try:
            secret = await orchestrator._vault.retrieve(spec.id)
            configured = bool(secret)
        except Exception:
            pass

        credentials.append({
            **spec.to_dict(),
            "configured": configured,
        })

    return {"skill_id": skill_id, "credentials": credentials}


@router.post("/{skill_id}/credentials")
async def store_skill_credential(skill_id: str, body: dict):
    """Store a credential for a skill (via the vault)."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}

    manifest = await orchestrator._skill_loader.get_manifest(skill_id)
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

    await orchestrator._vault.store(
        credential_id=credential_id,
        secret=secret,
        credential_type=body.get("type", "api_key"),
        service_name=manifest.name,
    )
    return {"status": "stored", "id": credential_id}


@router.delete("/{skill_id}/credentials/{credential_id}")
async def delete_skill_credential(skill_id: str, credential_id: str):
    """Delete a credential for a skill."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await orchestrator._vault.delete(credential_id)
    return {"status": "deleted", "id": credential_id}


@router.delete("/{skill_id}")
async def uninstall_skill(skill_id: str):
    """Uninstall a skill."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await orchestrator._skill_loader.uninstall(skill_id)
    await orchestrator._permissions.permission_repo.revoke_all_for_skill(skill_id)
    await orchestrator._rebuild_skills_catalog()
    return {"status": "uninstalled", "skill_id": skill_id}
