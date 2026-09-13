"""Create durable workspace assistant conversations."""

import sqlalchemy as sa
from alembic import op


revision = "20260912_0004"
down_revision = "20260912_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), sa.ForeignKey("platform_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("page", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index("assistant_session_owner", "assistant_sessions", ["workspace_id", "actor", "updated_at"])
    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("session_id", sa.Text(), sa.ForeignKey("assistant_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page", sa.Text(), nullable=False),
        sa.Column("sources", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("assistant_message_session", "assistant_messages", ["session_id", "created_at"])


def downgrade() -> None:
    op.drop_table("assistant_messages")
    op.drop_table("assistant_sessions")
