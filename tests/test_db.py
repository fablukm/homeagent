from pathlib import Path

from sqlalchemy import inspect

from homeagent.db.models import Base
from homeagent.db.session import make_engine


async def test_schema_creates_all_tables(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        names = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
    assert {"raw_events", "actions", "llm_calls", "labels", "rule_runs", "mode_log"} <= set(names)
    await engine.dispose()
