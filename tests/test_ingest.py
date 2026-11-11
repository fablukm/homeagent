from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from homeagent.core.events import Event, SourceKind
from homeagent.core.ingest import STATUS_QUARANTINED, Ingestor
from homeagent.db.models import Base, Cursor, RawEvent
from homeagent.db.session import make_engine, make_sessions
from homeagent.sources.base import Source, Upstream


class FakeMail(Source):
    name = "fake"

    def __init__(self, batches: list[list[Upstream]]) -> None:
        self.batches = batches
        self.polled_with: list[str | None] = []

    async def poll(self, cursor: str | None) -> tuple[list[Upstream], str | None]:
        self.polled_with.append(cursor)
        index = int(cursor) if cursor else 0
        batch = self.batches[index] if index < len(self.batches) else []
        return batch, str(index + 1)

    def normalise(self, raw: Upstream) -> Event | None:
        if "text" not in raw:
            raise ValueError("no text")
        return Event(
            source=SourceKind.MAIL,
            source_name=self.name,
            external_id=str(raw["id"]),
            occurred_at=datetime(2026, 11, 20, 18, tzinfo=UTC),
            title=str(raw["folder"]),
            body=str(raw["text"]),
        )


async def _ingestor(tmp_path: Path) -> Ingestor:
    engine = make_engine(tmp_path / "t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return Ingestor(make_sessions(engine))


async def test_dedupe_quarantine_and_cursor(tmp_path: Path) -> None:
    good = {"id": 1, "folder": "inbox", "text": "Rechnung Nr. 42"}
    bad = {"id": 2, "folder": "inbox"}
    source = FakeMail(
        [[good, bad], [good, {"id": 3, "folder": "inbox", "text": "Kita Elternabend"}]]
    )
    ingestor = await _ingestor(tmp_path)

    first = await ingestor.run(source)
    assert [e.body for e in first.events] == ["Rechnung Nr. 42"]
    assert first.quarantined == 1 and first.cursor == "1"

    second = await ingestor.run(source)
    assert [e.body for e in second.events] == ["Kita Elternabend"]
    assert second.duplicates == 1
    assert source.polled_with == [None, "1"]

    async with ingestor.sessions() as session:
        rows = (
            (await session.execute(select(RawEvent).order_by(RawEvent.external_id))).scalars().all()
        )
        assert [r.status for r in rows] == ["ok", STATUS_QUARANTINED, "ok"]
        assert rows[1].error.startswith("ValueError")
        cursor = await session.get(Cursor, ("fake", "poll"))
        assert cursor is not None and cursor.value == "2"


class BrokenPoll(FakeMail):
    async def poll(self, cursor: str | None) -> tuple[list[Upstream], str | None]:
        if cursor:
            raise ConnectionError("upstream down")
        return await super().poll(cursor)


async def _rows(ingestor: Ingestor) -> list[RawEvent]:
    async with ingestor.sessions() as session:
        return list((await session.execute(select(RawEvent))).scalars().all())


async def test_replay_from_lost_cursor_stores_nothing_twice(tmp_path: Path) -> None:
    batch = [
        {"id": 1, "folder": "inbox", "text": "Rechnung Nr. 42"},
        {"id": 2, "folder": "inbox"},
    ]
    source = FakeMail([batch])
    ingestor = await _ingestor(tmp_path)
    await ingestor.run(source)
    before = await _rows(ingestor)
    async with ingestor.sessions() as session, session.begin():
        await session.delete(await session.get(Cursor, ("fake", "poll")))

    replay = await ingestor.run(source)

    assert replay.events == []
    assert replay.duplicates == len(batch)
    assert replay.quarantined == 0
    assert source.polled_with == [None, None]
    after = await _rows(ingestor)
    assert {r.id for r in after} == {r.id for r in before}


async def test_failed_poll_keeps_cursor_and_rows(tmp_path: Path) -> None:
    source = BrokenPoll([[{"id": 1, "folder": "inbox", "text": "Kita Elternabend"}]])
    ingestor = await _ingestor(tmp_path)
    await ingestor.run(source)

    with pytest.raises(ConnectionError):
        await ingestor.run(source)

    assert len(await _rows(ingestor)) == 1
    async with ingestor.sessions() as session:
        cursor = await session.get(Cursor, ("fake", "poll"))
        assert cursor is not None and cursor.value == "1"
