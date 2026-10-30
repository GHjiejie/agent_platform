"""Private execution evidence, separate from usage receipts and public events."""

from sqlalchemy import (
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from .db import metadata

journal = Table(
    "execution_journal",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("root_run_id", String(64), nullable=False),
    Column("execution_id", String(64), nullable=False),
    Column("operation_key", String(160), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("effect", String(16), nullable=False),
    Column("state", String(24), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("request_ciphertext", Text, nullable=False),
    Column("result_digest", String(64)),
    Column("result_ciphertext", Text),
    Column("call_id", String(64)),
    Column("attempt_id", String(64)),
    Column("idempotency_key", String(64), nullable=False),
    Column("owner", String(64), nullable=False),
    Column("fence", Integer, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("sent_at", Float),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("tenant_id", "root_run_id", "execution_id", "operation_key"),
    UniqueConstraint("tenant_id", "call_id"),
    UniqueConstraint("tenant_id", "idempotency_key"),
    ForeignKeyConstraint(
        ["tenant_id", "root_run_id", "execution_id"],
        [
            "agent_executions.tenant_id",
            "agent_executions.root_run_id",
            "agent_executions.id",
        ],
    ),
)
Index(
    "ix_execution_journal_run", journal.c.tenant_id, journal.c.root_run_id, journal.c.id
)
JOURNAL_TABLES = (journal,)
