from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import FakeRouter, MakeDecider
from pydantic import BaseModel

from homeagent.config.settings import Settings
from homeagent.core.events import Event, SourceKind
from homeagent.core.mailflow import MailFlow, PickDeadline, TaskText
from homeagent.db.models import LlmCall

RECEIVED = date(2026, 11, 11)
SUNDAY = date(2026, 11, 15)
BUSINESS = "Kita Sonnenschein <info@kita.example.ch>"
PRIVATE = "Anna Muster <anna.muster@gmail.com>"
FORM = TaskText(task="Bring the signed form to the Kita")


def _mail(body: str, title: str = "Formular", sender: str = BUSINESS) -> Event:
    return Event(
        source=SourceKind.MAIL,
        source_name="gmail",
        external_id="m1",
        occurred_at=datetime(2026, 11, 10, 23, 30, tzinfo=UTC),
        title=title,
        body=body,
        sender=sender.split("<")[1].rstrip(">"),
        payload={"from": sender, "attachments": []},
    )


def _flow(
    settings: Settings, answers: list[BaseModel], action: str, make_decider: MakeDecider
) -> tuple[MailFlow, FakeRouter, list[LlmCall]]:
    calls: list[LlmCall] = []

    async def record(call: LlmCall) -> None:
        calls.append(call)

    router = FakeRouter(settings, answers)
    flow = MailFlow(
        settings.triage,
        settings.preferences,
        router,
        make_decider(action, 1.0),
        settings.app.timezone,
        record,
    )
    return flow, router, calls


async def test_an_invoice_number_writes_the_pay_task_without_the_model(
    settings: Settings, make_decider: MakeDecider
) -> None:
    flow, router, _ = _flow(settings, [], "pay", make_decider)
    mail = _mail(
        "Danke für Ihren Einkauf.",
        "Galaxus - Neue Rechnung 88254299",
        "Galaxus AG <noreply@galaxus.ch>",
    )

    outcome = await flow.run(mail)

    assert outcome.task == "Pay Galaxus invoice 88254299"
    assert outcome.deadline == RECEIVED + timedelta(days=settings.triage.rules.invoice_days)
    assert outcome.due == SUNDAY
    assert router.prompts == []


async def test_a_single_strong_deadline_skips_the_model(
    settings: Settings, make_decider: MakeDecider
) -> None:
    flow, router, _ = _flow(settings, [FORM], "todo", make_decider)

    outcome = await flow.run(_mail("Bitte anmelden. Anmeldeschluss: 18. Dezember"))

    assert outcome.deadline == date(2026, 12, 18)
    assert router.schemas == ["TaskText"]


async def test_the_model_picks_the_deadline_from_the_found_dates(
    settings: Settings, make_decider: MakeDecider
) -> None:
    pick = PickDeadline(needs_deadline=True, deadline="27/11/2026", relation="event")
    flow, router, calls = _flow(settings, [FORM, pick], "todo", make_decider)

    outcome = await flow.run(_mail("Bitte bringen Sie das Formular am 20.11. oder am 27.11."))

    assert outcome.deadline == date(2026, 11, 26)
    assert outcome.due == SUNDAY
    assert outcome.task == FORM.task
    assert router.schemas == ["TaskText", "PickDeadline"]
    assert "- 20/11/2026 (Fri): " in router.prompts[1]
    assert "[[ ## received ## ]]\nWed 11/11/2026" in router.prompts[1]
    assert router.systems[1] == flow.systems["deadline"]
    assert [c.schema_name for c in calls] == ["action", "TaskText", "PickDeadline"]


@pytest.mark.parametrize(
    ("pick", "expected"),
    [
        (
            PickDeadline(needs_deadline=True, deadline="20/11/2026", relation="last_day"),
            date(2026, 11, 20),
        ),
        (PickDeadline(needs_deadline=True, deadline="21/11/2026", relation="last_day"), None),
        (PickDeadline(needs_deadline=False, deadline="20/11/2026", relation="last_day"), None),
        (PickDeadline(needs_deadline=True, deadline="none", relation="last_day"), None),
        (PickDeadline(needs_deadline=True, deadline="11/11/2026", relation="event"), None),
    ],
)
def test_only_a_found_date_on_or_after_receipt_becomes_the_deadline(
    settings: Settings, make_decider: MakeDecider, pick: PickDeadline, expected: date | None
) -> None:
    flow, _, _ = _flow(settings, [], "todo", make_decider)
    found = flow.text.dates("am 11.11. oder am 20.11.", RECEIVED)

    assert flow.picked(pick, found, RECEIVED) == expected


@pytest.mark.parametrize(
    ("action", "sender", "title", "deadline", "due"),
    [
        ("appointment", BUSINESS, "Termin", None, RECEIVED),
        ("keep", BUSINESS, "Police", None, None),
        ("reply", BUSINESS, "Frage", None, RECEIVED),
        ("reply", BUSINESS, "Frage", date(2026, 12, 20), RECEIVED),
        ("reply", PRIVATE, "Frage", None, SUNDAY),
        ("reply", PRIVATE, "Frage", date(2026, 12, 20), SUNDAY),
        ("reply", PRIVATE, "Frage", date(2026, 11, 16), RECEIVED),
        ("todo", BUSINESS, "Formular", None, None),
        ("todo", BUSINESS, "Formular", date(2026, 11, 30), SUNDAY),
        ("todo", BUSINESS, "Formular", date(2026, 11, 16), date(2026, 11, 12)),
        ("todo", BUSINESS, "Formular", RECEIVED, RECEIVED),
        ("pay", BUSINESS, "Rechnung", date(2026, 11, 30), SUNDAY),
        ("pay", BUSINESS, "Mahnung", date(2026, 11, 30), RECEIVED),
        ("pay", BUSINESS, "Rechnung", date(2026, 11, 16), date(2026, 11, 12)),
    ],
)
def test_due_dates_follow_the_rules(
    settings: Settings,
    make_decider: MakeDecider,
    action: str,
    sender: str,
    title: str,
    deadline: date | None,
    due: date | None,
) -> None:
    flow, _, _ = _flow(settings, [], action, make_decider)

    assert flow.due(_mail("", title, sender), action, deadline) == due


async def test_the_description_names_sender_date_and_subject(
    settings: Settings, make_decider: MakeDecider
) -> None:
    flow, _, _ = _flow(settings, [], "none", make_decider)

    outcome = await flow.run(_mail("Hallo", "Elternabend", PRIVATE))

    assert outcome.description == "Mail from Anna Muster, 11.11.2026: Elternabend"
    assert outcome.action == "none"
    assert outcome.task == ""
