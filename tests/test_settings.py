from homeagent.config.settings import Settings


def test_every_config_file_loads(settings: Settings) -> None:
    assert settings.app.timezone == "Europe/Zurich"
    assert set(settings.telegram.topics) == {"orders", "facts", "log"}
    assert settings.llm.classes["answer"].allow_cloud
    assert not settings.llm.classes["classify"].allow_cloud


def test_task_sizes_have_hours(settings: Settings) -> None:
    assert set(settings.load.hours_per_size) == set(settings.schema_.task_sizes)
