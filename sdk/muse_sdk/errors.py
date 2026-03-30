"""SDK exception types for MUSE skills."""


class SkillError(Exception):
    """Generic skill-level error for custom failure cases."""


class PermissionDenied(SkillError):
    """Raised when a capability check fails."""

    def __init__(self, permission: str, message: str = ""):
        self.permission = permission
        super().__init__(message or f"Permission denied: {permission}")


class UserCancelled(SkillError):
    """Raised when the user declines a confirmation or cancels a task."""


class ExternalServiceError(SkillError):
    """Raised when an HTTP request to an external API fails after retries."""

    def __init__(self, url: str, status_code: int = 0, message: str = ""):
        self.url = url
        self.status_code = status_code
        super().__init__(message or f"External service error: {url} (status {status_code})")
