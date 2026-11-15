import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from conftest import MakeDecider, TasksApi, tasks_for
from pydantic import BaseModel

from homeagent.config.settings import Secrets, Settings
from homeagent.core.events import Event, SourceKind
from homeagent.core.mailflow import TaskText
from homeagent.core.syntax import parse_title
from homeagent.db.models import Base
from homeagent.llm.laya import LayaBackend
from homeagent.llm.router import LlmRouter
from homeagent.main import Agent, configure_logging
from homeagent.sinks.base import Topic
from homeagent.sinks.stdout import StdoutSink
from homeagent.sources.base import Source, Upstream
from homeagent.sources.gmail import GmailSource

type MakeRouter = Callable[[list[BaseModel] | None], LlmRouter]

ROOT = Path(__file__).resolve().parent.parent


class OneEvent(Source):
    name = "private"

    def __init__(self, creator: str) -> None:
        self.creator = creator

    async def poll(self, cursor: str | None) -> tuple[list[Upstream], str | None]:
        return [{"id": "evt-1", "summary": "! Kinderarzt #kids"}], "1"

    def normalise(self, raw: Upstream) -> Event | None:
        parsed = parse_title(str(raw["summary"]))
        return Event(
            source=SourceKind.CALENDAR,
            source_name=self.name,
            external_id=str(raw["id"]),
            occurred_at=datetime(2026, 11, 20, 9, tzinfo=UTC),
            title=parsed.text,
            creator=self.creator,
            calendar=self.name,
            tokens=parsed.tokens,
        )


def _agent(
    settings: Settings, tmp_path: Path, api: TasksApi | None = None, **secrets: str
) -> Agent:
    app = settings.app.model_copy(
        update={"data_dir": tmp_path, "rules_dir": ROOT / "rules.example"}
    )
    local = settings.model_copy(update={"app": app})
    agent = Agent(local, Secrets.model_validate({"telegram_token": "123:test-token", **secrets}))
    agent.triage.tasks.client = tasks_for(settings, api or TasksApi(settings)).client
    return agent


def test_gmail_is_polled_only_with_a_google_login(settings: Settings, tmp_path: Path) -> None:
    without = _agent(settings, tmp_path)
    with_login = _agent(settings, tmp_path, google_refresh_token="r")

    assert without.sources == []
    [(source, interval)] = with_login.sources
    assert isinstance(source, GmailSource)
    assert interval == next(c.interval_min for c in settings.sources.email if c.name == "gmail")


async def test_poll_stores_events_then_runs_rules_once(settings: Settings, tmp_path: Path) -> None:
    agent = _agent(settings, tmp_path)
    async with agent.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sink = StdoutSink()
    agent.dispatcher.sink = sink
    source = OneEvent(settings.sources.partner_emails[0])

    await agent.poll(source)
    await agent.poll(source)

    assert sink.lines == [(Topic.LOG, "paediatrician: blocked 20.11., childcare task set")]
    await agent.engine.dispose()


class OneMail(Source):
    name = "gmail"

    def __init__(self) -> None:
        self.polls = 0

    async def poll(self, cursor: str | None) -> tuple[list[Upstream], str | None]:
        self.polls += 1
        return [{"id": "mail-1"}], "1"

    def normalise(self, raw: Upstream) -> Event | None:
        return Event(
            source=SourceKind.MAIL,
            source_name=self.name,
            external_id=str(raw["id"]),
            occurred_at=datetime(2026, 11, 10, 9, tzinfo=UTC),
            title="Rechnung",
            body="CHF 10",
            sender="billing@example.ch",
            payload={"from": "Billing <billing@example.ch>"},
        )


class BrokenSource(OneMail):
    name = "broken"

    async def poll(self, cursor: str | None) -> tuple[list[Upstream], str | None]:
        raise ConnectionError("gmail down")


