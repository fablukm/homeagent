import json
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from homeagent.config.settings import Preferences
from homeagent.config.settings import Triage as TriageCfg
from homeagent.core.events import Event
from homeagent.db.models import LlmCall, Proposal
from homeagent.llm.base import Answer, DecisionBackend, Question
from homeagent.llm.router import LlmRouter, NoBackendError
from homeagent.sinks.base import Sink, Topic

VERSION_LEN = 12
DUE_FORMAT = "%d.%m.%Y"
PREFERENCE_MARK = "- "
DECIDE_CLASS = "decide"
ATTACHMENT_LIST = ", "
ATTACHMENT_LINES = "\n"


type NoNeed = Literal["none"]
NONE: NoNeed = "none"


class MailSummary(BaseModel):
    summary: str
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
        preferences: Preferences,
        sessions: async_sessionmaker[AsyncSession],
        router: LlmRouter,
        decider: DecisionBackend,
        sink: Sink,
        timezone: str,
    ) -> None:
        self.cfg = cfg
        self.sessions = sessions
        self.router = router
        self.decider = decider
        self.sink = sink
        self.zone = ZoneInfo(timezone)
        self.skip = [*cfg.skip, *preferences.skip]
        self.system = cfg.instructions.format(
            needs="\n".join(f"{need.value}: {text}" for need, text in cfg.needs.items()),
            priority=", ".join(need.value for need in cfg.priority),
            preferences="\n".join(f"{PREFERENCE_MARK}{line}" for line in preferences.mail),
        )
        self.version = _sha(self.system + cfg.template)[:VERSION_LEN]
        laya = cfg.laya
        self.question = Question(laya.question, "choice", laya.instructions, laya.criteria)
        self.question_version = _sha(laya.instructions + json.dumps(laya.criteria))[:VERSION_LEN]

    def skip_reason(self, event: Event) -> str:
        fields = {
            "subject": event.title,
            "sender": event.sender,
            "body": event.body,
            "attachments": ATTACHMENT_LINES.join(event.payload.get("attachments", [])),
        }
        hits = (rule.reason for rule in self.skip if re.search(rule.pattern, fields[rule.field]))
        return next(hits, "")

    def state(self, event: Event) -> dict[str, str]:
        return {
            "from": event.payload["from"],
            "subject": event.title,
            "attachments": ATTACHMENT_LIST.join(event.payload.get("attachments", [])),
            "body": event.body[: self.cfg.body_chars],
        }

    def prompt(self, event: Event, action: str) -> str:
        return self.cfg.template.format(
            action=action,
            sender=event.sender,
            date=event.occurred_at.astimezone(self.zone).date().isoformat(),
            subject=event.title,
            attachments=ATTACHMENT_LIST.join(event.payload.get("attachments", [])),
            body=event.body[: self.cfg.body_chars],
        )

    async def run(self, event: Event) -> Proposal | None:
        if self.skip_reason(event):
            return None
        decision_call, answer = await self._decide(event)
        action = str(answer.value)
        if action == NONE and answer.confidence >= self.cfg.laya.check_below:
            return None
        summary = await self._summarise(event, action)
        proposal = Proposal(
            ts=_now(),
            raw_event_id=event.id,
            llm_call_id=decision_call.id,
            need=action,
            due=summary.due_date.isoformat() if summary.due_date else "",
            summary=summary.summary,
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

    async def _decide(self, event: Event) -> tuple[LlmCall, Answer]:
        state = self.state(event)
        call = LlmCall(
            ts=_now(),
            raw_event_id=event.id,
            task_class=DECIDE_CLASS,
            backend=self.decider.name,
            model=self.decider.model,
            prompt_hash=_sha(json.dumps(state, sort_keys=True)),
            schema_name=self.question.id,
            prompt_version=self.question_version,
        )
        try:
            decision = await self.decider.decide(state, [self.question])
        except NoBackendError as exc:
            call.valid = False
            call.error = repr(exc.__cause__ or exc)
            await self._record(call)
            raise
        [answer] = decision.answers
        call.output = {"choice": answer.value, "probabilities": answer.probabilities}
        call.confidence = answer.confidence
        call.prompt_tokens = decision.usage.prompt_tokens
        call.latency_ms = decision.usage.latency_ms
        await self._record(call)
        return call, answer

    async def _summarise(self, event: Event, action: str) -> MailSummary:
        prompt = self.prompt(event, action)
        call = LlmCall(
            ts=_now(),
            raw_event_id=event.id,
            task_class=self.cfg.task_class,
            backend="",
            model="",
            prompt_hash=_sha(self.system + prompt),
            schema_name=MailSummary.__name__,
            prompt_version=self.version,
        )
        try:
            completion = await self.router.complete(
                self.cfg.task_class, prompt, MailSummary, system=self.system
            )
        except NoBackendError as exc:
            call.valid = False
            call.error = repr(exc.__cause__ or exc)
            await self._record(call)
            raise
        call.backend = completion.backend
        call.model = completion.model
        call.output = completion.value.model_dump(mode="json")
        call.prompt_tokens = completion.usage.prompt_tokens
        call.completion_tokens = completion.usage.completion_tokens
        call.latency_ms = completion.usage.latency_ms
        call.load_ms = completion.usage.load_ms
        call.eval_ms = completion.usage.eval_ms
        await self._record(call)
        return completion.value

    async def _record(self, call: LlmCall) -> None:
        async with self.sessions() as session, session.begin():
            session.add(call)

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
