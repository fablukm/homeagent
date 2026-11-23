import json
import os
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from homeagent.actions.google_tasks import GoogleTasks
from homeagent.config.settings import CONFIG_FILES, LlmBackendCfg, Secrets, Settings, load_settings
from homeagent.core.google_guard import google_client
from homeagent.db.models import Base
from homeagent.db.session import make_engine
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


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(tmp_path / "t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


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


class TasksApi:
    def __init__(self, settings: Settings, titles: list[str] | None = None) -> None:
        self.token_url = settings.sources.google.token_url
        names = [settings.triage.tasks.list_title] if titles is None else titles
        self.lists = [{"id": f"L{i}", "title": title} for i, title in enumerate(names)]
        self.created: list[tuple[str, dict[str, str]]] = []
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url.copy_with(query=None)) == self.token_url:
            return httpx.Response(200, json={"access_token": "access-1"})
        if request.method == "GET":
            return httpx.Response(200, json={"items": self.lists})
        list_id = request.url.path.split("/")[-2]
        self.created.append((list_id, json.loads(request.content)))
        return httpx.Response(200, json={"id": f"T{len(self.created)}"})


def tasks_for(settings: Settings, api: TasksApi) -> GoogleTasks:
    google = settings.sources.google
    secrets = Secrets.model_validate(
        {"google_client_id": "c", "google_client_secret": "s", "google_refresh_token": "r"}
    )
    tasks = GoogleTasks(google, settings.triage.tasks.list_title, secrets)
    tasks.client = google_client(
        google.token_url, google.tasks_url, google.timeout_s, httpx.MockTransport(api.handle)
    )
    return tasks
