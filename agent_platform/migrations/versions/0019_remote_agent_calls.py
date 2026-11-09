"""Remote call identity and bounded reconciliation; private payloads stay in journal."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    false,
)

revision = "0019_remote_agent_calls"
down_revision = "0018_fixed_remote_charges"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
for name, columns in (
    ("execution_journal", ("tenant_id", "call_id")),
    ("agent_executions", ("tenant_id", "root_run_id", "task_id", "id")),
    ("remote_agent_versions", ("tenant_id", "id")),
    ("remote_agent_endpoints", ("tenant_id", "id")),
):
    Table(
        name,
        metadata,
        *(Column(key, String(64)) for key in columns),
        sa.UniqueConstraint(*columns),
    )


calls = Table(
    "remote_agent_calls",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("root_run_id", String(64), nullable=False),
    Column("task_id", String(64), nullable=False),
    Column("execution_id", String(64), nullable=False),
    Column("registration_id", String(64), nullable=False),
    Column("endpoint_id", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("remote_state", String(24), nullable=False),
    Column("deadline", Float, nullable=False),
    Column("poll_count", Integer, nullable=False, server_default="0"),
    Column("cancel_due", Boolean, nullable=False, server_default=false()),
    Column("cancel_sent", Boolean, nullable=False, server_default=false()),
    Column("reconciliation", String(24), nullable=False, server_default="pending"),
    Column("next_poll_at", Float, nullable=False, server_default="0"),
    Column("reconcile_count", Integer, nullable=False, server_default="0"),
    Column("owner", String(64)),
    Column("fence", Integer, nullable=False, server_default="0"),
    Column("lease_until", Float),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "id"],
        ["execution_journal.tenant_id", "execution_journal.call_id"],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "root_run_id", "task_id", "execution_id"],
        [
            "agent_executions.tenant_id",
            "agent_executions.root_run_id",
            "agent_executions.task_id",
            "agent_executions.id",
        ],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "registration_id"],
        ["remote_agent_versions.tenant_id", "remote_agent_versions.id"],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "endpoint_id"],
        ["remote_agent_endpoints.tenant_id", "remote_agent_endpoints.id"],
    ),
)
Index("ix_remote_agent_calls_run", calls.c.tenant_id, calls.c.root_run_id, calls.c.id)
Index(
    "ix_remote_agent_calls_reconcile",
    calls.c.tenant_id,
    calls.c.reconciliation,
    calls.c.next_poll_at,
)

CALL_PRIVILEGES = {
    "api": ("SELECT", "UPDATE"),
    "worker": ("INSERT", "SELECT", "UPDATE"),
    "maintenance": ("SELECT",),
}


def upgrade():
    calls.create(op.get_bind())
    if op.get_bind().dialect.name != "postgresql":
        return
    name = calls.name
    op.execute(sa.text(f'ALTER TABLE "{name}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{name}" FORCE ROW LEVEL SECURITY'))
    expression = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
    op.execute(
        sa.text(
            f'CREATE POLICY tenant_isolation ON "{name}" USING ({expression}) WITH CHECK ({expression})'
        )
    )
    for service, grants in CALL_PRIVILEGES.items():
        role = "platform_" + service
        if op.get_bind().scalar(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
        ):
            op.execute(sa.text(f'GRANT {", ".join(grants)} ON "{name}" TO "{role}"'))


def downgrade():
    raise RuntimeError("Remote call identities require a reviewed backup rollback.")
