import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr
from hashlib import sha256
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from homeagent.config.settings import Need, NodePrompt, Preferences
from homeagent.config.settings import Triage as TriageCfg
from homeagent.core.events import Event
from homeagent.core.mailtext import Found, MailText, following_sunday
from homeagent.db.models import LlmCall
from homeagent.llm.base import DecisionBackend, Question
from homeagent.llm.router import LlmRouter, NoBackendError

VERSION_LEN = 12
DECIDE_CLASS = "decide"
NONE = "none"
ATTACHMENT_LIST = ", "
ATTACHMENT_LINES = "\n"
FIELD_GAP = "\n\n"

type Record = Callable[[LlmCall], Awaitable[None]]


class TaskText(BaseModel):
    task: str


class PickDeadline(BaseModel):
    needs_deadline: bool
    deadline: str
    relation: Literal["last_day", "event"]


class Update(TypedDict, total=False):
    skip: str
    decision: LlmCall
    action: str
    unsure: bool
    task: str
    deadline: date | None
    due: date | None


class MailState(Update):
    event: Event


@dataclass(frozen=True)
class Outcome:
    skip: str
    decision: LlmCall | None
    action: str
    unsure: bool
    task: str
    deadline: date | None
    due: date | None
    description: str


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _sha(text: str) -> str:
    return sha256(text.encode()).hexdigest()


