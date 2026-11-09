"""Durable provider discovery and gateway import, with write-only API secrets."""

revision = "0020_provider_catalog"
down_revision = "0019_remote_agent_calls"
branch_labels = None
depends_on = None

from alembic import op
from sqlalchemy import (
    JSON,
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text,
)

metadata = MetaData()

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


def upgrade():
    for table in (providers, secrets, operations):
        table.create(op.get_bind())
    if op.get_bind().dialect.name == "postgresql":
        grants = {
            service: dict(tables) for service, tables in PROVIDER_PRIVILEGES.items()
        }
        grants["gateway-sync"].update(
            {
                "model_deployments": ("INSERT", "SELECT"),
                "platform_users": ("SELECT",),
                "platform_roles": ("SELECT",),
            }
        )
        for service, tables in grants.items():
            role = "platform_" + service.replace("-", "_")
            if op.get_bind().scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
            ):
                for name, privileges in tables.items():
                    op.execute(
                        text(f'GRANT {", ".join(privileges)} ON "{name}" TO "{role}"')
                    )


def downgrade():
    raise RuntimeError(
        "Provider credentials and imported deployment records require a reviewed backup rollback."
    )
