"""Remote registration metadata and separately protected credential material."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import (
    JSON,
    Boolean,
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

revision = "0017_remote_agent_registry"
down_revision = "0016_member_task_leases"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
Table(
    "platform_agents",
    metadata,
    Column("tenant_id", String(64)),
    Column("id", String(64)),
    UniqueConstraint("tenant_id", "id"),
)


def resource_columns():
    return (
        Column("id", String(64), primary_key=True),
        Column("tenant_id", String(64), nullable=False),
        Column("created_at", String(40), nullable=False),
        UniqueConstraint("tenant_id", "id"),
    )


credentials = Table(
    "remote_agent_credentials",
    metadata,
    *resource_columns(),
    Column("name", String(120), nullable=False),
    Column("active", Boolean, nullable=False),
)
secrets = Table(
    "remote_agent_credential_secrets",
    metadata,
    *resource_columns(),
    Column("ciphertext", Text, nullable=False),
    Column("digest", String(64), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "id"],
        ["remote_agent_credentials.tenant_id", "remote_agent_credentials.id"],
    ),
)
versions = Table(
    "remote_agent_versions",
    metadata,
    *resource_columns(),
    Column("agent_id", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("descriptor", JSON, nullable=False),
    Column("idempotency_key", String(64), nullable=False),
    Column("request_digest", String(64), nullable=False),
    UniqueConstraint("tenant_id", "idempotency_key"),
    Column("digest", String(64), nullable=False),
    Column("enabled", Boolean, nullable=False),
    UniqueConstraint("tenant_id", "agent_id", "version"),
    ForeignKeyConstraint(
        ["tenant_id", "agent_id"], ["platform_agents.tenant_id", "platform_agents.id"]
    ),
)
endpoints = Table(
    "remote_agent_endpoints",
    metadata,
    *resource_columns(),
    Column("registration_id", String(64), nullable=False),
    Column("credential_id", String(64), nullable=False),
    Column("url", String(2048), nullable=False),
    Column("health_status", String(24), nullable=False),
    Column("health_until", Float, nullable=False),
    Column("health_error", String(64), nullable=False),
    Column("probe_owner", String(64)),
    Column("probe_fence", Integer, nullable=False),
    Column("probe_until", Float),
    Column("next_probe_at", Float, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "registration_id"],
        ["remote_agent_versions.tenant_id", "remote_agent_versions.id"],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "credential_id"],
        ["remote_agent_credentials.tenant_id", "remote_agent_credentials.id"],
    ),
)
REMOTE_TABLES = (credentials, secrets, versions, endpoints)
for table in REMOTE_TABLES:
    Index(f"ix_{table.name}_tenant", table.c.tenant_id, table.c.id)


REMOTE_PRIVILEGES = {
    "api": {
        "remote_agent_credentials": ("INSERT", "SELECT", "UPDATE"),
        "remote_agent_credential_secrets": ("INSERT",),
        "remote_agent_versions": ("INSERT", "SELECT", "UPDATE"),
        "remote_agent_endpoints": ("INSERT", "SELECT", "UPDATE"),
    },
    "worker": {
        "remote_agent_credentials": ("SELECT",),
        "remote_agent_credential_secrets": ("SELECT",),
        "remote_agent_versions": ("SELECT",),
        "remote_agent_endpoints": ("SELECT", "UPDATE"),
    },
    "maintenance": {table.name: ("SELECT", "UPDATE") for table in REMOTE_TABLES},
}


def upgrade():
    op.add_column("platform_agents", Column("remote_ref", JSON))
    for table in REMOTE_TABLES:
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
        for service, grants in REMOTE_PRIVILEGES.items():
            if name not in grants:
                continue
            role = "platform_" + service
            if op.get_bind().scalar(
                sa.text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
            ):
                op.execute(
                    sa.text(f'GRANT {", ".join(grants[name])} ON "{name}" TO "{role}"')
                )


def downgrade():
    raise RuntimeError(
        "Remote execution identities and credentials require a reviewed backup rollback."
    )
