from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from homeagent.config.settings import LlmBackendCfg
from homeagent.core.registry import Registry


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0
    load_ms: int = 0
    eval_ms: int = 0


@dataclass(frozen=True)
class Completion[T: BaseModel]:
    value: T
    usage: Usage
    model: str
    backend: str


class LlmBackend(ABC):
    name: str
    model: str
    cloud: bool

    @abstractmethod
    def __init__(self, name: str, cfg: LlmBackendCfg, api_key: str) -> None: ...

    @abstractmethod
    async def complete[T: BaseModel](
        self, prompt: str, schema: type[T], *, system: str, temperature: float, task_class: str
    ) -> Completion[T]: ...

    @abstractmethod
    async def healthy(self) -> bool: ...


QuestionType = Literal["choice", "score", "noul"]


@dataclass(frozen=True)
class Question:
    id: str
    type: QuestionType
    instructions: str
    criteria: dict[str, str] | list[str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class Answer:
    question_id: str
    value: str | int | bool
    confidence: float
    probabilities: dict[str, float]


@dataclass(frozen=True)
class Decision:
    answers: list[Answer]
    usage: Usage
    model: str
    backend: str


class DecisionBackend(ABC):
    name: str
    model: str

    @abstractmethod
    def __init__(self, name: str, cfg: LlmBackendCfg) -> None: ...

    @abstractmethod
    async def decide(self, state: str, questions: list[Question]) -> Decision: ...

    @abstractmethod
    async def healthy(self) -> bool: ...


LLM_BACKENDS: Registry[LlmBackend] = Registry("llm_backend")
DECISION_BACKENDS: Registry[DecisionBackend] = Registry("decision_backend")
register_llm_backend = LLM_BACKENDS.register
register_decision_backend = DECISION_BACKENDS.register
