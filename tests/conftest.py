import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from homeagent.config.settings import CONFIG_FILES, LlmBackendCfg, Secrets, Settings, load_settings
from homeagent.llm.base import Answer, Completion, Decision, DecisionBackend, Question, Usage
from homeagent.llm.router import LlmRouter, NoBackendError

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_SUFFIX = ".example.toml"
ENV_PREFIX = "HOMEAGENT_"


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Secrets.model_config, "env_file", None)
    for name in [n for n in os.environ if n.startswith(ENV_PREFIX)]:
        monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    config = ROOT / "config"
    target = tmp_path_factory.mktemp("config")
    for name in CONFIG_FILES:
        example = config / name.replace(".toml", EXAMPLE_SUFFIX)
        shutil.copy(example if example.exists() else config / name, target / name)
    return load_settings(target)


class FakeRouter(LlmRouter):
    def __init__(self, settings: Settings, answers: list[BaseModel] | None) -> None:
        super().__init__(settings.llm.model_copy(update={"backends": {}}), Secrets())
        self.answers = answers
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.schemas: list[str] = []

    async def complete[T: BaseModel](
        self, task_class: str, prompt: str, schema: type[T], *, system: str
    ) -> Completion[T]:
        self.prompts.append(prompt)
        self.systems.append(system)
        self.schemas.append(schema.__name__)
        if self.answers is None:
            raise NoBackendError(task_class) from ConnectionError("down")
        answer = next(a for a in self.answers if isinstance(a, schema))
        value = schema.model_validate(answer.model_dump())
        return Completion(value, Usage(prompt_tokens=7, latency_ms=3), "fake-model", "fake")


type MakeRouter = Callable[[list[BaseModel] | None], FakeRouter]


@pytest.fixture
def make_router(settings: Settings) -> MakeRouter:
    return lambda answer: FakeRouter(settings, answer)


class FakeDecider(DecisionBackend):
    def __init__(self, name: str, cfg: LlmBackendCfg) -> None:
        self.name = name
        self.model = cfg.model
        self.choice: str | None = "none"
        self.confidence = 1.0
        self.probabilities: dict[str, float] = {}
        self.states: list[dict[str, str]] = []
        self.questions: list[Question] = []

    async def decide(self, state: dict[str, str], questions: list[Question]) -> Decision:
        self.states.append(state)
        self.questions.extend(questions)
        if self.choice is None:
            raise NoBackendError(self.name) from ConnectionError("laya down")
        probabilities = self.probabilities or {self.choice: self.confidence}
        answer = Answer(questions[0].id, self.choice, self.confidence, probabilities)
        return Decision([answer], Usage(prompt_tokens=5, latency_ms=2), self.model, self.name)

    async def healthy(self) -> bool:
        return True


type MakeDecider = Callable[[str | None, float], FakeDecider]


@pytest.fixture
def make_decider(settings: Settings) -> MakeDecider:
    def make(choice: str | None, confidence: float) -> FakeDecider:
        name = settings.triage.laya.backend
        decider = FakeDecider(name, settings.llm.backends[name])
        decider.choice = choice
        decider.confidence = confidence
        return decider

    return make
