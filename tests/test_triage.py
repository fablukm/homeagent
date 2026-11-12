import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid7

import pytest
from conftest import FakeDecider, FakeRouter, MakeDecider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from homeagent.config.settings import Need, Settings, SkipRule
from homeagent.core.events import Event, SourceKind
from homeagent.core.triage import MailSummary, ProposalStatus, Triage
from homeagent.db.models import Base, LlmCall, Proposal
from homeagent.db.session import make_engine, make_sessions
from homeagent.llm.router import LlmRouter, NoBackendError
from homeagent.sinks.base import Topic
from homeagent.sinks.stdout import StdoutSink

USER = 42
FROM = "Billing Team <billing@example.ch>"
SURE = 0.97
UNSURE = 0.6

type MakeRouter = Callable[[MailSummary | None], LlmRouter]


def _mail(
    body: str = "Bitte bezahlen Sie CHF 84.20 bis 30.11.", title: str = "Rechnung Oktober"
) -> Event:
    return Event(
        source=SourceKind.MAIL,
        source_name="gmail",
        external_id="m1",
        occurred_at=datetime(2026, 11, 10, 23, 30, tzinfo=UTC),
        title=title,
        body=body,
        sender="billing@example.ch",
        payload={"from": FROM, "attachments": ["Rechnung.pdf", "QR.png"]},
    )


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncEngine:
    engine = make_engine(tmp_path / "t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


def _triage(
    settings: Settings,
    engine: AsyncEngine,
    answer: MailSummary | None,
    make_router: MakeRouter,
    decider: FakeDecider,
) -> tuple[Triage, StdoutSink, async_sessionmaker[AsyncSession]]:
    sink = StdoutSink()
    sessions = make_sessions(engine)
    router = make_router(answer)
    triage = Triage(
        settings.triage,
        settings.preferences,
        sessions,
        router,
        decider,
        sink,
        settings.app.timezone,
    )
    return triage, sink, sessions


def test_prompt_names_every_need_the_action_and_the_mail(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    triage, _, _ = _triage(settings, engine, None, make_router, make_decider("pay", SURE))
    long_body = "x" * (settings.triage.body_chars + 50)

    prompt = triage.prompt(_mail(long_body), "pay")

    for need, text in settings.triage.needs.items():
        assert f"{need.value}: {text}" in triage.system
    assert "pay, reply, appointment, todo, keep" in triage.system
    for preference in settings.preferences.mail:
        assert f"- {preference}" in triage.system
    assert prompt.splitlines()[0] == "Action: pay"
    assert "billing@example.ch" in prompt
    assert "Rechnung Oktober" in prompt
    assert "2026-11-11" in prompt
    assert "x" * settings.triage.body_chars in prompt
    assert "x" * (settings.triage.body_chars + 1) not in prompt
    assert "Rechnung.pdf, QR.png" in prompt
    assert "billing@example.ch" not in triage.system


async def test_laya_reads_the_mail_as_it_was_trained(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    decider = make_decider("none", SURE)
    triage, _, _ = _triage(settings, engine, None, make_router, decider)
    long_body = "y" * (settings.triage.body_chars + 50)

    await triage.run(_mail(long_body))

    laya = settings.triage.laya
    assert decider.states == [
        {
            "from": FROM,
            "subject": "Rechnung Oktober",
            "attachments": "Rechnung.pdf, QR.png",
            "body": "y" * settings.triage.body_chars,
        }
    ]
    [question] = decider.questions
    assert question.id == laya.question
    assert question.type == "choice"
    assert question.instructions == laya.instructions
    assert question.criteria == laya.criteria
    assert list(laya.criteria) == ["none", *(need.value for need in settings.triage.priority)]
    await engine.dispose()


async def test_the_instructions_go_to_the_model_as_system_message(
    settings: Settings, engine: AsyncEngine, make_decider: MakeDecider
) -> None:
    router = FakeRouter(settings, MailSummary(summary="Bill", due_date=None))
    triage = Triage(
        settings.triage,
        settings.preferences,
        make_sessions(engine),
        router,
        make_decider("pay", SURE),
        StdoutSink(),
        settings.app.timezone,
    )

    await triage.run(_mail())

    assert router.systems == [triage.system]
    assert router.prompts == [triage.prompt(_mail(), "pay")]
    await engine.dispose()


def test_prompt_version_follows_template_and_needs(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    decider = make_decider("none", SURE)
    triage, _, _ = _triage(settings, engine, None, make_router, decider)
    same, _, _ = _triage(settings, engine, None, make_router, decider)

    def variant(cfg: Settings) -> Triage:
        return Triage(
            cfg.triage,
            cfg.preferences,
            same.sessions,
            same.router,
            decider,
            same.sink,
            settings.app.timezone,
        )

    def with_triage(**update: object) -> Settings:
        return settings.model_copy(update={"triage": settings.triage.model_copy(update=update)})

    changed = variant(with_triage(template=settings.triage.template + "!"))
    reordered = variant(with_triage(priority=list(reversed(settings.triage.priority))))
    instructed = variant(with_triage(instructions=settings.triage.instructions + "!"))
    more_mail = [*settings.preferences.mail, "Concert tickets need no action."]
    preferred = variant(
        settings.model_copy(
            update={"preferences": settings.preferences.model_copy(update={"mail": more_mail})}
        )
    )
    retrained_laya = settings.triage.laya.model_copy(
        update={"instructions": settings.triage.laya.instructions + "!"}
    )
    retrained = variant(with_triage(laya=retrained_laya))

    assert triage.version == same.version
    assert triage.question_version == same.question_version
    assert triage.version != changed.version
    assert triage.version != instructed.version
    assert triage.version != reordered.version
    assert triage.version != preferred.version
    assert triage.question_version != retrained.question_version
    assert triage.version == retrained.version


async def test_the_one_need_is_asked_and_tracked(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    answer = MailSummary(due_date=date(2026, 11, 30), summary="Power bill")
    decider = make_decider("pay", UNSURE)
    triage, sink, sessions = _triage(settings, engine, answer, make_router, decider)
    event = _mail()

    proposal = await triage.run(event)

    assert proposal is not None
    assert proposal.need == Need.PAY
    assert [(topic, key) for topic, _, key in sink.asked] == [(Topic.APPROVAL, str(proposal.id))]
    first = sink.asked[0][1].splitlines()
    assert first == [
        "pay by 30.11.2026",
        "Power bill",
        "gmail: Rechnung Oktober",
        "from billing@example.ch",
    ]
    async with sessions() as session:
        calls = {c.task_class: c for c in (await session.scalars(select(LlmCall))).all()}
        stored = (await session.scalars(select(Proposal))).all()
    decided = calls["decide"]
    summarised = calls[settings.triage.task_class]
    assert decided.raw_event_id == summarised.raw_event_id == event.id
    assert decided.backend == settings.triage.laya.backend
    assert decided.output == {"choice": "pay", "probabilities": {"pay": UNSURE}}
    assert decided.confidence == UNSURE
    assert decided.prompt_version == triage.question_version
    assert decided.valid
    assert summarised.prompt_version == triage.version
    assert summarised.model == "fake-model"
    assert summarised.output == answer.model_dump(mode="json")
    assert summarised.valid
    assert [p.llm_call_id for p in stored] == [decided.id]
    assert {p.message_ref for p in stored} == {"1"}
    assert {p.status for p in stored} == {ProposalStatus.PENDING}
    assert {p.due for p in stored} == {"2026-11-30"}
    await engine.dispose()


async def test_a_sure_none_asks_nothing_and_skips_the_llm_but_keeps_the_decision(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    decider = make_decider("none", SURE)
    triage, sink, sessions = _triage(settings, engine, None, make_router, decider)

    assert await triage.run(_mail()) is None

    assert sink.asked == []
    async with sessions() as session:
        [call] = (await session.scalars(select(LlmCall))).all()
    assert call.task_class == "decide"
    assert call.output == {"choice": "none", "probabilities": {"none": SURE}}
    await engine.dispose()


async def test_an_unsure_none_goes_to_a_quick_check(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    below = settings.triage.laya.check_below - 0.01
    answer = MailSummary(due_date=None, summary="Club news")
    triage, sink, _ = _triage(settings, engine, answer, make_router, make_decider("none", below))

    proposal = await triage.run(_mail())

    assert proposal is not None
    assert proposal.need == "none"
    [(topic, text, _)] = sink.asked
    assert topic is Topic.APPROVAL
    assert text.splitlines()[:2] == ["none", "Club news"]
    await engine.dispose()


async def test_skip_rules_answer_before_laya(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    decider = make_decider("pay", SURE)
    personal = SkipRule(reason="never sender", field="sender", pattern=r"@example\.ch$")
    preferences = settings.preferences.model_copy(update={"skip": [personal]})
    triage = Triage(
        settings.triage,
        preferences,
        make_sessions(engine),
        make_router(None),
        decider,
        StdoutSink(),
        settings.app.timezone,
    )
    invite = _mail(title="Updated invitation: Elternabend")
    ics = {"from": FROM, "attachments": ["Rechnung.pdf", "invite.ICS"]}
    calendar_file = _mail(title="Termin").model_copy(update={"payload": ics})
    stranger = _mail().model_copy(update={"sender": "someone@example.org"})

    assert triage.skip_reason(invite) == "calendar invitation"
    assert triage.skip_reason(calendar_file) == "calendar file attached"
    assert triage.skip_reason(_mail()) == "never sender"
    assert triage.skip_reason(stranger) == ""
    assert await triage.run(invite) is None
    assert decider.states == []
    await engine.dispose()


async def test_failed_llm_is_recorded(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    triage, sink, sessions = _triage(settings, engine, None, make_router, make_decider("pay", SURE))

    with pytest.raises(NoBackendError):
        await triage.run(_mail())

    assert sink.asked == []
    async with sessions() as session:
        calls = {c.task_class: c for c in (await session.scalars(select(LlmCall))).all()}
    failed = calls[settings.triage.task_class]
    assert calls["decide"].valid
    assert not failed.valid
    assert "down" in failed.error
    await engine.dispose()


async def test_failed_laya_is_recorded_and_the_llm_is_not_asked(
    settings: Settings, engine: AsyncEngine, make_decider: MakeDecider
) -> None:
    router = FakeRouter(settings, MailSummary(summary="Bill", due_date=None))
    sessions = make_sessions(engine)
    triage = Triage(
        settings.triage,
        settings.preferences,
        sessions,
        router,
        make_decider(None, SURE),
        StdoutSink(),
        settings.app.timezone,
    )

    with pytest.raises(NoBackendError):
        await triage.run(_mail())

    assert router.prompts == []
    async with sessions() as session:
        [call] = (await session.scalars(select(LlmCall))).all()
    assert call.task_class == "decide"
    assert not call.valid
    assert "laya down" in call.error
    await engine.dispose()


async def test_a_decision_is_recorded_once(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    answer = MailSummary(due_date=None, summary="Question from the Kita")
    decider = make_decider("reply", SURE)
    triage, _, sessions = _triage(settings, engine, answer, make_router, decider)
    proposal = await triage.run(_mail())
    assert proposal is not None

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


def test_none_is_an_action_laya_can_name(settings: Settings) -> None:
    criteria = settings.triage.laya.criteria

    assert set(criteria) == {"none", *(need.value for need in Need)}
    assert criteria["none"]


def test_the_model_writes_the_summary_before_the_due_date() -> None:
    schema = json.dumps(MailSummary.model_json_schema())

    assert list(MailSummary.model_fields) == ["summary", "due_date"]
    assert "need" not in schema
