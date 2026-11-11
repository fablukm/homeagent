from collections.abc import Callable
from typing import cast


class Registry[T]:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, type[T]] = {}

    def register[C](self, name: str) -> Callable[[type[C]], type[C]]:
        def decorator(cls: type[C]) -> type[C]:
            if name in self._items:
                raise ValueError(f"{self.kind} {name!r} registered twice")
            self._items[name] = cast(type[T], cls)
            return cls

        return decorator

    def get(self, name: str) -> type[T]:
        try:
            return self._items[name]
        except KeyError:
            known = ", ".join(sorted(self._items)) or "none"
            raise KeyError(f"unknown {self.kind} {name!r}; registered: {known}") from None

    def names(self) -> list[str]:
        return sorted(self._items)
