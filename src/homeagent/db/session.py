from pathlib import Path

from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command


def make_engine(db_path: Path) -> AsyncEngine:
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}")


def make_sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def upgrade_db(ini_path: Path = Path("alembic.ini")) -> None:
    command.upgrade(Config(str(ini_path)), "head")
