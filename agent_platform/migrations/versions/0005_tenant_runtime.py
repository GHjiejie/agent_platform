"""Runtime routing evidence, SSE budgets, composite references and tenant RLS.

Runtime connections must be non-owner/NOBYPASSRLS. Global identity/control
metadata stays outside content policies because authentication must enumerate
memberships before a tenant is selected. Platform access still selects one
explicit tenant; there is no application GUC for bypassing RLS.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_tenant_runtime"
down_revision = "0004"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "platform_models",
    "platform_agents",
    "platform_agent_versions",
    "platform_sessions",
    "platform_runs",
    "platform_messages",
    "platform_run_events",
    "platform_calls",
    "platform_audits",
    "billing_wallets",
    "billing_quotas",
    "billing_reservations",
    "billing_transactions",
    "billing_entries",
    "billing_audit",
    "billing_reconciliations",
    "tenant_model_bindings",
    "model_price_versions",
    "gateway_bindings",
    "gateway_credential_versions",
    "gateway_outbox",
    "gateway_control_audits",
    "platform_call_attempts",
    "platform_stream_leases",
)


def upgrade():
    op.create_table(
        "platform_service_heartbeats",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_index(
        "ix_heartbeat_role_expiry",
        "platform_service_heartbeats",
        ["role", "expires_at"],
    )
    op.create_table(
        "platform_call_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("membership_id", sa.String(64)),
        sa.Column("deployment_id", sa.String(64), nullable=False),
        sa.Column("credential_version_id", sa.String(64)),
        sa.Column("price_version_id", sa.String(64)),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("route", sa.String(200), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["platform_runs.tenant_id", "platform_runs.id"]
        ),
    )
    op.create_index(
        "ix_attempt_tenant_run", "platform_call_attempts", ["tenant_id", "run_id"]
    )
    op.create_table(
        "platform_stream_leases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["platform_runs.tenant_id", "platform_runs.id"]
        ),
    )
    op.create_index(
        "ix_stream_tenant_expiry", "platform_stream_leases", ["tenant_id", "expires_at"]
    )
    with op.batch_alter_table("platform_messages") as batch:
        batch.create_foreign_key(
            "fk_messages_session",
            "platform_sessions",
            ["tenant_id", "session_id"],
            ["tenant_id", "id"],
        )
        batch.create_foreign_key(
            "fk_messages_run",
            "platform_runs",
            ["tenant_id", "run_id"],
            ["tenant_id", "id"],
        )
    with op.batch_alter_table("platform_calls") as batch:
        batch.create_foreign_key(
            "fk_calls_run",
            "platform_runs",
            ["tenant_id", "run_id"],
            ["tenant_id", "id"],
        )
        batch.create_foreign_key(
            "fk_calls_model",
            "platform_models",
            ["tenant_id", "model_id"],
            ["tenant_id", "id"],
        )
    op.create_index(
        "ix_runs_tenant_status_created",
        "platform_runs",
        ["tenant_id", "status", "created_at", "id"],
    )
    op.create_index(
        "ix_reservations_tenant_status",
        "billing_reservations",
        ["tenant_id", "status", "updated_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        for table in TENANT_TABLES:
            op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
            expression = (
                "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
            )
            op.execute(
                sa.text(
                    f'CREATE POLICY tenant_isolation ON "{table}" USING ({expression}) WITH CHECK ({expression})'
                )
            )


def downgrade():
    raise RuntimeError(
        "Tenant isolation cannot be downgraded automatically; restore a drained, approved backup."
    )
