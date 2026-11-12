from homeagent.config.settings import Need, Settings


def test_every_config_file_loads(settings: Settings) -> None:
    assert settings.app.timezone == "Europe/Zurich"
    assert set(settings.telegram.topics) == {"orders", "facts", "log", "approval"}
    assert settings.llm.classes["answer"].allow_cloud
    assert not settings.llm.classes["classify"].allow_cloud


def test_triage_describes_every_need(settings: Settings) -> None:
    assert set(settings.triage.needs) == set(Need)
    assert sorted(settings.triage.priority) == sorted(Need)
    assert settings.triage.task_class in settings.llm.classes
    assert settings.preferences.mail


def test_task_sizes_have_hours(settings: Settings) -> None:
    assert set(settings.load.hours_per_size) == set(settings.schema_.task_sizes)
