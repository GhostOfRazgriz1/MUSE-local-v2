"""Permission manager — orchestrates grants, budgets, and approval flow.

Approval modes
--------------
* **always**  – permanent grant, survives across sessions.
* **session** – scoped to the current session; auto-revoked when the session
  ends (or server restarts).
* **once**    – single-use; automatically revoked after the first successful
  permission check.
"""

import functools
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from muse.permissions.repository import PermissionRepository
from muse.permissions.trust_budget import TrustBudgetManager

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

VALID_APPROVAL_MODES = {"always", "session", "once"}

# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class PermissionCheck:
    """Result of checking whether a skill may exercise a permission."""
    allowed: bool
    requires_user_approval: bool
    reason: str


# ------------------------------------------------------------------
# Human-readable labels
# ------------------------------------------------------------------

_PERMISSION_LABELS: dict[str, str] = {
    "calendar:read": "read your calendar",
    "calendar:write": "modify your calendar",
    "contacts:read": "read your contacts",
    "memory:read": "read agent memory",
    "email:draft": "draft emails on your behalf",
    "email:send": "send emails on your behalf",
    "file:write": "write files to disk",
    "file:delete": "delete files from disk",
    "payment:execute": "make payments",
    "message:send": "send messages",
    "account:modify": "modify account settings",
    "skill:install": "install new skills",
}

# ------------------------------------------------------------------
# Risk-tier patterns (evaluated in order — first match wins)
# ------------------------------------------------------------------

_RISK_RULES: list[tuple[str, str]] = [
    # Critical
    (r".*:delete$", "critical"),
    (r".*:modify$", "critical"),
    (r"^skill:install$", "critical"),
    # High
    (r".*:send$", "high"),
    (r".*:execute$", "high"),
    # Medium
    (r".*:write$", "medium"),
    (r".*:draft$", "medium"),
    # Low (catch-all for reads)
    (r".*:read$", "low"),
]

# Default approval mode suggested to the user per risk tier.
_SUGGESTED_MODE: dict[str, str] = {
    "low": "always",
    "medium": "session",
    "high": "once",
    "critical": "once",
}


@functools.lru_cache(maxsize=256)
def _classify_risk(permission: str) -> str:
    """Classify *permission* into a risk tier (cached, deterministic)."""
    for pattern, tier in _RISK_RULES:
        if re.match(pattern, permission):
            return tier
    return "high"


