from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from homeagent.config.settings import Need, Secrets, Settings
from homeagent.core.events import Event, SourceKind
from homeagent.core.syntax import parse_title
from homeagent.core.triage import MailTriage
from homeagent.db.models import Base
from homeagent.llm.router import LlmRouter
from homeagent.main import Agent
from homeagent.sinks.base import Topic
from homeagent.sinks.stdout import StdoutSink
from homeagent.sources.base import Source, Upstream
from homeagent.sources.gmail import GmailSource

type MakeRouter = Callable[[MailTriage | None], LlmRouter]

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


def _agent(settings: Settings, tmp_path: Path, **secrets: str) -> Agent:
    app = settings.app.model_copy(
        update={"data_dir": tmp_path, "rules_dir": ROOT / "rules.example"}
    )
    local = settings.model_copy(update={"app": app})
    return Agent(local, Secrets.model_validate({"telegram_token": "t", **secrets}))


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

    async def poll(self, cursor: str | None) -> tuple[list[Upstream], str | None]:
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
        )


async def test_poll_triages_new_mail(
    settings: Settings, tmp_path: Path, make_router: MakeRouter
) -> None:
    agent = _agent(settings, tmp_path)
    async with agent.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sink = StdoutSink()
    answer = MailTriage(need=Need.PAY, due_date=None, summary="A bill")
    agent.triage.router = make_router(answer)
    agent.triage.sink = sink

    await agent.poll(OneMail())
    await agent.poll(OneMail())

    [(topic, text, key)] = sink.asked
    assert topic is Topic.APPROVAL
    assert text.startswith("pay\nA bill")
    assert await agent.decide(key, True, 1)
    await agent.engine.dispose()


async def test_poll_survives_a_failed_triage(
    settings: Settings, tmp_path: Path, make_router: MakeRouter
) -> None:
    agent = _agent(settings, tmp_path)
    async with agent.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sink = StdoutSink()
    agent.triage.router = make_router(None)
    agent.triage.sink = sink

    await agent.poll(OneMail())

    assert sink.asked == []
    await agent.engine.dispose()
