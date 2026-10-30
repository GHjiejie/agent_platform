"""Append-only usage evidence and independent supplier cost admission."""

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
    UniqueConstraint,
)

from agent_platform.infrastructure.db import metadata
from agent_platform.infrastructure.money import ExactMoney

cost_prices = Table(
    "platform_cost_price_versions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("deployment_id", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("input_price", ExactMoney(), nullable=False),
    Column("output_price", ExactMoney(), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("created_by", String(64), nullable=False),
    Column("created_at", String(40), nullable=False),
    ForeignKeyConstraint(["deployment_id"], ["model_deployments.id"]),
    UniqueConstraint("deployment_id", "version"),
)
cost_budgets = Table(
    "platform_cost_budgets",
    metadata,
    Column("scope", String(16), primary_key=True),
    Column("subject_id", String(64), primary_key=True),
    Column("limit", ExactMoney(), nullable=False),
    Column("spent", ExactMoney(), nullable=False),
    Column("reserved", ExactMoney(), nullable=False),
    Column("blocked", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
    Column("updated_by", String(64), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
cost_holds = Table(
    "platform_cost_reservations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("call_id", String(64), nullable=False),
    Column("deployment_id", String(64)),
    Column("price_version_id", String(64)),
    Column("charge_kind", String(16), nullable=False, server_default="tokens"),
    Column("input_price", ExactMoney(), nullable=False),
    Column("output_price", ExactMoney(), nullable=False),
    Column("amount", ExactMoney(), nullable=False),
    Column("cost", ExactMoney()),
    Column("quality", String(24), nullable=False),
    Column("status", String(24), nullable=False),
    Column("budgets", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("tenant_id", "call_id"),
    ForeignKeyConstraint(["price_version_id"], ["platform_cost_price_versions.id"]),
    ForeignKeyConstraint(
        ["tenant_id", "call_id"],
        ["billing_reservations.tenant_id", "billing_reservations.call_id"],
    ),
)
budget_totals = Table(
    "billing_budget_totals",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("scope", String(16), primary_key=True),
    Column("subject_id", String(64), primary_key=True),
    Column("spent", ExactMoney(), nullable=False),
)
usage_receipts = Table(
    "platform_usage_receipts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("call_id", String(64), nullable=False),
    Column("attempt_id", String(64)),
    Column("source", String(40), nullable=False),
    Column("source_event_id", String(200), nullable=False),
    Column("gateway_request_id", String(200)),
    Column("quality", String(24), nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("provider_cost", ExactMoney()),
    Column("evidence", JSON, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("tenant_id", "call_id", "source", "source_event_id"),
    ForeignKeyConstraint(
        ["tenant_id", "call_id"],
        ["billing_reservations.tenant_id", "billing_reservations.call_id"],
    ),
)
reconciliation_jobs = Table(
    "platform_metering_jobs",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("attempt_id", String(64), primary_key=True),
    Column("call_id", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("next_attempt_at", Float, nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("owner", String(64)),
    Column("fence", Integer, nullable=False),
    Column("lease_until", Float),
    Column("last_error", String(80), nullable=False),
    Column("updated_at", String(40), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "attempt_id"],
        ["platform_call_attempts.tenant_id", "platform_call_attempts.id"],
    ),
)
Index(
    "ix_metering_jobs_due",
    reconciliation_jobs.c.tenant_id,
    reconciliation_jobs.c.status,
    reconciliation_jobs.c.next_attempt_at,
)
Index("ix_usage_receipts_call", usage_receipts.c.tenant_id, usage_receipts.c.call_id)
