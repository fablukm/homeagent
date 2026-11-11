from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

import homeagent.actions.telegram_log
from homeagent.actions.base import ActionStatus
from homeagent.config.settings import Settings
from homeagent.core.dispatch import Dispatcher, rule_sha
from homeagent.core.events import Event, SourceKind
from homeagent.core.ingest import Ingestor
from homeagent.core.rules import RuleEngine, TomlRuleStore
from homeagent.core.syntax import parse_title
from homeagent.db.models import ActionRow, Base, RawEvent, RuleRun
from homeagent.db.session import make_engine, make_sessions
from homeagent.sinks.base import Topic
from homeagent.sinks.stdout import StdoutSink
from homeagent.sources.base import Source, Upstream

ROOT = Path(__file__).resolve().parent.parent
_ = homeagent.actions.telegram_log


class FakeCalendar(Source):
    name = "private"

    def __init__(self, raws: list[Upstream]) -> None:
        self.raws = raws

    async def poll(self, cursor: str | None) -> tuple[list[Upstream], str | None]:
        return self.raws, None

    def normalise(self, raw: Upstream) -> Event | None:
        parsed = parse_title(str(raw["summary"]))
        return Event(
            source=SourceKind.CALENDAR,
            source_name=self.name,
            external_id=str(raw["id"]),
            occurred_at=datetime(2026, 11, 20, 9, tzinfo=UTC),
            title=parsed.text,
            creator=str(raw["creator"]),
            calendar=self.name,
            tokens=parsed.tokens,
        )


async def _setup(tmp_path: Path, settings: Settings) -> tuple[Ingestor, Dispatcher, StdoutSink]:
    engine = make_engine(tmp_path / "t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = make_sessions(engine)
    sink = StdoutSink()
    rules = RuleEngine(TomlRuleStore(ROOT / "rules.example", settings.schema_))
    return Ingestor(sessions), Dispatcher(sessions, rules, sink), sink


async def test_matched_rule_writes_runs_and_actions_and_logs(
    tmp_path: Path, settings: Settings
) -> None:
    partner = settings.sources.partner_emails[0]
    source = FakeCalendar([{"id": "evt-1", "summary": "! Kinderarzt #kids", "creator": partner}])
    ingestor, dispatcher, sink = await _setup(tmp_path, settings)
    [event] = (await ingestor.run(source)).events

    report = await dispatcher.run(event, "work")

    rules = dispatcher.rules.store.reload()
    assert report.rule_runs == len(rules)
    assert sink.lines == [(Topic.LOG, "paediatrician: blocked 20.11., childcare task set")]
    async with ingestor.sessions() as session:
        raw = await session.get(RawEvent, event.id)
        assert raw is not None
        runs = (await session.execute(select(RuleRun))).scalars().all()
        assert {(r.rule_id, r.matched) for r in runs} == {
            (rule.id, rule.id == "paediatrician") for rule in rules
        }
        [run] = [r for r in runs if r.matched]
        assert run.raw_event_id == event.id
        assert run.rule_sha == rule_sha(next(r for r in rules if r.id == "paediatrician"))
        actions = (await session.execute(select(ActionRow).order_by(ActionRow.type))).scalars()
        rows = {a.type: a for a in actions}
    assert set(rows) == {"calendar.block", "task.create", "telegram.log"}
    assert all(a.rule_run_id == run.id for a in rows.values())
    assert rows["telegram.log"].status == ActionStatus.TAKEN
    assert rows["telegram.log"].executed_at is not None
    assert rows["calendar.block"].status == ActionStatus.FAILED
    assert "calendar.block" in rows["calendar.block"].error
    assert rows["task.create"].params == {
        "list": "home",
        "title": "Book childcare for {event.occurred_at:%d.%m.}",
        "due_offset_days": -10,
    }


async def test_replayed_event_reruns_rules_but_fires_no_action_twice(
    tmp_path: Path, settings: Settings
) -> None:
    partner = settings.sources.partner_emails[0]
    source = FakeCalendar([{"id": "evt-1", "summary": "! Kinderarzt #kids", "creator": partner}])
    ingestor, dispatcher, sink = await _setup(tmp_path, settings)
    [event] = (await ingestor.run(source)).events
    await dispatcher.run(event, "work")

    replay = await dispatcher.run(event, "work")

    assert replay.results == []
    assert replay.already_fired == len(["calendar.block", "task.create", "telegram.log"])
    assert len(sink.lines) == 1
    async with ingestor.sessions() as session:
        runs = (await session.execute(select(RuleRun))).scalars().all()
        actions = (await session.execute(select(ActionRow))).scalars().all()
    assert len(runs) == 2 * len(dispatcher.rules.store.reload())
    assert len(actions) == len(["calendar.block", "task.create", "telegram.log"])


async def test_unmatched_event_writes_runs_only(tmp_path: Path, settings: Settings) -> None:
    source = FakeCalendar([{"id": "evt-2", "summary": "Lunch", "creator": "me"}])
    ingestor, dispatcher, sink = await _setup(tmp_path, settings)
    [event] = (await ingestor.run(source)).events

    report = await dispatcher.run(event, "work")

    assert report.results == []
    assert sink.lines == []
    async with ingestor.sessions() as session:
        runs = (await session.execute(select(RuleRun))).scalars().all()
        actions = (await session.execute(select(ActionRow))).scalars().all()
    assert runs and not any(r.matched for r in runs)
    assert actions == []
