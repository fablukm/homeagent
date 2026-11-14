import sqlalchemy as sa

from alembic import op

revision = "5c2f8a1e7b13"
down_revision = "3b7e1d2c9a40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("final_need", sa.String(length=16), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("final_due", sa.String(length=16), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("final_summary", sa.Text(), nullable=False, server_default="")
        )
    op.execute("UPDATE proposals SET final_need = need, final_due = due, final_summary = summary")


def downgrade() -> None:
    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.drop_column("final_summary")
        batch_op.drop_column("final_due")
        batch_op.drop_column("final_need")