class MailFlow:
    def __init__(
        self,
        cfg: TriageCfg,
        preferences: Preferences,
        router: LlmRouter,
        decider: DecisionBackend,
        timezone: str,
        record: Record,
    ) -> None:
        self.cfg = cfg
        self.router = router
        self.decider = decider
        self.zone = ZoneInfo(timezone)
        self.record = record
        self.text = MailText(cfg.text, cfg.body_chars)
        self.skip_rules = [*cfg.skip, *preferences.skip]
        self.about_me = ATTACHMENT_LINES.join(preferences.about_me)
        rules = cfg.rules
        self.strong = re.compile(rules.strong_deadline)
        self.not_deadline = re.compile(rules.not_deadline)
        self.reminder = re.compile(rules.reminder)
        self.invoice = re.compile(rules.invoice_number)
        self.org_noise = re.compile(rules.org_noise)
        laya = cfg.laya
        self.question = Question(laya.question, "choice", laya.instructions, laya.criteria)
        self.question_version = _sha(laya.instructions + json.dumps(laya.criteria))[:VERSION_LEN]
        self.systems = {
            "task": self._system(cfg.prompts.task, preferences.task_instructions),
            "deadline": self._system(cfg.prompts.deadline, preferences.deadline_instructions),
        }
        self.closings = {"task": cfg.prompts.task.closing, "deadline": cfg.prompts.deadline.closing}
        self.versions = {
            node: _sha(system + self.closings[node])[:VERSION_LEN]
            for node, system in self.systems.items()
        }
        graph = StateGraph(MailState)
        graph.add_node("skip", self._skip)
        graph.add_node("classify", self._classify)
        graph.add_node("task", self._task)
        graph.add_node("deadline", self._deadline)
        graph.add_node("due", self._due)
        graph.add_edge(START, "skip")
        graph.add_conditional_edges("skip", self._after_skip, ["classify", END])
        graph.add_conditional_edges("classify", self._after_classify, ["task", END])
        graph.add_edge("task", "deadline")
        graph.add_edge("deadline", "due")
        graph.add_edge("due", END)
        self.graph = graph.compile()

    def _system(self, node: NodePrompt, instructions: str) -> str:
        indent = self.cfg.prompts.indent
        return node.system.format(
            instructions="\n".join(indent + line for line in instructions.split("\n"))
        )

    async def run(self, event: Event) -> Outcome:
        state = MailState(**await self.graph.ainvoke(MailState(event=event)))
        received = self.received(event)
        name, address = parseaddr(event.payload["from"])
        description = self.cfg.rules.description.format(
            sender=name or address,
            date=f"{received:{self.cfg.rules.description_date}}",
            subject=event.title,
        )
        return Outcome(
            skip=state.get("skip", ""),
            decision=state.get("decision"),
            action=state.get("action", NONE),
            unsure=state.get("unsure", False),
            task=state.get("task", ""),
            deadline=state.get("deadline"),
            due=state.get("due"),
            description=description,
        )

    def received(self, event: Event) -> date:
        return event.occurred_at.astimezone(self.zone).date()

    def skip_reason(self, event: Event) -> str:
        fields = {
            "subject": event.title,
            "sender": event.sender,
            "body": event.body,
            "attachments": ATTACHMENT_LINES.join(event.payload.get("attachments", [])),
        }
        hits = (r.reason for r in self.skip_rules if re.search(r.pattern, fields[r.field]))
        return next(hits, "")

    def laya_state(self, event: Event) -> dict[str, str]:
        return {
            "from": event.payload["from"],
            "subject": event.title,
            "attachments": ATTACHMENT_LIST.join(event.payload.get("attachments", [])),
            "body": event.body[: self.cfg.body_chars],
        }

    def prompt(self, node: str, fields: dict[str, str]) -> str:
        parts = [self.cfg.prompts.field.format(name=k, value=v) for k, v in fields.items()]
        return FIELD_GAP.join([*parts, self.closings[node]])

    def task_fields(self, event: Event, action: str) -> dict[str, str]:
        return {
            "about_me": self.about_me,
            "action": action,
            "sender": event.payload["from"],
            "subject": event.title,
            "attachments": ATTACHMENT_LIST.join(event.payload.get("attachments", [])),
            "body": self.text.clean(event.body),
        }

    def deadline_fields(self, event: Event, action: str, found: list[Found]) -> dict[str, str]:
        prompts = self.cfg.prompts
        return {
            "action": action,
            "received": f"{self.received(event):{prompts.received}}",
            "sender": event.payload["from"],
            "subject": event.title,
            "body": self.text.clean(event.body),
            "dates": "\n".join(prompts.date_line.format(day=f.day, quote=f.quote) for f in found),
        }

    def _skip(self, state: MailState) -> Update:
        return {"skip": self.skip_reason(state["event"])}

    @staticmethod
    def _after_skip(state: MailState) -> str:
        return END if state.get("skip") else "classify"

    @staticmethod
    def _after_classify(state: MailState) -> str:
        return END if state.get("action", NONE) == NONE else "task"

    async def _classify(self, state: MailState) -> Update:
        event = state["event"]
        laya_state = self.laya_state(event)
        call = LlmCall(
            ts=_now(),
            raw_event_id=event.id,
            task_class=DECIDE_CLASS,
            backend=self.decider.name,
            model=self.decider.model,
            prompt_hash=_sha(json.dumps(laya_state, sort_keys=True)),
            schema_name=self.question.id,
            prompt_version=self.question_version,
        )
        try:
            decision = await self.decider.decide(laya_state, [self.question])
        except NoBackendError as exc:
            call.valid = False
            call.error = repr(exc.__cause__ or exc)
            await self.record(call)
            raise
        [answer] = decision.answers
        call.output = {"choice": answer.value, "probabilities": answer.probabilities}
        call.confidence = answer.confidence
        call.prompt_tokens = decision.usage.prompt_tokens
        call.latency_ms = decision.usage.latency_ms
        await self.record(call)
        action = str(answer.value)
        if action != NONE or answer.confidence >= self.cfg.laya.check_below:
            return {"decision": call, "action": action, "unsure": False}
        others = {k: p for k, p in answer.probabilities.items() if k != NONE}
        second = max(others, key=lambda k: others[k], default=NONE)
        return {"decision": call, "action": second, "unsure": True}

    async def _task(self, state: MailState) -> Update:
        event, action = state["event"], state.get("action", NONE)
        template = self.pay_task(event, action)
        if template:
            return {"task": template}
        answer = await self._complete("task", event, self.task_fields(event, action), TaskText)
        return {"task": answer.task}

    def pay_task(self, event: Event, action: str) -> str:
        match = self.invoice.search(event.title)
        if action != Need.PAY or not match:
            return ""
        return self.cfg.rules.pay_task.format(org=self.org(event), number=match["number"])

    def org(self, event: Event) -> str:
        name, address = parseaddr(event.payload["from"])
        name = " ".join(self.org_noise.sub("", name.strip('"')).split()).strip(" ,-")
        if name and "@" not in name:
            return name
        labels = address.split("@")[-1].split(".")
        return labels[-2].capitalize() if len(labels) > 1 else address

    async def _deadline(self, state: MailState) -> Update:
        event, action = state["event"], state.get("action", NONE)
        rules = self.cfg.rules
        if action not in rules.deadline_actions:
            return {"deadline": None}
        received = self.received(event)
        found = self.text.dates(event.body, received)
        strong = [
            f
            for f in found
            if self.strong.search(f.quote) and not self.not_deadline.search(f.quote)
        ]
        deadline: date | None = None
        if len(strong) == 1:
            deadline = strong[0].day
        elif found:
            fields = self.deadline_fields(event, action, found)
            pick = await self._complete("deadline", event, fields, PickDeadline)
            deadline = self.picked(pick, found, received)
        if action == Need.PAY and deadline is None:
            deadline = received + timedelta(days=rules.invoice_days)
        return {"deadline": deadline}

    def picked(self, pick: PickDeadline, found: list[Found], received: date) -> date | None:
        if not pick.needs_deadline:
            return None
        try:
            chosen = datetime.strptime(pick.deadline.strip(), self.cfg.prompts.day).date()
        except ValueError:
            return None
        if chosen not in {f.day for f in found}:
            return None
        if pick.relation == "event":
            chosen -= timedelta(days=1)
        return chosen if chosen >= received else None

    def _due(self, state: MailState) -> Update:
        action = state.get("action", NONE)
        return {"due": self.due(state["event"], action, state.get("deadline"))}

    def due(self, event: Event, action: str, deadline: date | None) -> date | None:
        received = self.received(event)
        if action == Need.APPOINTMENT:
            return received
        if action == Need.KEEP:
            return None
        sunday = following_sunday(received)
        soon = deadline is not None and (deadline - sunday).days < self.cfg.rules.sunday_margin
        next_day = min(received + timedelta(days=1), deadline) if deadline else received
        if action == Need.REPLY:
            return sunday if self.private(event) and not soon else received
        if action == Need.TODO:
            return None if deadline is None else next_day if soon else sunday
        reminder = self.reminder.search(f"{event.title} {event.body[: self.cfg.body_chars]}")
        picked = received if reminder else sunday
        return min(picked, next_day if soon else sunday)

    def private(self, event: Event) -> bool:
        address = parseaddr(event.payload["from"])[1].lower()
        return address.split("@")[-1] in self.cfg.rules.private_domains

    async def _complete[T: BaseModel](
        self, node: str, event: Event, fields: dict[str, str], schema: type[T]
    ) -> T:
        system = self.systems[node]
        prompt = self.prompt(node, fields)
        call = LlmCall(
            ts=_now(),
            raw_event_id=event.id,
            task_class=self.cfg.task_class,
            backend="",
            model="",
            prompt_hash=_sha(system + prompt),
            schema_name=schema.__name__,
            prompt_version=self.versions[node],
        )
        try:
            completion = await self.router.complete(
                self.cfg.task_class, prompt, schema, system=system
            )
        except NoBackendError as exc:
            call.valid = False
            call.error = repr(exc.__cause__ or exc)
            await self.record(call)
            raise
        call.backend = completion.backend
        call.model = completion.model
        call.output = completion.value.model_dump(mode="json")
        call.prompt_tokens = completion.usage.prompt_tokens
        call.completion_tokens = completion.usage.completion_tokens
        call.latency_ms = completion.usage.latency_ms
        call.load_ms = completion.usage.load_ms
        call.eval_ms = completion.usage.eval_ms
        await self.record(call)
        return completion.value
