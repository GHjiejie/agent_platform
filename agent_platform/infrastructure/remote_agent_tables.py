"""Remote registration metadata and separately protected credential material."""

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

from .db import metadata


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
