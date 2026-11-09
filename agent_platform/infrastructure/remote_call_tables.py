"""Remote call identity and bounded reconciliation; private payloads stay in journal."""

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

from .db import metadata

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
