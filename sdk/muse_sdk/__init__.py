"""MUSE Skill SDK — capability-gated abstractions for skill development."""

from muse_sdk.context import SkillContext, SkillResult
from muse_sdk.errors import (
    PermissionDenied,
    UserCancelled,
    ExternalServiceError,
    SkillError,
)

__all__ = [
    "SkillContext",
    "SkillResult",
    "PermissionDenied",
    "UserCancelled",
    "ExternalServiceError",
    "SkillError",
]
