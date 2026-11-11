from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from homeagent.core.events import Event
from homeagent.core.registry import Registry
from homeagent.core.rules import ActionSpec, Rule
from homeagent.sinks.base import Sink


class ActionStatus(StrEnum):
    PENDING = "pending"
    TAKEN = "taken"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True)
class ActionContext:
    event: Event
    rule: Rule
    spec: ActionSpec
    mode: str
    sink: Sink

    @property
    def idempotency_key(self) -> str:
        raw = f"{self.rule.id}|{self.event.source}|{self.event.external_id}|{self.spec.type}"
        return sha256(raw.encode()).hexdigest()[:32]

    def render(self, template: str) -> str:
        return template.format(event=self.event, rule=self.rule)


@dataclass(frozen=True)
class ActionResult:
    status: ActionStatus
    external_ref: str = ""
    detail: str = ""


class Action(ABC):
    type: str

    @abstractmethod
    async def run(self, ctx: ActionContext) -> ActionResult: ...


ACTIONS: Registry[Action] = Registry("action")
register_action = ACTIONS.register
