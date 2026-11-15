import sqlalchemy as sa

from alembic import op

revision = "8e4b6c2d1f90"
down_revision = "5c2f8a1e7b13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("task_ref", sa.String(length=256), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.drop_column("task_ref")
