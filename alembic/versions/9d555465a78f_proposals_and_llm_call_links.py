import sqlalchemy as sa

from alembic import op

revision = "9d555465a78f"
down_revision = "f65b2ffb250b"
branch_labels = None
depends_on = None

FK_NAME = "fk_llm_calls_raw_event_id"


def upgrade() -> None:
    op.create_table(
        "proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.Integer(), nullable=False),
        sa.Column("raw_event_id", sa.Uuid(), nullable=False),
        sa.Column("llm_call_id", sa.Uuid(), nullable=False),
        sa.Column("need", sa.String(length=16), nullable=False),
        sa.Column("due", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("message_ref", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decided_at", sa.Integer(), nullable=True),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["llm_call_id"], ["llm_calls.id"]),
        sa.ForeignKeyConstraint(["raw_event_id"], ["raw_events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("raw_event_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("prompt_version", sa.String(length=16), nullable=False, server_default="")
        )
        batch_op.create_foreign_key(FK_NAME, "raw_events", ["raw_event_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.drop_constraint(FK_NAME, type_="foreignkey")
        batch_op.drop_column("prompt_version")
        batch_op.drop_column("raw_event_id")
    op.drop_table("proposals")
