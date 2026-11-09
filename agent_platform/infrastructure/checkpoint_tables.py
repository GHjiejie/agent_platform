"""Execution-scoped encrypted LangGraph state; not an authorization mechanism."""

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

from .db import metadata


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
