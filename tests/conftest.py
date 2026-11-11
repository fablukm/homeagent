import os
import shutil
from pathlib import Path

import pytest

from homeagent.config.settings import CONFIG_FILES, Secrets, Settings, load_settings

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
