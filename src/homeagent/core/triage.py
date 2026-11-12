from datetime import UTC, date, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from homeagent.config.settings import Need
from homeagent.config.settings import Triage as TriageCfg
from homeagent.core.events import Event
from homeagent.db.models import LlmCall, Proposal
from homeagent.llm.router import LlmRouter, NoBackendError
from homeagent.sinks.base import Sink, Topic

VERSION_LEN = 12
DUE_FORMAT = "%d.%m.%Y"
PREFERENCE_MARK = "- "


type NoNeed = Literal["none"]


class MailTriage(BaseModel):
    summary: str
    need: Need | NoNeed
    due_date: date | None


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _sha(text: str) -> str:
    return sha256(text.encode()).hexdigest()


class Triage:
    def __init__(
        self,
        cfg: TriageCfg,
        preferences: list[str],
        sessions: async_sessionmaker[AsyncSession],
        router: LlmRouter,
        sink: Sink,
        timezone: str,
    ) -> None:
        self.cfg = cfg
        self.sessions = sessions
        self.router = router
        self.sink = sink
        self.zone = ZoneInfo(timezone)
        self.system = cfg.instructions.format(
            needs="\n".join(f"{need.value}: {text}" for need, text in cfg.needs.items()),
            priority=", ".join(need.value for need in cfg.priority),
            preferences="\n".join(f"{PREFERENCE_MARK}{line}" for line in preferences),
        )
        self.version = _sha(self.system + cfg.template)[:VERSION_LEN]

    def prompt(self, event: Event) -> str:
        return self.cfg.template.format(
            sender=event.sender,
            date=event.occurred_at.astimezone(self.zone).date().isoformat(),
            subject=event.title,
            attachments=", ".join(event.payload.get("attachments", [])),
            body=event.body[: self.cfg.body_chars],
        )

    async def run(self, event: Event) -> Proposal | None:
        prompt = self.prompt(event)
        call = LlmCall(
            ts=_now(),
            raw_event_id=event.id,
            task_class=self.cfg.task_class,
            backend="",
            model="",
            prompt_hash=_sha(self.system + prompt),
            schema_name=MailTriage.__name__,
            prompt_version=self.version,
        )
        try:
            completion = await self.router.complete(
                self.cfg.task_class, prompt, MailTriage, system=self.system
            )
        except NoBackendError as exc:
            call.valid = False
            call.error = repr(exc.__cause__ or exc)
            async with self.sessions() as session, session.begin():
                session.add(call)
            raise
        triage = completion.value
        call.backend = completion.backend
        call.model = completion.model
        call.output = triage.model_dump(mode="json")
        call.prompt_tokens = completion.usage.prompt_tokens
        call.completion_tokens = completion.usage.completion_tokens
        call.latency_ms = completion.usage.latency_ms
        call.load_ms = completion.usage.load_ms
        call.eval_ms = completion.usage.eval_ms
        async with self.sessions() as session, session.begin():
            session.add(call)
        if not isinstance(triage.need, Need):
            return None
        proposal = Proposal(
            ts=_now(),
            raw_event_id=event.id,
            llm_call_id=call.id,
            need=triage.need.value,
            due=triage.due_date.isoformat() if triage.due_date else "",
            summary=triage.summary,
            status=ProposalStatus.PENDING,
        )
        async with self.sessions() as session, session.begin():
            session.add(proposal)
        ref = await self.sink.ask(Topic.APPROVAL, self.text(event, proposal), str(proposal.id))
        async with self.sessions() as session, session.begin():
            stored = await session.get_one(Proposal, proposal.id)
            stored.message_ref = ref
        proposal.message_ref = ref
        return proposal

    def text(self, event: Event, proposal: Proposal) -> str:
        head = proposal.need
        if proposal.due:
            head = f"{head} by {date.fromisoformat(proposal.due):{DUE_FORMAT}}"
        return "\n".join(
            [head, proposal.summary, f"{event.source_name}: {event.title}", f"from {event.sender}"]
        )

    async def decide(self, key: str, accepted: bool, user_id: int) -> bool:
        try:
            proposal_id = UUID(key)
        except ValueError:
            return False
        async with self.sessions() as session, session.begin():
            proposal = await session.get(Proposal, proposal_id)
            if proposal is None or proposal.status != ProposalStatus.PENDING:
                return False
            proposal.status = ProposalStatus.ACCEPTED if accepted else ProposalStatus.DECLINED
            proposal.decided_at = _now()
            proposal.decided_by = user_id
        return True
