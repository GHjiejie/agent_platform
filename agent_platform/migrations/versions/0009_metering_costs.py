"""Independent supplier cost budgets, immutable usage receipts and bounded reconciliation.

Frozen schema declarations: do not import live application tables here.
"""

from decimal import Decimal

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
    Numeric,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.types import TypeDecorator

revision = "0009_metering_costs"
down_revision = "0008_execution_deadlines"
branch_labels = None
depends_on = None

metadata = sa.MetaData()
Table("model_deployments", metadata, Column("id", String(64), primary_key=True))
Table(
    "platform_call_attempts",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
)
Table(
    "billing_reservations",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("call_id", String(64), primary_key=True),
)


class ExactMoney(TypeDecorator):
    impl = Numeric(30, 12)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(
            String(64) if dialect.name == "sqlite" else Numeric(30, 12)
        )

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return format(Decimal(value), "f") if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


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
    Column("deployment_id", String(64), nullable=False),
    Column("price_version_id", String(64), nullable=False),
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


NEW_TABLES = (
    cost_prices,
    cost_budgets,
    cost_holds,
    budget_totals,
    usage_receipts,
    reconciliation_jobs,
)
IMMUTABLE = (
    "platform_cost_price_versions",
    "platform_usage_receipts",
    "billing_transactions",
    "billing_entries",
    "billing_audit",
    "billing_reconciliations",
)


def upgrade():
    connection = op.get_bind()
    op.add_column("platform_call_attempts", sa.Column("logical_call_id", sa.String(64)))
    with op.batch_alter_table("platform_call_attempts") as batch:
        batch.create_unique_constraint("uq_attempt_tenant_id", ["tenant_id", "id"])
    for table in NEW_TABLES:
        table.create(connection)
    postgres = connection.dialect.name == "postgresql"
    if postgres:
        expression = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
        for table in NEW_TABLES:
            if "tenant_id" in table.c:
                op.execute(
                    sa.text(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY')
                )
                op.execute(
                    sa.text(f'ALTER TABLE "{table.name}" FORCE ROW LEVEL SECURITY')
                )
                op.execute(
                    sa.text(
                        f'CREATE POLICY tenant_isolation ON "{table.name}" USING ({expression}) WITH CHECK ({expression})'
                    )
                )
    tenant_ids = (
        connection.execute(sa.text("SELECT id FROM platform_tenants")).scalars().all()
    )
    for tenant in tenant_ids:
        if postgres:
            connection.execute(
                sa.text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
        connection.execute(
            sa.text(
                "UPDATE platform_call_attempts SET logical_call_id=id WHERE tenant_id=:tenant"
            ),
            {"tenant": tenant},
        )
        totals = {}
        rows = connection.execution_options(stream_results=True).execute(
            sa.text(
                "SELECT user_id, model_id, cost FROM billing_reservations WHERE tenant_id=:tenant"
            ),
            {"tenant": tenant},
        )
        for user, model, cost in rows:
            for key in (("tenant", tenant), ("user", user), ("model", model)):
                totals[key] = totals.get(key, Decimal(0)) + Decimal(str(cost or 0))
        for (scope, subject), spent in totals.items():
            connection.execute(
                budget_totals.insert().values(
                    tenant_id=tenant, scope=scope, subject_id=subject, spent=spent
                )
            )
    if postgres:
        op.execute(
            sa.text(
                "CREATE FUNCTION platform_reject_financial_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Financial evidence is append-only'; END $$"
            )
        )
        for table in IMMUTABLE:
            op.execute(
                sa.text(
                    f'CREATE TRIGGER financial_append_only BEFORE UPDATE OR DELETE ON "{table}" FOR EACH ROW EXECUTE FUNCTION platform_reject_financial_mutation()'
                )
            )
    else:
        for table in IMMUTABLE:
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER financial_append_only_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'Financial evidence is append-only'); END"
                    )
                )


def downgrade():
    raise RuntimeError(
        "Cost and usage evidence cannot be downgraded automatically; restore a reviewed, drained backup."
    )
