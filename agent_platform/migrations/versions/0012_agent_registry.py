"""Tenant/run-bound collaboration records; attempts never overwrite task identity."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

revision = "0012_agent_registry"
down_revision = "0011_agent_execution_policy"
branch_labels = None
depends_on = None
metadata = MetaData()
Table(
    "platform_runs",
    metadata,
    Column("tenant_id", String(64)),
    Column("id", String(64)),
    UniqueConstraint("tenant_id", "id"),
)
Table(
    "platform_agent_versions",
    metadata,
    Column("tenant_id", String(64)),
    Column("agent_id", String(64)),
    Column("version", Integer),
    UniqueConstraint("tenant_id", "agent_id", "version"),
)


def run_columns():
    return (
        Column("id", String(64), primary_key=True),
        Column("tenant_id", String(64), nullable=False),
        Column("root_run_id", String(64), nullable=False),
        Column("created_at", String(40), nullable=False),
        UniqueConstraint("tenant_id", "root_run_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "root_run_id"],
            ["platform_runs.tenant_id", "platform_runs.id"],
        ),
    )


plans = Table(
    "collaboration_plans",
    metadata,
    *run_columns(),
    Column("revision", Integer, nullable=False),
    Column("plan", JSON, nullable=False),
    Column("digest", String(64), nullable=False),
    UniqueConstraint("tenant_id", "root_run_id", "revision"),
)
tasks = Table(
    "agent_tasks",
    metadata,
    *run_columns(),
    Column("plan_id", String(64), nullable=False),
    Column("logical_key", String(64), nullable=False),
    Column("member_key", String(64), nullable=False),
    Column("agent_id", String(64), nullable=False),
    Column("agent_version", Integer, nullable=False),
    Column("task_type", String(64), nullable=False),
    Column("objective", Text, nullable=False),
    Column("dependencies", JSON, nullable=False),
    Column("required", Boolean, nullable=False),
    Column("status", String(32), nullable=False),
    Column("revision", Integer, nullable=False, default=0),
    Column("error", Text, nullable=False, default=""),
    UniqueConstraint("tenant_id", "root_run_id", "plan_id", "logical_key"),
    ForeignKeyConstraint(
        ["tenant_id", "root_run_id", "plan_id"],
        [
            "collaboration_plans.tenant_id",
            "collaboration_plans.root_run_id",
            "collaboration_plans.id",
        ],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "agent_id", "agent_version"],
        [
            "platform_agent_versions.tenant_id",
            "platform_agent_versions.agent_id",
            "platform_agent_versions.version",
        ],
    ),
)
executions = Table(
    "agent_executions",
    metadata,
    *run_columns(),
    Column("task_id", String(64)),
    Column("attempt", Integer, nullable=False),
    Column("role", String(24), nullable=False),
    Column("agent_id", String(64), nullable=False),
    Column("agent_version", Integer, nullable=False),
    Column("spec", JSON, nullable=False),
    Column("input", JSON, nullable=False),
    Column("input_digest", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("output", JSON),
    Column("error", Text, nullable=False, default=""),
    Column("owner", String(64)),
    Column("fence", Integer, nullable=False),
    Column("finished_at", String(40)),
    UniqueConstraint("tenant_id", "root_run_id", "task_id", "attempt"),
    UniqueConstraint("tenant_id", "root_run_id", "task_id", "id"),
    ForeignKeyConstraint(
        ["tenant_id", "root_run_id", "task_id"],
        ["agent_tasks.tenant_id", "agent_tasks.root_run_id", "agent_tasks.id"],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "agent_id", "agent_version"],
        [
            "platform_agent_versions.tenant_id",
            "platform_agent_versions.agent_id",
            "platform_agent_versions.version",
        ],
    ),
)
artifacts = Table(
    "collaboration_artifacts",
    metadata,
    *run_columns(),
    Column("execution_id", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("kind", String(64), nullable=False),
    Column("content", JSON, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("validation_status", String(24), nullable=False),
    UniqueConstraint("tenant_id", "root_run_id", "execution_id", "kind", "version"),
    ForeignKeyConstraint(
        ["tenant_id", "root_run_id", "execution_id"],
        [
            "agent_executions.tenant_id",
            "agent_executions.root_run_id",
            "agent_executions.id",
        ],
    ),
)
COLLABORATION_TABLES = (plans, tasks, executions, artifacts)
for table in COLLABORATION_TABLES:
    Index(f"ix_{table.name}_run", table.c.tenant_id, table.c.root_run_id, table.c.id)


COLLABORATION_PRIVILEGES = {
    "api": ("SELECT",),
    "worker": ("INSERT", "SELECT", "UPDATE"),
    "maintenance": ("SELECT", "UPDATE"),
}


def upgrade():
    op.add_column(
        "platform_tenant_settings",
        Column(
            "collaboration_enabled", Boolean, nullable=False, server_default=sa.false()
        ),
    )
    for column in (
        Column("execution_mode", String(16), nullable=False, server_default="single"),
        Column("capability_keys", JSON, nullable=False, server_default="[]"),
        Column("task_contracts", JSON, nullable=False, server_default="[]"),
        Column("collaboration_spec", JSON),
        Column("available", Boolean, nullable=False, server_default=sa.true()),
    ):
        op.add_column("platform_agents", column)
    with op.batch_alter_table("platform_agent_versions") as batch:
        batch.create_unique_constraint(
            "uq_agent_versions_tenant_version", ["tenant_id", "agent_id", "version"]
        )
    for table in COLLABORATION_TABLES:
        table.create(op.get_bind())
    with op.batch_alter_table("platform_calls") as batch:
        batch.add_column(Column("task_id", String(64)))
        batch.add_column(Column("execution_id", String(64)))
        batch.create_foreign_key(
            "fk_calls_task",
            "agent_tasks",
            ["tenant_id", "run_id", "task_id"],
            ["tenant_id", "root_run_id", "id"],
        )
        batch.create_foreign_key(
            "fk_calls_execution",
            "agent_executions",
            ["tenant_id", "run_id", "execution_id"],
            ["tenant_id", "root_run_id", "id"],
        )
    if op.get_bind().dialect.name == "postgresql":
        for table in COLLABORATION_TABLES:
            name = table.name
            op.execute(sa.text(f'ALTER TABLE "{name}" ENABLE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'ALTER TABLE "{name}" FORCE ROW LEVEL SECURITY'))
            expression = (
                "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
            )
            op.execute(
                sa.text(
                    f'CREATE POLICY tenant_isolation ON "{name}" USING ({expression}) WITH CHECK ({expression})'
                )
            )
            for service, grants in COLLABORATION_PRIVILEGES.items():
                role, privileges = "platform_" + service, ", ".join(grants)
                if op.get_bind().scalar(
                    sa.text("SELECT 1 FROM pg_roles WHERE rolname=:role"),
                    {"role": role},
                ):
                    op.execute(sa.text(f'GRANT {privileges} ON "{name}" TO "{role}"'))


def downgrade():
    raise RuntimeError(
        "Collaboration history must be retained; drain runs and restore an approved backup."
    )
