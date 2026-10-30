"""Independent member leases, always subordinate to a live root generation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import (
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
)

revision = "0016_member_task_leases"
down_revision = "0015_execution_checkpoints"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
Table(
    "agent_tasks",
    metadata,
    Column("tenant_id", String(64)),
    Column("root_run_id", String(64)),
    Column("id", String(64)),
    sa.UniqueConstraint("tenant_id", "root_run_id", "id"),
)
Table(
    "agent_executions",
    metadata,
    Column("tenant_id", String(64)),
    Column("root_run_id", String(64)),
    Column("task_id", String(64)),
    Column("id", String(64)),
    sa.UniqueConstraint("tenant_id", "root_run_id", "task_id", "id"),
)

leases = Table(
    "agent_task_leases",
    metadata,
    Column("task_id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("root_run_id", String(64), nullable=False),
    Column("root_fence", Integer, nullable=False),
    Column("owner", String(64)),
    Column("fence", Integer, nullable=False),
    Column("lease_until", Float),
    Column("deadline", Float),
    Column("ready_at", Float, nullable=False),
    Column("recovery_count", Integer, nullable=False, server_default="0"),
    Column("execution_id", String(64)),
    ForeignKeyConstraint(
        ["tenant_id", "root_run_id", "task_id"],
        ["agent_tasks.tenant_id", "agent_tasks.root_run_id", "agent_tasks.id"],
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
)
Index("ix_agent_task_leases_root", leases.c.tenant_id, leases.c.root_run_id)


LEASE_PRIVILEGES = {
    "worker": ("INSERT", "SELECT", "UPDATE"),
    "maintenance": ("SELECT",),
}


def upgrade():
    leases.create(op.get_bind())
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text("ALTER TABLE agent_task_leases ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE agent_task_leases FORCE ROW LEVEL SECURITY"))
    expression = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON agent_task_leases USING ({expression}) WITH CHECK ({expression})"
        )
    )
    for service, grants in LEASE_PRIVILEGES.items():
        role = "platform_" + service
        if op.get_bind().scalar(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
        ):
            op.execute(
                sa.text(f'GRANT {", ".join(grants)} ON agent_task_leases TO "{role}"')
            )


def downgrade():
    raise RuntimeError(
        "Drain distributed tasks and restore a reviewed backup before rollback."
    )
