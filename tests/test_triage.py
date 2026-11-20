from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid7
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeDecider, FakeRouter, MakeDecider, TasksApi, tasks_for
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from homeagent.actions.google_tasks import TaskListMissingError
from homeagent.config.settings import Need, Settings, SkipRule
from homeagent.core.events import Event, SourceKind
from homeagent.core.mailflow import PickDeadline, TaskText
from homeagent.core.mailtext import following_sunday
from homeagent.core.triage import Op, ProposalStatus, Triage
from homeagent.db.models import Base, LlmCall, Proposal, RawEvent
from homeagent.db.session import make_engine, make_sessions
from homeagent.llm.router import LlmRouter, NoBackendError
from homeagent.sinks.base import Topic
from homeagent.sinks.stdout import StdoutSink

USER = 42
FROM = "Billing Team <billing@example.ch>"
SURE = 0.97
UNSURE = 0.6
POWER_BILL = TaskText(task="Pay the power bill")

type MakeRouter = Callable[[list[BaseModel] | None], LlmRouter]


def _mail(
    body: str = "Bitte bezahlen Sie innert 30 Tagen.", title: str = "Rechnung Oktober"
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


async def _ingested(
    sessions: async_sessionmaker[AsyncSession], event: Event, payload_hash: str = ""
) -> Event:
    async with sessions() as session, session.begin():
        session.add(
            RawEvent(
                id=event.id,
                source=event.source_name,
                external_id=event.external_id,
                occurred_at=0,
                ingested_at=0,
                payload_hash=payload_hash,
            )
        )
    return event


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncEngine:
    engine = make_engine(tmp_path / "t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


def _triage(
    settings: Settings,
    engine: AsyncEngine,
    answers: list[BaseModel] | None,
    make_router: MakeRouter,
    decider: FakeDecider,
) -> tuple[Triage, StdoutSink, async_sessionmaker[AsyncSession]]:
    sink = StdoutSink()
    sessions = make_sessions(engine)
    triage = Triage(
        settings.triage,
        settings.preferences,
        sessions,
        make_router(answers),
        decider,
        sink,
        settings.app.timezone,
        tasks_for(settings, TasksApi(settings)),
    )
    return triage, sink, sessions


def test_task_prompt_carries_the_latest_message_and_about_me(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    triage, _, _ = _triage(settings, engine, None, make_router, make_decider("pay", SURE))
    flow = triage.flow
    reply = "Can you send the form?\n\nAm 3. Nov. 2026 schrieb Kita <kita@example.ch>:\n> old"
    long_body = "x" * (settings.triage.body_chars + 50)

    fields = flow.task_fields(_mail(reply), "reply")
    long_fields = flow.task_fields(_mail(long_body), "reply")
    prompt = flow.prompt("task", fields)

    assert list(fields) == ["about_me", "action", "sender", "subject", "attachments", "body"]
    assert fields["about_me"] == "\n".join(settings.preferences.about_me)
    assert fields["sender"] == FROM
    assert fields["attachments"] == "Rechnung.pdf, QR.png"
    assert fields["body"] == "Can you send the form?"
    assert long_fields["body"] == "x" * settings.triage.body_chars
    assert prompt.startswith("[[ ## about_me ## ]]\n")
    assert "[[ ## action ## ]]\nreply\n\n" in prompt
    assert prompt.endswith(settings.triage.prompts.task.closing)
    first = settings.preferences.task_instructions.splitlines()[0]
    assert settings.triage.prompts.indent + first in flow.systems["task"]
    assert "billing@example.ch" not in flow.systems["task"]


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
    router = FakeRouter(settings, [POWER_BILL])
    triage = Triage(
        settings.triage,
        settings.preferences,
        make_sessions(engine),
        router,
        make_decider("pay", SURE),
        StdoutSink(),
        settings.app.timezone,
        tasks_for(settings, TasksApi(settings)),
    )

    await triage.run(_mail())

    flow = triage.flow
    assert router.systems == [flow.systems["task"]]
    assert router.prompts == [flow.prompt("task", flow.task_fields(_mail(), "pay"))]
    assert router.schemas == ["TaskText"]
    await engine.dispose()


def test_prompt_version_follows_instructions_and_closing(
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
            make_router(None),
            decider,
            same.sink,
            settings.app.timezone,
            tasks_for(settings, TasksApi(settings)),
        )

    def with_preferences(**update: object) -> Settings:
        preferences = settings.preferences.model_copy(update=update)
        return settings.model_copy(update={"preferences": preferences})

    def with_triage(**update: object) -> Settings:
        return settings.model_copy(update={"triage": settings.triage.model_copy(update=update)})

    task = with_preferences(task_instructions=settings.preferences.task_instructions + "!")
    deadline = with_preferences(
        deadline_instructions=settings.preferences.deadline_instructions + "!"
    )
    closing = settings.triage.prompts.task.model_copy(
        update={"closing": settings.triage.prompts.task.closing + "!"}
    )
    reclosed = with_triage(prompts=settings.triage.prompts.model_copy(update={"task": closing}))
    retrained_laya = settings.triage.laya.model_copy(
        update={"instructions": settings.triage.laya.instructions + "!"}
    )
    retrained = variant(with_triage(laya=retrained_laya)).flow

    flow = triage.flow
    assert flow.versions == same.flow.versions
    assert flow.question_version == same.flow.question_version
    assert flow.versions["task"] != variant(task).flow.versions["task"]
    assert flow.versions["deadline"] == variant(task).flow.versions["deadline"]
    assert flow.versions["deadline"] != variant(deadline).flow.versions["deadline"]
    assert flow.versions["task"] != variant(reclosed).flow.versions["task"]
    assert flow.question_version != retrained.question_version
    assert flow.versions == retrained.versions


async def test_the_one_need_is_asked_and_tracked(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    decider = make_decider("pay", UNSURE)
    triage, sink, sessions = _triage(settings, engine, [POWER_BILL], make_router, decider)
    event = _mail()

    proposal = await triage.run(event)

    assert proposal is not None
    assert proposal.need == Need.PAY
    assert not proposal.unsure
    assert [(topic, card.key) for topic, card in sink.asked] == [(Topic.APPROVAL, str(proposal.id))]
    card = sink.asked[0][1]
    assert card.text.splitlines() == [
        "pay by 15.11.2026, deadline 11.12.2026",
        "Pay the power bill",
        "Mail from Billing Team, 11.11.2026: Rechnung Oktober",
    ]
    verdicts, actions, dues = card.buttons
    assert [b.label for b in verdicts] == ["Accept", "Nothing to do"]
    assert [b.arg for b in actions] == ["reply", "todo", "appointment", "keep"]
    assert [b.label for b in dues] == ["today", "tomorrow", "Sunday", "no date"]
    async with sessions() as session:
        calls = {c.task_class: c for c in (await session.scalars(select(LlmCall))).all()}
        stored = (await session.scalars(select(Proposal))).all()
    decided = calls["decide"]
    written = calls[settings.triage.task_class]
    assert decided.raw_event_id == written.raw_event_id == event.id
    assert decided.backend == settings.triage.laya.backend
    assert decided.output == {"choice": "pay", "probabilities": {"pay": UNSURE}}
    assert decided.confidence == UNSURE
    assert decided.prompt_version == triage.flow.question_version
    assert decided.valid
    assert written.prompt_version == triage.flow.versions["task"]
    assert written.schema_name == "TaskText"
    assert written.model == "fake-model"
    assert written.output == POWER_BILL.model_dump(mode="json")
    assert written.valid
    assert [p.llm_call_id for p in stored] == [decided.id]
    assert {p.message_ref for p in stored} == {"1"}
    assert {p.status for p in stored} == {ProposalStatus.PENDING}
    assert {(p.due, p.deadline) for p in stored} == {("2026-11-15", "2026-12-11")}
    assert {p.description for p in stored} == {
        "Mail from Billing Team, 11.11.2026: Rechnung Oktober"
    }
    await engine.dispose()


async def test_a_sure_none_is_asked_to_confirm_and_skips_the_llm_but_keeps_the_decision(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    decider = make_decider("none", SURE)
    triage, sink, sessions = _triage(settings, engine, None, make_router, decider)

    proposal = await triage.run(_mail())

    assert proposal is not None
    assert (proposal.need, proposal.final_need, proposal.unsure) == ("none", "none", False)
    [(topic, card)] = sink.asked
    assert topic is Topic.APPROVAL
    assert card.text.splitlines() == [
        "nothing to do",
        "Mail from Billing Team, 11.11.2026: Rechnung Oktober",
    ]
    confirm, actions = card.buttons
    assert [b.label for b in confirm] == ["Accept"]
    assert [b.arg for b in actions] == ["reply", "pay", "todo", "appointment", "keep"]
    async with sessions() as session:
        [call] = (await session.scalars(select(LlmCall))).all()
        stored = await session.get_one(Proposal, proposal.id)
    assert call.task_class == "decide"
    assert call.output == {"choice": "none", "probabilities": {"none": SURE}}
    assert stored.llm_call_id == call.id
    assert (stored.due, stored.deadline, stored.summary) == ("", "", "")

    missed = await triage.tap(str(proposal.id), Op.ACTION, "todo", USER)
    assert missed is not None
    assert missed.text.splitlines()[0] == "todo"
    assert [b.label for b in missed.buttons[0]] == ["Accept", "Nothing to do"]
    assert len(missed.buttons) == 3
    await engine.dispose()


async def test_a_confirmed_none_is_stored_as_accepted_none(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    triage, _, sessions = _triage(settings, engine, None, make_router, make_decider("none", SURE))
    api = TasksApi(settings)
    triage.tasks = tasks_for(settings, api)
    proposal = await triage.run(_mail())
    assert proposal is not None

    confirmed = await triage.tap(str(proposal.id), Op.ACCEPT, "", USER)

    assert confirmed is not None
    assert confirmed.text.splitlines()[::2] == ["nothing to do", "accepted"]
    async with sessions() as session:
        stored = await session.get_one(Proposal, proposal.id)
    assert (stored.final_need, stored.status) == ("none", ProposalStatus.ACCEPTED)
    assert api.requests == []
    await engine.dispose()


async def test_an_unsure_none_proposes_lay_s_second_choice(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    below = settings.triage.laya.check_below - 0.01
    decider = make_decider("none", below)
    decider.probabilities = {"none": below, "reply": 0.02, "pay": 1 - below - 0.02}
    triage, sink, _ = _triage(settings, engine, [POWER_BILL], make_router, decider)

    proposal = await triage.run(_mail())

    assert proposal is not None
    assert proposal.need == Need.PAY
    assert proposal.unsure
    [(topic, card)] = sink.asked
    assert topic is Topic.APPROVAL
    assert card.text.splitlines()[:2] == [
        "maybe pay by 15.11.2026, deadline 11.12.2026",
        "Pay the power bill",
    ]
    [verdicts, *_] = card.buttons
    assert [(b.label, b.op) for b in verdicts] == [
        ("Accept", Op.ACCEPT),
        ("Nothing to do", Op.NOTHING),
    ]
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
        tasks_for(settings, TasksApi(settings)),
    )
    invite = _mail(title="Updated invitation: Elternabend")
    ics = {"from": FROM, "attachments": ["Rechnung.pdf", "invite.ICS"]}
    calendar_file = _mail(title="Termin").model_copy(update={"payload": ics})
    stranger = _mail().model_copy(update={"sender": "someone@example.org"})

    flow = triage.flow
    assert flow.skip_reason(invite) == "calendar invitation"
    assert flow.skip_reason(calendar_file) == "calendar file attached"
    assert flow.skip_reason(_mail()) == "never sender"
    assert flow.skip_reason(stranger) == ""
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
    router = FakeRouter(settings, [POWER_BILL])
    sessions = make_sessions(engine)
    triage = Triage(
        settings.triage,
        settings.preferences,
        sessions,
        router,
        make_decider(None, SURE),
        StdoutSink(),
        settings.app.timezone,
        tasks_for(settings, TasksApi(settings)),
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
    no_deadline = PickDeadline(needs_deadline=False, deadline="none", relation="last_day")
    answers: list[BaseModel] = [TaskText(task="Reply to the Kita"), no_deadline]
    decider = make_decider("reply", SURE)
    triage, _, sessions = _triage(settings, engine, answers, make_router, decider)
    proposal = await triage.run(_mail(body="Kommt Ihr Kind am 24.12.?", title="Frage"))
    assert proposal is not None

    decided = await triage.tap(str(proposal.id), Op.NOTHING, "", USER)
    assert decided is not None
    assert decided.text.splitlines()[-1] == "nothing to do"
    assert decided.buttons == ()
    assert await triage.tap(str(proposal.id), Op.ACCEPT, "", USER) is None
    assert await triage.tap(str(proposal.id), Op.ACTION, "pay", USER) is None
    assert await triage.reply(proposal.message_ref, "Answer the Kita", USER) is None
    assert await triage.tap(str(uuid7()), Op.ACCEPT, "", USER) is None
    assert await triage.tap("not-a-uuid", Op.ACCEPT, "", USER) is None

    async with sessions() as session:
        stored = await session.get_one(Proposal, proposal.id)
    assert stored.status == ProposalStatus.DECLINED
    assert stored.decided_by == USER
    assert stored.decided_at is not None
    await engine.dispose()


async def test_a_mail_read_after_its_card_gets_no_second_card(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    decider = make_decider("pay", SURE)
    triage, sink, sessions = _triage(settings, engine, [POWER_BILL], make_router, decider)
    unread = await _ingested(sessions, _mail())
    first = await triage.run(unread)
    assert first is not None
    await triage.tap(str(first.id), Op.NOTHING, "", USER)
    read = await _ingested(sessions, unread.model_copy(update={"id": uuid7()}), "labels-changed")

    second = await triage.run(read)

    assert second is None
    assert len(sink.asked) == 1
    assert len(decider.states) == 1
    async with sessions() as session:
        assert len((await session.scalars(select(Proposal))).all()) == 1
    await engine.dispose()


async def test_corrections_change_the_final_values_and_keep_the_proposal(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    triage, _, sessions = _triage(
        settings, engine, [POWER_BILL], make_router, make_decider("pay", SURE)
    )
    api = TasksApi(settings)
    triage.tasks = tasks_for(settings, api)
    proposal = await triage.run(await _ingested(sessions, _mail()))
    assert proposal is not None
    key = str(proposal.id)
    today = datetime.now(ZoneInfo(settings.app.timezone)).date()

    changed = await triage.tap(key, Op.ACTION, "todo", USER)
    assert changed is not None
    assert changed.text.splitlines()[0].startswith("todo by ")
    assert [b.arg for b in changed.buttons[1]] == ["reply", "pay", "appointment", "keep"]
    assert await triage.tap(key, Op.DUE, "tomorrow", USER) is not None
    assert await triage.tap(key, Op.ACTION, "none", USER) is None
    assert await triage.tap(key, Op.DUE, "someday", USER) is None
    assert await triage.tap(key, "x", "", USER) is None
    renamed = await triage.reply(proposal.message_ref, " Pay the\npower bill  online ", USER)
    assert renamed is not None
    assert renamed.text.splitlines()[1] == "Pay the power bill online"
    assert await triage.reply(proposal.message_ref, "   ", USER) is None
    assert await triage.reply("other", "text", USER) is None

    async with sessions() as session:
        stored = await session.get_one(Proposal, proposal.id)
    assert (stored.need, stored.due, stored.summary) == ("pay", "2026-11-15", "Pay the power bill")
    assert (stored.final_need, stored.final_summary) == ("todo", "Pay the power bill online")
    assert stored.final_due == (today + timedelta(days=1)).isoformat()
    assert stored.status == ProposalStatus.PENDING

    for arg, expected in (("today", today), ("sunday", following_sunday(today)), ("none", None)):
        card = await triage.tap(key, Op.DUE, arg, USER)
        assert card is not None
        async with sessions() as session:
            stored = await session.get_one(Proposal, proposal.id)
        assert stored.final_due == (expected.isoformat() if expected else "")
    assert card.text.splitlines()[0] == "todo, deadline 11.12.2026"

    accepted = await triage.tap(key, Op.ACCEPT, "", USER)
    assert accepted is not None
    assert accepted.text.splitlines() == [
        "todo, deadline 11.12.2026",
        "Pay the power bill online",
        "Mail from Billing Team, 11.11.2026: Rechnung Oktober",
        "accepted, task in Home Agent",
    ]
    assert len(accepted.text.splitlines()) <= settings.telegram.limits.other_lines
    async with sessions() as session:
        stored = await session.get_one(Proposal, proposal.id)
    assert stored.status == ProposalStatus.ACCEPTED
    assert stored.decided_by == USER
    assert stored.task_ref == "T1"
    assert api.created == [
        (
            "L0",
            {
                "title": "Pay the power bill online",
                "notes": "Mail from Billing Team, 11.11.2026: Rechnung Oktober\n"
                "https://mail.google.com/mail/u/0/#all/m1",
            },
        )
    ]
    await engine.dispose()


def test_none_is_an_action_laya_can_name(settings: Settings) -> None:
    criteria = settings.triage.laya.criteria

    assert set(criteria) == {"none", *(need.value for need in Need)}
    assert criteria["none"]


def test_the_deadline_answer_decides_before_it_picks() -> None:
    schema = PickDeadline.model_json_schema()

    assert list(PickDeadline.model_fields) == ["needs_deadline", "deadline", "relation"]
    assert schema["properties"]["relation"]["enum"] == ["last_day", "event"]
    assert list(TaskText.model_fields) == ["task"]


async def test_an_accepted_keep_only_creates_an_undated_archive_task(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    archive = TaskText(task="Archive insurance statement from Helsana in Paperless")
    decider = make_decider("keep", SURE)
    triage, _, sessions = _triage(settings, engine, [archive], make_router, decider)
    api = TasksApi(settings)
    triage.tasks = tasks_for(settings, api)
    mail = _mail(body="Ihre Jahresabrechnung finden Sie im Anhang.", title="Jahresabrechnung")
    proposal = await triage.run(await _ingested(sessions, mail))
    assert proposal is not None
    assert proposal.need == Need.KEEP
    assert proposal.due == ""

    accepted = await triage.tap(str(proposal.id), Op.ACCEPT, "", USER)

    assert accepted is not None
    [(list_id, task)] = api.created
    assert list_id == "L0"
    assert task["title"] == archive.task
    assert "due" not in task
    assert {r.method for r in api.requests} == {"GET", "POST"}
    await engine.dispose()


async def test_a_task_from_a_mail_without_a_web_link_has_only_the_description(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    archive = TaskText(task="Archive invoice from Ozon in Paperless")
    triage, _, sessions = _triage(
        settings, engine, [archive], make_router, make_decider("keep", SURE)
    )
    api = TasksApi(settings)
    triage.tasks = tasks_for(settings, api)
    mail = _mail(title="Rechnung").model_copy(
        update={"source_name": "yandex", "external_id": "7:42"}
    )
    proposal = await triage.run(await _ingested(sessions, mail))
    assert proposal is not None

    await triage.tap(str(proposal.id), Op.ACCEPT, "", USER)

    [(_, task)] = api.created
    assert task["notes"] == proposal.description
    await engine.dispose()


async def test_a_corrected_none_becomes_a_task_named_after_the_mail_with_its_due_date(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    triage, _, sessions = _triage(settings, engine, None, make_router, make_decider("none", SURE))
    api = TasksApi(settings)
    triage.tasks = tasks_for(settings, api)
    proposal = await triage.run(await _ingested(sessions, _mail()))
    assert proposal is not None
    key = str(proposal.id)
    sunday = following_sunday(datetime.now(ZoneInfo(settings.app.timezone)).date())

    assert await triage.tap(key, Op.ACTION, "todo", USER) is not None
    assert await triage.tap(key, Op.DUE, "sunday", USER) is not None
    assert api.requests == []
    accepted = await triage.tap(key, Op.ACCEPT, "", USER)

    assert accepted is not None
    assert accepted.text.splitlines()[-1] == "accepted, task in Home Agent"
    [(list_id, task)] = api.created
    assert list_id == "L0"
    assert task["title"] == "Mail from Billing Team, 11.11.2026: Rechnung Oktober"
    assert task["due"] == f"{sunday.isoformat()}T00:00:00.000Z"
    await engine.dispose()


async def test_a_failed_task_keeps_the_card_open_to_accept_again(
    settings: Settings, engine: AsyncEngine, make_router: MakeRouter, make_decider: MakeDecider
) -> None:
    triage, _, sessions = _triage(
        settings, engine, [POWER_BILL], make_router, make_decider("pay", SURE)
    )
    api = TasksApi(settings, ["Groceries"])
    triage.tasks = tasks_for(settings, api)
    proposal = await triage.run(await _ingested(sessions, _mail()))
    assert proposal is not None

    with pytest.raises(TaskListMissingError, match="Home Agent"):
        await triage.tap(str(proposal.id), Op.ACCEPT, "", USER)
    async with sessions() as session:
        stored = await session.get_one(Proposal, proposal.id)
    assert (stored.status, stored.task_ref) == (ProposalStatus.PENDING, "")
    assert api.created == []

    api.lists.append({"id": "L7", "title": settings.triage.tasks.list_title})
    accepted = await triage.tap(str(proposal.id), Op.ACCEPT, "", USER)
    assert accepted is not None
    assert [list_id for list_id, _ in api.created] == ["L7"]
    await engine.dispose()
