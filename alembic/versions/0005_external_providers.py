"""add external provider integration records

Revision ID: 0005_external_providers
Revises: 0004_realtime
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_external_providers"
down_revision = "0004_realtime"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "payment_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False, server_default="razorpay"),
        sa.Column("provider_order_id", sa.String(120), nullable=False, unique=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(30), nullable=False, server_default="created"),
        sa.Column("receipt", sa.String(120), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_payment_orders_user_id", "payment_orders", ["user_id"])

    op.create_table(
        "code_executions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False, server_default="judge0"),
        sa.Column("provider_submission_id", sa.String(120), nullable=True),
        sa.Column("language_id", sa.Integer(), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("stdin", sa.Text(), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="queued"),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("compile_output", sa.Text(), nullable=True),
        sa.Column("time", sa.String(50), nullable=True),
        sa.Column("memory", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_code_executions_user_id", "code_executions", ["user_id"])
    op.create_index("ix_code_executions_provider_submission_id", "code_executions", ["provider_submission_id"])

    op.create_table(
        "livekit_token_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("room_name", sa.String(120), nullable=False),
        sa.Column("identity", sa.String(120), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_livekit_token_audits_user_id", "livekit_token_audits", ["user_id"])

def downgrade():
    op.drop_index("ix_livekit_token_audits_user_id", table_name="livekit_token_audits")
    op.drop_table("livekit_token_audits")
    op.drop_index("ix_code_executions_provider_submission_id", table_name="code_executions")
    op.drop_index("ix_code_executions_user_id", table_name="code_executions")
    op.drop_table("code_executions")
    op.drop_index("ix_payment_orders_user_id", table_name="payment_orders")
    op.drop_table("payment_orders")
