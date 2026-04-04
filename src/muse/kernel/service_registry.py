"""Service registry — typed dependency injection container.

Replaces the pattern where modules hold ``self._orch`` and reach into
private attributes.  Services register by name; consumers look them up.

Names match the existing ``_``-prefixed attributes on the orchestrator
for easy migration (e.g. ``"db"``, ``"memory_repo"``, ``"provider"``).
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ServiceNotFound(KeyError):
    """Raised when a requested service is not registered."""


class ServiceRegistry:
    """Lightweight DI container for MUSE kernel services."""

    __slots__ = ("_services",)

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}

    def register(self, name: str, service: Any) -> None:
        """Register a service. Overwrites if already registered."""
        self._services[name] = service
        logger.debug("Service registered: %s (%s)", name, type(service).__name__)

    def get(self, name: str) -> Any:
        """Look up a service by name. Raises ``ServiceNotFound`` if missing."""
        try:
            return self._services[name]
        except KeyError:
            raise ServiceNotFound(
                f"Service '{name}' not registered. "
                f"Available: {', '.join(sorted(self._services))}"
            ) from None

    def get_typed(self, name: str, expected_type: type[T]) -> T:
        """Look up a service and verify its type."""
        service = self.get(name)
        if not isinstance(service, expected_type):
            raise TypeError(
                f"Service '{name}' is {type(service).__name__}, "
                f"expected {expected_type.__name__}"
            )
        return service

    def has(self, name: str) -> bool:
        """Check if a service is registered."""
        return name in self._services

    @property
    def names(self) -> list[str]:
        """All registered service names."""
        return sorted(self._services)

    def __contains__(self, name: str) -> bool:
        return name in self._services

    def __repr__(self) -> str:
        return f"ServiceRegistry({', '.join(sorted(self._services))})"
