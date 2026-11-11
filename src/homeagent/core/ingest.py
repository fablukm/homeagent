import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from homeagent.core.events import Event
from homeagent.db.models import Cursor, RawEvent
from homeagent.sources.base import Source, Upstream

CURSOR_KEY = "poll"
MAX_ATTEMPTS = 3
STATUS_OK = "ok"
STATUS_QUARANTINED = "quarantined"
STATUS_DUPLICATE = "duplicate"


def payload_hash(raw: Upstream) -> str:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode()).hexdigest()


@dataclass
class IngestReport:
    source: str
    events: list[Event] = field(default_factory=list[Event])
    duplicates: int = 0
    quarantined: int = 0
    cursor: str | None = None


class Ingestor:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def run(self, source: Source) -> IngestReport:
        report = IngestReport(source.name)
        async with self.sessions() as session, session.begin():
            cursor = await session.get(Cursor, (source.name, CURSOR_KEY))
            raws, next_cursor = await source.poll(cursor.value if cursor else None)
            seen = await self._seen_hashes(session, source.name)
            now = int(datetime.now(UTC).timestamp())
            for raw in raws:
                digest = payload_hash(raw)
                if digest in seen:
                    report.duplicates += 1
                    continue
                seen.add(digest)
                row = RawEvent(
                    source=source.name,
                    external_id=source.external_id(raw),
                    occurred_at=now,
                    ingested_at=now,
                    payload=raw,
                    payload_hash=digest,
                )
                try:
                    event = source.normalise(raw)
                except Exception as exc:
                    row.status, row.error, row.attempts = STATUS_QUARANTINED, repr(exc), 1
                    report.quarantined += 1
                else:
                    if event is not None:
                        row.id = event.id
                        row.occurred_at = int(event.occurred_at.timestamp())
                        report.events.append(event)
                session.add(row)
            if next_cursor is not None:
                await session.merge(Cursor(source=source.name, key=CURSOR_KEY, value=next_cursor))
            report.cursor = next_cursor
        return report

    @staticmethod
    async def _seen_hashes(session: AsyncSession, source_name: str) -> set[str]:
        rows = await session.execute(
            select(RawEvent.payload_hash).where(RawEvent.source == source_name)
        )
        return set(rows.scalars())
