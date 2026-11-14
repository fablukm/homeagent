from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from homeagent.core.registry import Registry


class Topic(StrEnum):
    ORDERS = "orders"
    FACTS = "facts"
    LOG = "log"
    APPROVAL = "approval"


@dataclass(frozen=True)
class Button:
    label: str
    op: str
    arg: str = ""


@dataclass(frozen=True)
class Card:
    key: str
    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()


class Sink(ABC):
    name: str

    @abstractmethod
    async def emit(self, topic: Topic, text: str) -> None: ...

    @abstractmethod
    async def ask(self, topic: Topic, card: Card) -> str: ...

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def clip(text: str, max_lines: int) -> str:
    lines = text.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join([*lines[: max_lines - 1], "…"])


SINKS: Registry[Sink] = Registry("sink")
register_sink = SINKS.register
