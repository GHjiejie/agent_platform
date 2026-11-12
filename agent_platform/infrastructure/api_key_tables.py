"""Member credentials and model requests; both are tenant isolated."""

from sqlalchemy import (
    JSON,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    text,
)

from agent_platform.infrastructure.db import metadata

keys = Table(
    "member_api_keys",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("user_id", String(64), nullable=False),
    Column("membership_id", String(64), nullable=False),
    Column("membership_version", Integer, nullable=False),
    Column("name", String(120), nullable=False),
    Column("prefix", String(32), nullable=False),
    Column("secret_hash", String(64), nullable=False),
    Column("model_ids", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("expires_at", String(40), nullable=False),
    Column("revoked_at", String(40)),
    Column("last_used_at", String(40)),
    UniqueConstraint("tenant_id", "id"),
    ForeignKeyConstraint(
        ["tenant_id", "user_id"],
        ["platform_memberships.tenant_id", "platform_memberships.user_id"],
    ),
)
Index("ix_member_api_keys_owner", keys.c.tenant_id, keys.c.user_id, keys.c.created_at)

requests = Table(
    "model_api_requests",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("key_id", String(64), nullable=False),
    Column("messages", JSON, nullable=False),
    Column("tools", JSON, nullable=False),
    Column("result", JSON),
    Column("created_at", String(40), nullable=False),
    Column("error_code", String(80), nullable=False, server_default=text("''")),
    ForeignKeyConstraint(
        ["tenant_id", "id"], ["platform_runs.tenant_id", "platform_runs.id"]
    ),
    ForeignKeyConstraint(
        ["tenant_id", "key_id"], ["member_api_keys.tenant_id", "member_api_keys.id"]
    ),
)

API_KEY_PRIVILEGES = {
    "api": {
        keys.name: ("INSERT", "SELECT", "UPDATE"),
        requests.name: ("INSERT", "SELECT"),
    },
    "worker": {keys.name: ("SELECT",), requests.name: ("SELECT", "UPDATE")},
    "maintenance": {
        keys.name: ("SELECT", "UPDATE"),
        requests.name: ("SELECT", "UPDATE"),
    },
}
