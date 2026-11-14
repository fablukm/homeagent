from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from homeagent.config.settings import Need, Preferences
from homeagent.config.settings import Triage as TriageCfg
from homeagent.core.events import Event
from homeagent.core.mailflow import NONE, MailFlow
from homeagent.core.mailtext import following_sunday
from homeagent.db.models import LlmCall, Proposal
from homeagent.llm.base import DecisionBackend
from homeagent.llm.router import LlmRouter
from homeagent.sinks.base import Button, Card, Sink, Topic

DUE_FORMAT = "%d.%m.%Y"
UNSURE_MARK = "maybe "
NONE_TEXT = "nothing to do"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class Op(StrEnum):
    ACCEPT = "ok"
    NOTHING = "no"
    ACTION = "act"
    DUE = "due"


STATUS_TEXT = {ProposalStatus.ACCEPTED: "accepted", ProposalStatus.DECLINED: "nothing to do"}
VERDICTS = (
    Button("Accept", Op.ACCEPT),
    Button("Nothing to do", Op.NOTHING),
)
DUE_CHOICES: dict[str, tuple[str, Callable[[date], date | None]]] = {
    "today": ("today", lambda today: today),
    "tomorrow": ("tomorrow", lambda today: today + timedelta(days=1)),
    "sunday": ("Sunday", following_sunday),
    "none": ("no date", lambda today: None),
}


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def collapse(text: str) -> str:
    return " ".join(text.split())


class Triage:
    def __init__(
        self,
        cfg: TriageCfg,
        preferences: Preferences,
        sessions: async_sessionmaker[AsyncSession],
        router: LlmRouter,
        decider: DecisionBackend,
        sink: Sink,
        timezone: str,
    ) -> None:
        self.sessions = sessions
        self.sink = sink
        self.flow = MailFlow(cfg, preferences, router, decider, timezone, self._record)

    async def run(self, event: Event) -> Proposal | None:
        outcome = await self.flow.run(event)
        if outcome.decision is None:
            return None
        due = outcome.due.isoformat() if outcome.due else ""
        proposal = Proposal(
            ts=_now(),
            raw_event_id=event.id,
            llm_call_id=outcome.decision.id,
            need=outcome.action,
            unsure=outcome.unsure,
            due=due,
            deadline=outcome.deadline.isoformat() if outcome.deadline else "",
            summary=outcome.task,
            description=outcome.description,
            final_need=outcome.action,
            final_due=due,
            final_summary=outcome.task,
            status=ProposalStatus.PENDING,
        )
        async with self.sessions() as session, session.begin():
            session.add(proposal)
        ref = await self.sink.ask(Topic.APPROVAL, self.card(proposal))
        async with self.sessions() as session, session.begin():
            stored = await session.get_one(Proposal, proposal.id)
            stored.message_ref = ref
        proposal.message_ref = ref
        return proposal

    async def _record(self, call: LlmCall) -> None:
        async with self.sessions() as session, session.begin():
            session.add(call)

    def card(self, proposal: Proposal) -> Card:
        nothing = proposal.final_need == NONE
        head = NONE_TEXT if nothing else proposal.final_need
        if proposal.final_due:
            head = f"{head} by {date.fromisoformat(proposal.final_due):{DUE_FORMAT}}"
        if proposal.deadline:
            head = f"{head}, deadline {date.fromisoformat(proposal.deadline):{DUE_FORMAT}}"
        if proposal.unsure:
            head = UNSURE_MARK + head
        lines = [line for line in (head, proposal.final_summary, proposal.description) if line]
        if proposal.status != ProposalStatus.PENDING:
            lines.append(STATUS_TEXT[ProposalStatus(proposal.status)])
            return Card(str(proposal.id), "\n".join(lines))
        actions = tuple(
            Button(n.value, Op.ACTION, n.value) for n in Need if n != proposal.final_need
        )
        if nothing:
            return Card(str(proposal.id), "\n".join(lines), ((VERDICTS[0],), actions))
        dues = tuple(Button(label, Op.DUE, arg) for arg, (label, _) in DUE_CHOICES.items())
        return Card(str(proposal.id), "\n".join(lines), (VERDICTS, actions, dues))

    def today(self) -> date:
        return datetime.now(self.flow.zone).date()

    def apply(self, proposal: Proposal, op: str, arg: str, user_id: int) -> bool:
        if op in {Op.ACCEPT, Op.NOTHING}:
            accepted = op == Op.ACCEPT
            proposal.status = ProposalStatus.ACCEPTED if accepted else ProposalStatus.DECLINED
            proposal.decided_at = _now()
            proposal.decided_by = user_id
            return True
        if op == Op.ACTION and arg in set(Need):
            proposal.final_need = arg
            return True
        if op == Op.DUE and arg in DUE_CHOICES:
            due = DUE_CHOICES[arg][1](self.today())
            proposal.final_due = due.isoformat() if due else ""
            return True
        return False

    async def tap(self, key: str, op: str, arg: str, user_id: int) -> Card | None:
        try:
            proposal_id = UUID(key)
        except ValueError:
            return None
        async with self.sessions() as session, session.begin():
            proposal = await session.get(Proposal, proposal_id)
            if proposal is None or proposal.status != ProposalStatus.PENDING:
                return None
            if not self.apply(proposal, op, arg, user_id):
                return None
            return self.card(proposal)

    async def reply(self, ref: str, text: str, user_id: int) -> Card | None:
        summary = collapse(text)
        async with self.sessions() as session, session.begin():
            proposal = await session.scalar(
                select(Proposal).where(
                    Proposal.message_ref == ref, Proposal.status == ProposalStatus.PENDING
                )
            )
            if proposal is None or not summary:
                return None
            proposal.final_summary = summary
            return self.card(proposal)
