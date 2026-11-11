from sqlalchemy import engine_from_config, pool

from alembic import context
from homeagent.db.models import Base

config = context.config
target_metadata = Base.metadata


def run() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


run()
