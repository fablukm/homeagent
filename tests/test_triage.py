from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid7

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from homeagent.config.settings import Need, Settings
from homeagent.core.events import Event, SourceKind
from homeagent.core.triage import MailTriage, ProposalStatus, Triage
from homeagent.db.models import Base, LlmCall, Proposal
from homeagent.db.session import make_engine, make_sessions
from homeagent.llm.router import LlmRouter, NoBackendError
from homeagent.sinks.base import Topic
from homeagent.sinks.stdout import StdoutSink

USER = 42

type MakeRouter = Callable[[MailTriage | None], LlmRouter]


def _mail(body: str = "Bitte bezahlen Sie CHF 84.20 bis 30.11.") -> Event:
    return Event(
        source=SourceKind.MAIL,
        source_name="gmail",
        external_id="m1",
        occurred_at=datetime(2026, 11, 10, 23, 30, tzinfo=UTC),
        title="Rechnung Oktober",
        body=body,
        sender="billing@example.ch",
    )


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncEngine:
    engine = make_engine(tmp_path / "t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


def _triage(
    settings: Settings, engine: AsyncEngine, answer: MailTriage | None, make_router: MakeRouter
) -> tuple[Triage, StdoutSink, async_sessionmaker[AsyncSession]]:
    sink = StdoutSink()
    sessions = make_sessions(engine)
    router = make_router(answer)
    return Triage(settings.triage, sessions, router, sink, settings.app.timezone), sink, sessions


def test_prompt_names_every_need_and_the_mail(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter
) -> None:
    triage, _, _ = _triage(settings, engine, None, make_router)
    long_body = "x" * (settings.triage.body_chars + 50)

    prompt = triage.prompt(_mail(long_body))

    for need, text in settings.triage.needs.items():
        assert f"{need.value}: {text}" in prompt
    assert "billing@example.ch" in prompt
    assert "Rechnung Oktober" in prompt
    assert "2026-11-11" in prompt
    assert "x" * settings.triage.body_chars in prompt
    assert "x" * (settings.triage.body_chars + 1) not in prompt


def test_prompt_version_follows_template_and_needs(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter
) -> None:
    triage, _, _ = _triage(settings, engine, None, make_router)
    same, _, _ = _triage(settings, engine, None, make_router)
    changed_cfg = settings.triage.model_copy(update={"template": settings.triage.template + "!"})
    changed = Triage(changed_cfg, same.sessions, same.router, same.sink, settings.app.timezone)

    assert triage.version == same.version
    assert triage.version != changed.version


async def test_each_need_is_asked_once_and_tracked(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter
) -> None:
    answer = MailTriage(
        needs=[Need.PAY, Need.KEEP, Need.PAY], due_date=date(2026, 11, 30), summary="Power bill"
    )
    triage, sink, sessions = _triage(settings, engine, answer, make_router)
    event = _mail()

    proposals = await triage.run(event)

    assert [p.need for p in proposals] == ["pay", "keep"]
    assert [(topic, key) for topic, _, key in sink.asked] == [
        (Topic.APPROVAL, str(p.id)) for p in proposals
    ]
    first = sink.asked[0][1].splitlines()
    assert first == [
        "pay by 30.11.2026",
        "Power bill",
        "gmail: Rechnung Oktober",
        "from billing@example.ch",
    ]
    async with sessions() as session:
        call = await session.scalar(select(LlmCall))
        stored = (await session.scalars(select(Proposal))).all()
    assert call is not None
    assert call.raw_event_id == event.id
    assert call.prompt_version == triage.version
    assert call.model == "fake-model"
    assert call.output == answer.model_dump(mode="json")
    assert call.valid
    assert {p.llm_call_id for p in stored} == {call.id}
    assert {p.message_ref for p in stored} == {"1", "2"}
    assert {p.status for p in stored} == {ProposalStatus.PENDING}
    assert {p.due for p in stored} == {"2026-11-30"}
    await engine.dispose()


async def test_no_need_asks_nothing_but_keeps_the_call(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter
) -> None:
    newsletter = MailTriage(needs=[], due_date=None, summary="Newsletter")
    triage, sink, sessions = _triage(settings, engine, newsletter, make_router)

    assert await triage.run(_mail()) == []

    assert sink.asked == []
    async with sessions() as session:
        call = await session.scalar(select(LlmCall))
    assert call is not None
    assert call.output == {"needs": [], "due_date": None, "summary": "Newsletter"}
    await engine.dispose()


async def test_failed_backend_is_recorded(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter
) -> None:
    triage, sink, sessions = _triage(settings, engine, None, make_router)

    with pytest.raises(NoBackendError):
        await triage.run(_mail())

    assert sink.asked == []
    async with sessions() as session:
        call = await session.scalar(select(LlmCall))
    assert call is not None
    assert not call.valid
    assert "down" in call.error
    await engine.dispose()


async def test_a_decision_is_recorded_once(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter
) -> None:
    answer = MailTriage(needs=[Need.REPLY], due_date=None, summary="Question from the Kita")
    triage, _, sessions = _triage(settings, engine, answer, make_router)
    [proposal] = await triage.run(_mail())

    assert await triage.decide(str(proposal.id), False, USER)
    assert not await triage.decide(str(proposal.id), True, USER)
    assert not await triage.decide(str(uuid7()), True, USER)
    assert not await triage.decide("not-a-uuid", True, USER)

    async with sessions() as session:
        stored = await session.get_one(Proposal, proposal.id)
    assert stored.status == ProposalStatus.DECLINED
    assert stored.decided_by == USER
    assert stored.decided_at is not None
    await engine.dispose()
