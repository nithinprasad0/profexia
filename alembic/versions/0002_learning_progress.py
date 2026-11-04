"""add enrollment progress fields

Revision ID: 0002_learning_progress
Revises: 0001_core_domain
"""
from alembic import op
import sqlalchemy as sa
revision = "0002_learning_progress"
down_revision = "0001_core_domain"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("enrollments", sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("enrollments", sa.Column("status", sa.String(20), nullable=False, server_default="active"))
    op.alter_column("enrollments", "progress_percent", server_default=None)
    op.alter_column("enrollments", "status", server_default=None)

def downgrade():
    op.drop_column("enrollments", "status")
    op.drop_column("enrollments", "progress_percent")
