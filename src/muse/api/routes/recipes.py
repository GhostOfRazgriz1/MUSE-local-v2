"""Proactive recipes REST endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from muse.api.app import get_service, require_orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recipes", tags=["recipes"])


@router.get("")
async def list_recipes(orchestrator=Depends(require_orchestrator)):
    """List all registered proactive recipes with their current state."""
    engine = get_service("recipe_engine")
    recipes = engine.get_recipes()

    return {
        "recipes": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "enabled": r.enabled,
                "builtin": r.builtin,
                "user_toggleable": r.user_toggleable,
                "min_relationship": r.min_relationship,
                "cooldown": r.cooldown,
                "trigger_type": r.trigger.type.value,
                "trigger_params": r.trigger.params,
            }
            for r in recipes
        ]
    }


@router.get("/{recipe_id}")
async def get_recipe(recipe_id: str, orchestrator=Depends(require_orchestrator)):
    """Get details of a specific recipe."""
    engine = get_service("recipe_engine")
    recipe = engine.get_recipe(recipe_id)
    if not recipe:
        raise HTTPException(404, f"Recipe '{recipe_id}' not found")

    return {
        "id": recipe.id,
        "name": recipe.name,
        "description": recipe.description,
        "enabled": recipe.enabled,
        "builtin": recipe.builtin,
        "user_toggleable": recipe.user_toggleable,
        "min_relationship": recipe.min_relationship,
        "cooldown": recipe.cooldown,
        "trigger_type": recipe.trigger.type.value,
        "trigger_params": recipe.trigger.params,
        "conditions": [
            {"type": c.type.value, "params": c.params}
            for c in recipe.conditions
        ],
        "actions": [
            {"type": a.type.value, "params": a.params}
            for a in recipe.actions
        ],
    }


@router.put("/{recipe_id}/toggle")
async def toggle_recipe(recipe_id: str, body: dict, orchestrator=Depends(require_orchestrator)):
    """Enable or disable a recipe.

    Body: ``{"enabled": true}``
    """
    enabled = body.get("enabled")
    if enabled is None:
        raise HTTPException(400, "enabled field is required")

    engine = get_service("recipe_engine")
    success = await engine.set_enabled(recipe_id, bool(enabled))
    if not success:
        recipe = engine.get_recipe(recipe_id)
        if not recipe:
            raise HTTPException(404, f"Recipe '{recipe_id}' not found")
        raise HTTPException(400, f"Recipe '{recipe_id}' cannot be toggled")

    return {"id": recipe_id, "enabled": bool(enabled)}
