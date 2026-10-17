"""persist user-managed DNC independently from CRM status

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "lead_call_state",
        sa.Column("manual_do_not_call", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("lead_call_state", "manual_do_not_call", server_default=None)


def downgrade():
    op.drop_column("lead_call_state", "manual_do_not_call")
