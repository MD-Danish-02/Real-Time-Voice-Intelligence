"""add pages_processed to crm_sync_jobs
Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('crm_sync_jobs', sa.Column('pages_processed', sa.Integer(), nullable=False, server_default='0'))
    op.alter_column('crm_sync_jobs', 'pages_processed', server_default=None)

def downgrade():
    op.drop_column('crm_sync_jobs', 'pages_processed')
