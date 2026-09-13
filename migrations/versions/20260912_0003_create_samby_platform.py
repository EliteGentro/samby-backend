"""Create the active Samby platform and analytical resource schema."""

import sqlalchemy as sa
from alembic import op


revision = "20260912_0003"
down_revision = "20260911_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "namespaces",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("mode", sa.Text(), nullable=True),
    )
    op.create_table(
        "definitions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index("definition_namespace", "definitions", ["namespace", "kind", "archived"])
    op.create_table(
        "snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("definition_id", sa.Text(), nullable=False),
        sa.Column("definition_name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), sa.ForeignKey("snapshots.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("warnings", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_of_run_id", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.Text(), nullable=True),
        sa.Column("archived", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("run_namespace", "runs", ["namespace", "kind", "status", "archived"])
    op.create_table(
        "idempotency",
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("namespace", "key"),
    )
    op.create_table(
        "artifacts",
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("result", sa.Text(), nullable=False),
    )
    op.create_table(
        "transitions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("previous_status", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_table(
        "platform_users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "platform_sessions",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("platform_users.id"), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "platform_workspaces",
        sa.Column("id", sa.Text(), sa.ForeignKey("namespaces.id"), primary_key=True),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("guest_key_hash", sa.Text(), nullable=True),
        sa.Column("archived", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "platform_memberships",
        sa.Column("workspace_id", sa.Text(), sa.ForeignKey("platform_workspaces.id"), nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("platform_users.id"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
    )
    op.create_table(
        "platform_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("at", sa.Text(), nullable=False),
    )
    op.create_table(
        "platform_login_failures",
        sa.Column("identity", sa.Text(), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.Text(), nullable=False),
    )

    op.execute("""
        CREATE FUNCTION samby_reject_immutable_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% are immutable', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
    """)
    for name, table, action in (
        ("immutable_snapshot_update", "snapshots", "UPDATE"),
        ("immutable_snapshot_delete", "snapshots", "DELETE"),
        ("immutable_artifact_update", "artifacts", "UPDATE"),
        ("immutable_artifact_delete", "artifacts", "DELETE"),
    ):
        op.execute(
            f"CREATE TRIGGER {name} BEFORE {action} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION samby_reject_immutable_change()"
        )
    op.execute("""
        CREATE FUNCTION samby_reject_run_input_change() RETURNS trigger AS $$
        BEGIN
            IF NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id
               OR NEW.namespace IS DISTINCT FROM OLD.namespace
               OR NEW.definition_id IS DISTINCT FROM OLD.definition_id
               OR NEW.definition_name IS DISTINCT FROM OLD.definition_name
               OR NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.retry_of_run_id IS DISTINCT FROM OLD.retry_of_run_id
               OR NEW.provenance IS DISTINCT FROM OLD.provenance THEN
                RAISE EXCEPTION 'Submitted run identity and inputs are immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER immutable_run_inputs BEFORE UPDATE ON runs
        FOR EACH ROW EXECUTE FUNCTION samby_reject_run_input_change()
    """)
    op.execute("""
        CREATE FUNCTION samby_reject_terminal_status_change() RETURNS trigger AS $$
        BEGIN
            IF OLD.status IN ('succeeded', 'failed', 'cancelled')
               AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION 'Terminal run state is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER terminal_run_status BEFORE UPDATE OF status ON runs
        FOR EACH ROW EXECUTE FUNCTION samby_reject_terminal_status_change()
    """)


def downgrade() -> None:
    for table in (
        "platform_login_failures", "platform_audit", "platform_memberships",
        "platform_workspaces", "platform_sessions", "platform_users", "transitions",
        "artifacts", "idempotency", "runs", "snapshots", "definitions", "namespaces",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION samby_reject_terminal_status_change()")
    op.execute("DROP FUNCTION samby_reject_run_input_change()")
    op.execute("DROP FUNCTION samby_reject_immutable_change()")
