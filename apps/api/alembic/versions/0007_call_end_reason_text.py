"""Store full call end reasons without varchar truncation.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "calls",
        "end_reason",
        existing_type=sa.String(length=64),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade():
    # PostgreSQL refuses values longer than 64 characters rather than truncating.
    op.alter_column(
        "calls",
        "end_reason",
        existing_type=sa.Text(),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
