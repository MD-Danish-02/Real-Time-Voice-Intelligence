"""persist normalized custom lead fields

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("leads", sa.Column("custom_fields_json", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("leads", "custom_fields_json")
