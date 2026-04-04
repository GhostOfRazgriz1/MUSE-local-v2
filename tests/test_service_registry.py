"""Unit tests for ServiceRegistry."""

import pytest
from muse.kernel.service_registry import ServiceRegistry, ServiceNotFound


def test_register_and_get():
    reg = ServiceRegistry()
    obj = {"key": "value"}
    reg.register("test", obj)
    assert reg.get("test") is obj


def test_get_missing_raises():
    reg = ServiceRegistry()
    with pytest.raises(ServiceNotFound):
        reg.get("nonexistent")


def test_get_typed():
    reg = ServiceRegistry()
    reg.register("num", 42)
    assert reg.get_typed("num", int) == 42


def test_get_typed_wrong_type():
    reg = ServiceRegistry()
    reg.register("num", 42)
    with pytest.raises(TypeError):
        reg.get_typed("num", str)


def test_overwrite():
    reg = ServiceRegistry()
    reg.register("x", "first")
    reg.register("x", "second")
    assert reg.get("x") == "second"


def test_has():
    reg = ServiceRegistry()
    assert not reg.has("x")
    reg.register("x", 1)
    assert reg.has("x")


def test_contains():
    reg = ServiceRegistry()
    reg.register("a", 1)
    assert "a" in reg
    assert "b" not in reg


def test_names():
    reg = ServiceRegistry()
    reg.register("b", 2)
    reg.register("a", 1)
    assert reg.names == ["a", "b"]


def test_repr():
    reg = ServiceRegistry()
    reg.register("db", None)
    reg.register("provider", None)
    assert "db" in repr(reg)
    assert "provider" in repr(reg)
