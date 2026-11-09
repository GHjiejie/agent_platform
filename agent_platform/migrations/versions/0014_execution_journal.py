"""Private execution evidence, separate from usage receipts and public events."""

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
    Text,
    UniqueConstraint,
)

revision = "0014_execution_journal"
down_revision = "0013_collaboration_outcome"
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


JOURNAL_PRIVILEGES = {
    "worker": ("INSERT", "SELECT", "UPDATE"),
    "maintenance": ("SELECT", "UPDATE"),
}


def upgrade():
    with op.batch_alter_table("collaboration_artifacts") as batch:
        batch.add_column(Column("source_key", String(160)))
        batch.create_unique_constraint(
            "uq_artifact_execution_source",
            ["tenant_id", "root_run_id", "execution_id", "source_key"],
        )
    for table in JOURNAL_TABLES:
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
        for service, grants in JOURNAL_PRIVILEGES.items():
            role, privileges = "platform_" + service, ", ".join(grants)
            if op.get_bind().scalar(
                sa.text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
            ):
                op.execute(sa.text(f'GRANT {privileges} ON "{name}" TO "{role}"'))


def downgrade():
    raise RuntimeError(
        "Execution evidence must be retained; drain runs and restore a backup."
    )
