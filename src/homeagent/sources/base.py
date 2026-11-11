from abc import ABC, abstractmethod
from typing import Any

from homeagent.config.settings import EmailSourceCfg
from homeagent.core.events import Event
from homeagent.core.registry import Registry

Upstream = dict[str, Any]


class Source(ABC):
    name: str

    @abstractmethod
    async def poll(self, cursor: str | None) -> tuple[list[Upstream], str | None]: ...

    @abstractmethod
    def normalise(self, raw: Upstream) -> Event | None: ...

    def external_id(self, raw: Upstream) -> str:
        return str(raw["id"])


class EmailSource(Source):
    def __init__(self, cfg: EmailSourceCfg) -> None:
        self.name = cfg.name
        self.cfg = cfg


SOURCES: Registry[Source] = Registry("source")
register_source = SOURCES.register
