"""Bounded stream leases and immutable inference routing evidence."""

from sqlalchemy import (
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
)

from .db import metadata

call_attempts = Table(
    "platform_call_attempts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("run_id", String(64), nullable=False),
    Column("logical_call_id", String(64)),
    Column("membership_id", String(64)),
    Column("deployment_id", String(64), nullable=False),
    Column("credential_version_id", String(64)),
    Column("price_version_id", String(64)),
    Column("config_version", Integer, nullable=False),
    Column("route", String(200), nullable=False),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("tenant_id", "id"),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"], ["platform_runs.tenant_id", "platform_runs.id"]
    ),
)
Index("ix_attempt_tenant_run", call_attempts.c.tenant_id, call_attempts.c.run_id)

stream_leases = Table(
    "platform_stream_leases",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("user_id", String(64), nullable=False),
    Column("run_id", String(64), nullable=False),
    Column("expires_at", Float, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"], ["platform_runs.tenant_id", "platform_runs.id"]
    ),
)
Index("ix_stream_tenant_expiry", stream_leases.c.tenant_id, stream_leases.c.expires_at)

service_heartbeats = Table(
    "platform_service_heartbeats",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("role", String(32), nullable=False),
    Column("expires_at", Float, nullable=False),
)
Index(
    "ix_heartbeat_role_expiry",
    service_heartbeats.c.role,
    service_heartbeats.c.expires_at,
)
