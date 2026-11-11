from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from homeagent.actions.base import ACTIONS, ActionContext, ActionResult, ActionStatus
from homeagent.core.events import Event
from homeagent.core.rules import Rule, RuleEngine
from homeagent.db.models import ActionRow, RuleRun
from homeagent.sinks.base import Sink


def rule_sha(rule: Rule) -> str:
    return sha256(rule.model_dump_json().encode()).hexdigest()


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


@dataclass
class DispatchReport:
    rule_runs: int = 0
    results: list[ActionResult] = field(default_factory=list[ActionResult])
    already_fired: int = 0


class Dispatcher:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], rules: RuleEngine, sink: Sink
    ) -> None:
        self.sessions = sessions
        self.rules = rules
        self.sink = sink

    async def run(self, event: Event, mode: str) -> DispatchReport:
        report = DispatchReport()
        matched: list[tuple[Rule, UUID]] = []
        async with self.sessions() as session, session.begin():
            for rule in self.rules.store.reload():
                hit = self.rules.matches(rule, event, mode)
                run = RuleRun(
                    raw_event_id=event.id,
                    rule_id=rule.id,
                    rule_sha=rule_sha(rule),
                    matched=hit,
                    decided_at=_now(),
                )
                session.add(run)
                await session.flush()
                report.rule_runs += 1
                if hit:
                    matched.append((rule, run.id))
        for rule, run_id in matched:
            for spec in rule.actions:
                ctx = ActionContext(event, rule, spec, mode, self.sink)
                if not await self._claim(ctx, run_id):
                    report.already_fired += 1
                    continue
                try:
                    result = await ACTIONS.get(spec.type)().run(ctx)
                except Exception as exc:
                    result = ActionResult(ActionStatus.FAILED, detail=repr(exc))
                await self._finish(ctx.idempotency_key, result)
                report.results.append(result)
        return report

    async def _claim(self, ctx: ActionContext, run_id: UUID) -> bool:
        key = ctx.idempotency_key
        async with self.sessions() as session, session.begin():
            taken = await session.scalar(
                select(ActionRow.id).where(ActionRow.idempotency_key == key)
            )
            if taken is not None:
                return False
            session.add(
                ActionRow(
                    ts=_now(),
                    rule_run_id=run_id,
                    type=ctx.spec.type,
                    params=ctx.spec.model_dump(exclude={"type"}),
                    idempotency_key=key,
                    status=ActionStatus.PENDING,
                )
            )
        return True

    async def _finish(self, key: str, result: ActionResult) -> None:
        async with self.sessions() as session, session.begin():
            rows = await session.execute(select(ActionRow).where(ActionRow.idempotency_key == key))
            row = rows.scalar_one()
            row.status = result.status
            row.external_ref = result.external_ref
            row.executed_at = _now()
            row.error = result.detail if result.status == ActionStatus.FAILED else ""