class PermissionManager:
    """High-level permission orchestrator used by the runtime."""

    def __init__(
        self,
        permission_repo: PermissionRepository,
        trust_budget: TrustBudgetManager,
    ) -> None:
        self.permission_repo = permission_repo
        self.trust_budget = trust_budget
        # Pending requests stored in memory (lost on restart — intentional).
        self._pending_requests: dict[str, dict] = {}
        # The active session id — set by the orchestrator so session-scoped
        # grants can be filtered correctly.
        self._current_session_id: str | None = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def set_session(self, session_id: str | None) -> None:
        """Track the active session so session-scoped grants can be checked."""
        self._current_session_id = session_id

    async def end_session(self, session_id: str) -> None:
        """Revoke all session-scoped grants tied to *session_id*."""
        await self.permission_repo.revoke_by_mode_and_session(
            approval_mode="session",
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # Permission checking
    # ------------------------------------------------------------------

    async def check_permission(
        self, skill_id: str, permission: str
    ) -> PermissionCheck:
        """Check whether *skill_id* is allowed to exercise *permission*."""
        grant = await self.permission_repo.find_active_grant(
            skill_id, permission, session_id=self._current_session_id,
        )
        if grant is None:
            risk_tier = await self.get_risk_tier(permission)
            logger.info(
                "Permission denied: skill=%s permission=%s risk=%s (no grant)",
                skill_id, permission, risk_tier,
            )
            return PermissionCheck(
                allowed=False,
                requires_user_approval=True,
                reason=f"No active grant for '{permission}' (risk: {risk_tier})",
            )

        # Grant exists — check budget.
        budget_result = await self.trust_budget.check_budget(permission)
        if not budget_result["allowed"]:
            logger.info(
                "Permission denied: skill=%s permission=%s (budget exhausted)",
                skill_id, permission,
            )
            return PermissionCheck(
                allowed=False,
                requires_user_approval=True,
                reason=budget_result["reason"] or "Budget exhausted",
            )

        # If the grant is "once", consume it now (auto-revoke).
        if grant["approval_mode"] == "once":
            await self.permission_repo.revoke_by_id(grant["id"])

        return PermissionCheck(
            allowed=True,
            requires_user_approval=False,
            reason="Permission granted",
        )

    # ------------------------------------------------------------------
    # Budget consumption
    # ------------------------------------------------------------------

    async def consume_budget(
        self,
        permission: str,
        actions: int = 1,
        tokens: int = 0,
    ) -> bool:
        """Record actual resource consumption against a permission's budget.

        Called AFTER a permission-gated operation completes successfully.
        Returns False if the budget is now exceeded (future operations will
        be blocked by check_budget, but the current one already happened).
        """
        return await self.trust_budget.consume(
            permission=permission, actions=actions, tokens=tokens,
        )

    # ------------------------------------------------------------------
    # Request / approve / deny flow
    # ------------------------------------------------------------------

    async def request_permission(
        self,
        skill_id: str,
        permission: str,
        risk_tier: str,
        context: str,
    ) -> dict:
        """Create a pending permission request for the UI to display."""
        request_id = str(uuid.uuid4())
        label = _PERMISSION_LABELS.get(permission, permission)
        suggested = _SUGGESTED_MODE.get(risk_tier, "once")
        # Build a human-friendly display_text.
        display_text = f"{skill_id} wants to {label}"
        if context:
            display_text += f" to {context}"
        display_text += "."

        request = {
            "request_id": request_id,
            "skill_id": skill_id,
            "permission": permission,
            "risk_tier": risk_tier,
            "context": context,
            "display_text": display_text,
            "suggested_mode": suggested,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._pending_requests[request_id] = request
        return request

    async def approve_request(
        self, request_id: str, approval_mode: str = "once"
    ) -> None:
        """Grant the permission described by *request_id*."""
        request = self._pending_requests.pop(request_id, None)
        if request is None:
            raise ValueError(f"No pending request with id '{request_id}'")

        # Normalise legacy modes coming from older clients.
        mode = self._normalise_mode(approval_mode)

        await self.permission_repo.grant(
            skill_id=request["skill_id"],
            permission=request["permission"],
            risk_tier=request["risk_tier"],
            approval_mode=mode,
            granted_by="user",
            session_id=self._current_session_id if mode == "session" else None,
        )

    async def deny_request(self, request_id: str) -> None:
        """Remove a pending request without granting anything."""
        request = self._pending_requests.pop(request_id, None)
        if request is None:
            raise ValueError(f"No pending request with id '{request_id}'")

    async def get_pending_requests(self) -> list[dict]:
        """Return all outstanding permission requests."""
        return list(self._pending_requests.values())

    # ------------------------------------------------------------------
    # Risk classification
    # ------------------------------------------------------------------

    async def get_risk_tier(self, permission: str) -> str:
        """Classify *permission* into a risk tier (cached)."""
        return _classify_risk(permission)

    async def get_suggested_mode(self, risk_tier: str) -> str:
        """Map a risk tier to its suggested approval mode."""
        return _SUGGESTED_MODE.get(risk_tier, "once")

    # ------------------------------------------------------------------
    # Manifest-driven bulk grant
    # ------------------------------------------------------------------

    async def grant_manifest_permissions(
        self, skill_id: str, permissions: list[str]
    ) -> None:
        """Grant every permission declared in a skill's manifest at install time.

        Manifest grants are always "always" mode so the skill can operate
        without per-action prompts for its declared capabilities.
        """
        for permission in permissions:
            risk_tier = await self.get_risk_tier(permission)
            await self.permission_repo.grant(
                skill_id=skill_id,
                permission=permission,
                risk_tier=risk_tier,
                approval_mode="always",
                granted_by="manifest",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_mode(mode: str) -> str:
        """Accept legacy mode names and map to the canonical three."""
        _LEGACY_MAP = {
            "per_action": "once",
            "permanent": "always",
        }
        canonical = _LEGACY_MAP.get(mode, mode)
        if canonical not in VALID_APPROVAL_MODES:
            return "once"
        return canonical
