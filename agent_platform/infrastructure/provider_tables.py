"""Platform-managed provider connections; only gateway-sync can read secrets."""

from sqlalchemy import (
    JSON,
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
)

from agent_platform.infrastructure.db import metadata

providers = Table(
    "model_providers",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("api_base", String(500), nullable=False),
    Column("catalog", JSON, nullable=False),
    Column("catalog_updated_at", String(40)),
    Column("created_at", String(40), nullable=False),
)
secrets = Table(
    "model_provider_secrets",
    metadata,
    Column("provider_id", String(64), primary_key=True),
    Column("ciphertext", Text, nullable=False),
    ForeignKeyConstraint(["provider_id"], ["model_providers.id"]),
)
operations = Table(
    "model_provider_operations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("provider_id", String(64), nullable=False),
    Column("requested_by", String(64), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("dedupe_key", String(128), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
    Column("result", JSON, nullable=False),
    Column("status", String(24), nullable=False),
    Column("error_code", String(80), nullable=False),
    Column("sent_at", Float),
    Column("owner", String(64)),
    Column("fence", Integer, nullable=False),
    Column("lease_until", Float),
    Column("attempts", Integer, nullable=False),
    Column("next_poll_at", Float, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    ForeignKeyConstraint(["provider_id"], ["model_providers.id"]),
)
Index("ix_model_provider_queue", operations.c.status, operations.c.next_poll_at)

PROVIDER_PRIVILEGES = {
    "api": {
        providers.name: ("SELECT", "INSERT"),
        secrets.name: ("INSERT",),
        operations.name: ("SELECT", "INSERT"),
    },
    "gateway-sync": {
        providers.name: ("SELECT", "UPDATE"),
        secrets.name: ("SELECT",),
        operations.name: ("SELECT", "UPDATE"),
    },
}
