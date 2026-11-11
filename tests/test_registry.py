import pytest

from homeagent.core.registry import Registry


class Thing:
    pass


def test_register_and_get() -> None:
    reg: Registry[Thing] = Registry("thing")

    @reg.register("a")
    class A(Thing):
        pass

    assert reg.get("a") is A
    assert reg.names() == ["a"]


def test_duplicate_and_unknown_fail_loudly() -> None:
    reg: Registry[Thing] = Registry("thing")
    reg.register("a")(Thing)
    with pytest.raises(ValueError, match="twice"):
        reg.register("a")(Thing)
    with pytest.raises(KeyError, match="registered: a"):
        reg.get("b")
