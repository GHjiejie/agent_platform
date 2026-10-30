"""Tenant/run-bound collaboration records; attempts never overwrite task identity."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from .db import metadata


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
    Column("source_key", String(160)),
    UniqueConstraint("tenant_id", "root_run_id", "execution_id", "source_key"),
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
