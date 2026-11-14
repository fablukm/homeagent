import sqlalchemy as sa

from alembic import op

revision = "3b7e1d2c9a40"
down_revision = "9d555465a78f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("unsure", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("deadline", sa.String(length=16), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.drop_column("description")
        batch_op.drop_column("deadline")
        batch_op.drop_column("unsure")