async def test_poll_triages_new_mail(
    settings: Settings, tmp_path: Path, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    api = TasksApi(settings)
    agent = _agent(settings, tmp_path, api)
    async with agent.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sink = StdoutSink()
    agent.triage.flow.router = make_router([TaskText(task="Pay the bill")])
    agent.triage.flow.decider = make_decider("pay", 1.0)
    agent.triage.sink = sink

    await agent.poll(OneMail())
    await agent.poll(OneMail())

    [(topic, card)] = sink.asked
    assert topic is Topic.APPROVAL
    head, task = card.text.splitlines()[:2]
    assert (head, task) == ("pay by 15.11.2026, deadline 20.11.2026", "Pay the bill")
    decided = await agent.tap(card.key, "ok", "", 1)
    assert decided is not None
    assert decided.text.splitlines()[-1] == "accepted, task in Home Agent"
    [(list_id, task)] = api.created
    assert list_id == "L0"
    assert (task["title"], task["due"]) == ("Pay the bill", "2026-11-15T00:00:00.000Z")
    assert task["notes"].endswith("#all/mail-1")
    assert await agent.reply("unknown", "text", 1) is None
    await agent.engine.dispose()


async def test_poll_survives_a_failed_triage(
    settings: Settings, tmp_path: Path, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    agent = _agent(settings, tmp_path)
    async with agent.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sink = StdoutSink()
    agent.triage.flow.router = make_router(None)
    agent.triage.flow.decider = make_decider(None, 1.0)
    agent.triage.sink = sink
    agent.sink = sink

    await agent.poll(OneMail())

    assert sink.asked == []
    [(topic, text)] = sink.lines
    assert topic is Topic.LOG
    assert text.splitlines()[0] == "error in gmail mail-1"
    assert text.splitlines()[1].startswith("NoBackendError")
    await agent.engine.dispose()


async def test_stop_and_start_pause_mail_and_survive_a_restart(
    settings: Settings, tmp_path: Path, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    agent = _agent(settings, tmp_path)
    async with agent.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sink = StdoutSink()
    agent.triage.flow.router = make_router([TaskText(task="Pay the bill")])
    agent.triage.flow.decider = make_decider("pay", 1.0)
    agent.triage.sink = sink
    source = OneMail()

    assert await agent.command("stop") == "stopped: mail is not read until start"
    await agent.poll(source)
    status = await agent.command("status")
    assert status is not None
    assert status.splitlines()[1] == "mail stopped"
    assert source.polls == 0
    assert await agent.command("hello") is None

    restarted = _agent(settings, tmp_path)
    await restarted.load_paused()
    assert restarted.paused
    await restarted.engine.dispose()

    assert await agent.command("start") == "started: mail is read again"
    await agent.poll(source)
    assert source.polls == 1
    assert len(sink.asked) == 1
    again = _agent(settings, tmp_path)
    await again.load_paused()
    assert not again.paused
    await again.engine.dispose()
    await agent.engine.dispose()


async def test_errors_are_reported_once_per_place_without_secrets(
    settings: Settings, tmp_path: Path
) -> None:
    agent = _agent(settings, tmp_path, google_refresh_token="refresh-secret")
    sink = StdoutSink()
    agent.sink = sink

    await agent.report("poll gmail", RuntimeError("token refresh-secret rejected"))
    await agent.report("poll gmail", RuntimeError("token refresh-secret rejected"))
    await agent.report("poll gmail", RuntimeError("timeout"))
    await agent.report("gmail m1", RuntimeError("timeout"))

    assert [text for _, text in sink.lines] == [
        "error in poll gmail\nRuntimeError: token <secret> rejected",
        "error in poll gmail\nRuntimeError: timeout",
        "error in gmail m1\nRuntimeError: timeout",
    ]
    agent.reported["gmail m1"] = ("error in gmail m1\nRuntimeError: timeout", 0.0)
    await agent.report("gmail m1", RuntimeError("timeout"))
    assert len(sink.lines) == 4
    await agent.engine.dispose()


async def test_a_failing_poll_is_reported(settings: Settings, tmp_path: Path) -> None:
    agent = _agent(settings, tmp_path)
    async with agent.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sink = StdoutSink()
    agent.sink = sink

    await agent.poll(BrokenSource())

    [(topic, text)] = sink.lines
    assert topic is Topic.LOG
    assert text == "error in poll broken\nConnectionError: gmail down"
    await agent.engine.dispose()


def test_logging_keeps_noisy_libraries_quiet(settings: Settings) -> None:
    configure_logging(settings.app)

    for name in settings.app.quiet_loggers:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    assert logging.getLogger().getEffectiveLevel() == logging.INFO
    assert "httpx" in settings.app.quiet_loggers


def test_triage_decides_with_the_configured_laya(settings: Settings, tmp_path: Path) -> None:
    agent = _agent(settings, tmp_path)
    laya = settings.llm.backends[settings.triage.laya.backend]

    assert isinstance(agent.triage.flow.decider, LayaBackend)
    assert agent.triage.flow.decider.model == laya.model
