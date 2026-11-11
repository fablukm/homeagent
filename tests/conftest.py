import shutil
from pathlib import Path

import pytest

from homeagent.config.settings import CONFIG_FILES, Settings, load_settings

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_SUFFIX = ".example.toml"


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    config = ROOT / "config"
    target = tmp_path_factory.mktemp("config")
    for name in CONFIG_FILES:
        example = config / name.replace(".toml", EXAMPLE_SUFFIX)
        shutil.copy(example if example.exists() else config / name, target / name)
    return load_settings(target)
