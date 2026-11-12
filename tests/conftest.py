import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from homeagent.config.settings import CONFIG_FILES, Secrets, Settings, load_settings
from homeagent.core.triage import MailTriage
from homeagent.llm.base import Completion, Usage
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
    def __init__(self, settings: Settings, answer: MailTriage | None) -> None:
        super().__init__(settings.llm.model_copy(update={"backends": {}}), Secrets())
        self.answer = answer
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def complete[T: BaseModel](
        self, task_class: str, prompt: str, schema: type[T], *, system: str
    ) -> Completion[T]:
        self.prompts.append(prompt)
        self.systems.append(system)
        if self.answer is None:
            raise NoBackendError(task_class) from ConnectionError("down")
        value = schema.model_validate(self.answer.model_dump())
        return Completion(value, Usage(prompt_tokens=7, latency_ms=3), "fake-model", "fake")


type MakeRouter = Callable[[MailTriage | None], FakeRouter]


@pytest.fixture
def make_router(settings: Settings) -> MakeRouter:
    return lambda answer: FakeRouter(settings, answer)
