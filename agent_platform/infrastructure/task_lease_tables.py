"""Independent member leases, always subordinate to a live root generation."""

from sqlalchemy import (
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
)

from .db import metadata

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
