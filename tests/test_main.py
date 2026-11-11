from datetime import UTC, datetime
from pathlib import Path

from homeagent.config.settings import Secrets, Settings
from homeagent.core.events import Event, SourceKind
from homeagent.core.syntax import parse_title
from homeagent.db.models import Base
from homeagent.main import Agent
from homeagent.sinks.base import Topic
from homeagent.sinks.stdout import StdoutSink
from homeagent.sources.base import Source, Upstream
from homeagent.sources.gmail import GmailSource

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
