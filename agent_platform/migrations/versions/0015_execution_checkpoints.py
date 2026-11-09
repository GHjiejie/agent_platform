"""Execution-scoped encrypted LangGraph state; not an authorization mechanism."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)

revision = "0015_execution_checkpoints"
down_revision = "0014_execution_journal"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
Table(
    "agent_executions",
    metadata,
    Column("tenant_id", String(64)),
    Column("root_run_id", String(64)),
    Column("id", String(64)),
    UniqueConstraint("tenant_id", "root_run_id", "id"),
)


def scope_columns():
    return (
        Column("id", String(64), primary_key=True),
        Column("tenant_id", String(64), nullable=False),
        Column("root_run_id", String(64), nullable=False),
        Column("execution_id", String(64), nullable=False),
        Column("phase", String(24), nullable=False),
        Column("checkpoint_id", String(64), nullable=False),
        Column("payload_ciphertext", Text, nullable=False),
        Column("payload_digest", String(64), nullable=False),
        Column("created_at", String(40), nullable=False),
        ForeignKeyConstraint(
            ["tenant_id", "root_run_id", "execution_id"],
            [
                "agent_executions.tenant_id",
                "agent_executions.root_run_id",
                "agent_executions.id",
            ],
        ),
    )


checkpoints = Table(
    "execution_checkpoints",
    metadata,
    *scope_columns(),
    Column("parent_checkpoint_id", String(64)),
    UniqueConstraint(
        "tenant_id", "root_run_id", "execution_id", "phase", "checkpoint_id"
    ),
)
writes = Table(
    "execution_checkpoint_writes",
    metadata,
    *scope_columns(),
    Column("graph_task_id", String(64), nullable=False),
    Column("write_index", Integer, nullable=False),
    Column("channel", String(160), nullable=False),
    UniqueConstraint(
        "tenant_id",
        "root_run_id",
        "execution_id",
        "phase",
        "checkpoint_id",
        "graph_task_id",
        "write_index",
    ),
)
CHECKPOINT_TABLES = (checkpoints, writes)
for table in CHECKPOINT_TABLES:
    Index(
        f"ix_{table.name}_execution",
        table.c.tenant_id,
        table.c.execution_id,
        table.c.phase,
        table.c.checkpoint_id,
    )


CHECKPOINT_PRIVILEGES = {
    "worker": ("INSERT", "SELECT", "UPDATE"),
    "maintenance": ("SELECT", "UPDATE"),
}


def upgrade():
    op.add_column(
        "platform_runs",
        Column("recovery_count", Integer, nullable=False, server_default="0"),
    )
    for table in CHECKPOINT_TABLES:
        table.create(op.get_bind())
        if op.get_bind().dialect.name != "postgresql":
            continue
        name = table.name
        op.execute(sa.text(f'ALTER TABLE "{name}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{name}" FORCE ROW LEVEL SECURITY'))
        expression = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
        op.execute(
            sa.text(
                f'CREATE POLICY tenant_isolation ON "{name}" USING ({expression}) WITH CHECK ({expression})'
            )
        )
        for service, grants in CHECKPOINT_PRIVILEGES.items():
            role, privileges = "platform_" + service, ", ".join(grants)
            if op.get_bind().scalar(
                sa.text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
            ):
                op.execute(sa.text(f'GRANT {privileges} ON "{name}" TO "{role}"'))


def downgrade():
    raise RuntimeError(
        "Private execution history must be retained; drain runs and restore a backup."
    )
