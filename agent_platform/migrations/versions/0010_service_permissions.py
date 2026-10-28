"""Restrict preprovisioned service identities to the reviewed table matrix.

Roles/passwords are created by the administrator before this owner migration.
Absent roles are left absent (e.g. local SQLite/custom managed PostgreSQL).
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_service_permissions"
down_revision = "0009_metering_costs"
branch_labels = None
depends_on = None

ROLE_NAMES = {
    "api": "platform_api",
    "gateway-sync": "platform_gateway_sync",
    "maintenance": "platform_maintenance",
    "worker": "platform_worker",
}
PRIVILEGES = {
    "api": {
        "alembic_version": ("SELECT",),
        "billing_audit": ("INSERT", "SELECT"),
        "billing_budget_totals": ("INSERT", "SELECT", "UPDATE"),
        "billing_entries": ("INSERT", "SELECT"),
        "billing_quotas": ("INSERT", "SELECT", "UPDATE"),
        "billing_reconciliations": ("INSERT", "SELECT"),
        "billing_reservations": ("INSERT", "SELECT", "UPDATE"),
        "billing_transactions": ("INSERT", "SELECT"),
        "billing_wallets": ("INSERT", "SELECT", "UPDATE"),
        "export_artifacts": ("INSERT", "SELECT", "UPDATE"),
        "export_jobs": ("INSERT", "SELECT", "UPDATE"),
        "gateway_bindings": ("INSERT", "SELECT", "UPDATE"),
        "gateway_control_audits": ("INSERT", "SELECT"),
        "gateway_credential_versions": ("INSERT", "SELECT", "UPDATE"),
        "gateway_outbox": ("INSERT", "SELECT", "UPDATE"),
        "model_deployments": ("INSERT", "SELECT", "UPDATE"),
        "model_price_versions": ("INSERT", "SELECT", "UPDATE"),
        "platform_agent_versions": ("INSERT", "SELECT", "UPDATE"),
        "platform_agents": ("INSERT", "SELECT", "UPDATE"),
        "platform_audits": ("INSERT", "SELECT"),
        "platform_auth_sessions": ("DELETE", "INSERT", "SELECT", "UPDATE"),
        "platform_call_attempts": ("INSERT", "SELECT", "UPDATE"),
        "platform_calls": ("INSERT", "SELECT", "UPDATE"),
        "platform_cost_budgets": ("INSERT", "SELECT", "UPDATE"),
        "platform_cost_price_versions": ("INSERT", "SELECT"),
        "platform_cost_reservations": ("INSERT", "SELECT", "UPDATE"),
        "platform_entitlements": ("INSERT", "SELECT", "UPDATE"),
        "platform_invitations": ("INSERT", "SELECT", "UPDATE"),
        "platform_login_attempts": ("DELETE", "INSERT", "SELECT", "UPDATE"),
        "platform_memberships": ("INSERT", "SELECT", "UPDATE"),
        "platform_messages": ("INSERT", "SELECT", "UPDATE"),
        "platform_metering_jobs": ("INSERT", "SELECT", "UPDATE"),
        "platform_models": ("INSERT", "SELECT", "UPDATE"),
        "platform_operation_audits": ("INSERT", "SELECT"),
        "platform_roles": ("INSERT", "SELECT", "UPDATE"),
        "platform_run_events": ("INSERT", "SELECT", "UPDATE"),
        "platform_runs": ("INSERT", "SELECT", "UPDATE"),
        "platform_service_heartbeats": ("SELECT",),
        "platform_sessions": ("INSERT", "SELECT", "UPDATE"),
        "platform_stream_leases": ("DELETE", "INSERT", "SELECT", "UPDATE"),
        "platform_support_grants": ("INSERT", "SELECT", "UPDATE"),
        "platform_tenant_creation_requests": ("INSERT", "SELECT", "UPDATE"),
        "platform_tenant_settings": ("INSERT", "SELECT", "UPDATE"),
        "platform_tenants": ("INSERT", "SELECT", "UPDATE"),
        "platform_usage_receipts": ("INSERT", "SELECT"),
        "platform_users": ("INSERT", "SELECT", "UPDATE"),
        "tenant_lifecycle_jobs": ("INSERT", "SELECT", "UPDATE"),
        "tenant_model_bindings": ("INSERT", "SELECT", "UPDATE"),
    },
    "gateway-sync": {
        "alembic_version": ("SELECT",),
        "billing_reservations": ("SELECT",),
        "gateway_bindings": ("INSERT", "SELECT", "UPDATE"),
        "gateway_control_audits": ("INSERT", "SELECT"),
        "gateway_credential_versions": ("INSERT", "SELECT", "UPDATE"),
        "gateway_outbox": ("INSERT", "SELECT", "UPDATE"),
        "model_deployments": ("SELECT",),
        "platform_call_attempts": ("SELECT",),
        "platform_entitlements": ("SELECT",),
        "platform_metering_jobs": ("INSERT", "SELECT", "UPDATE"),
        "platform_models": ("SELECT",),
        "platform_service_heartbeats": ("DELETE", "INSERT", "SELECT", "UPDATE"),
        "platform_tenant_settings": ("SELECT",),
        "platform_tenants": ("SELECT",),
        "platform_usage_receipts": ("INSERT", "SELECT"),
        "tenant_model_bindings": ("SELECT",),
    },
    "maintenance": {
        "alembic_version": ("SELECT",),
        "billing_reservations": ("SELECT", "UPDATE"),
        "export_artifacts": ("DELETE", "INSERT", "SELECT", "UPDATE"),
        "export_jobs": ("INSERT", "SELECT", "UPDATE"),
        "gateway_bindings": ("INSERT", "SELECT", "UPDATE"),
        "gateway_control_audits": ("INSERT", "SELECT"),
        "gateway_credential_versions": ("INSERT", "SELECT", "UPDATE"),
        "gateway_outbox": ("INSERT", "SELECT", "UPDATE"),
        "model_deployments": ("SELECT",),
        "model_price_versions": ("SELECT",),
        "platform_agent_versions": ("INSERT", "SELECT", "UPDATE"),
        "platform_agents": ("INSERT", "SELECT", "UPDATE"),
        "platform_auth_sessions": ("SELECT",),
        "platform_call_attempts": ("SELECT",),
        "platform_calls": ("SELECT", "UPDATE"),
        "platform_entitlements": ("SELECT",),
        "platform_invitations": ("SELECT", "UPDATE"),
        "platform_memberships": ("SELECT", "UPDATE"),
        "platform_messages": ("INSERT", "SELECT", "UPDATE"),
        "platform_models": ("SELECT",),
        "platform_operation_audits": ("INSERT", "SELECT"),
        "platform_roles": ("SELECT",),
        "platform_run_events": ("INSERT", "SELECT", "UPDATE"),
        "platform_runs": ("INSERT", "SELECT", "UPDATE"),
        "platform_service_heartbeats": ("DELETE", "INSERT", "SELECT", "UPDATE"),
        "platform_sessions": ("INSERT", "SELECT", "UPDATE"),
        "platform_tenant_settings": ("INSERT", "SELECT", "UPDATE"),
        "platform_tenants": ("SELECT",),
        "platform_users": ("SELECT",),
        "tenant_lifecycle_jobs": ("INSERT", "SELECT", "UPDATE"),
        "tenant_model_bindings": ("SELECT",),
    },
    "worker": {
        "alembic_version": ("SELECT",),
        "billing_audit": ("INSERT", "SELECT"),
        "billing_budget_totals": ("INSERT", "SELECT", "UPDATE"),
        "billing_entries": ("INSERT", "SELECT"),
        "billing_quotas": ("SELECT",),
        "billing_reconciliations": ("SELECT",),
        "billing_reservations": ("INSERT", "SELECT", "UPDATE"),
        "billing_transactions": ("INSERT", "SELECT"),
        "billing_wallets": ("INSERT", "SELECT", "UPDATE"),
        "gateway_bindings": ("SELECT",),
        "gateway_credential_versions": ("SELECT",),
        "gateway_outbox": ("SELECT",),
        "model_deployments": ("SELECT",),
        "model_price_versions": ("SELECT",),
        "platform_agent_versions": ("SELECT",),
        "platform_agents": ("SELECT",),
        "platform_call_attempts": ("INSERT", "SELECT", "UPDATE"),
        "platform_calls": ("INSERT", "SELECT", "UPDATE"),
        "platform_cost_budgets": ("INSERT", "SELECT", "UPDATE"),
        "platform_cost_price_versions": ("SELECT",),
        "platform_cost_reservations": ("INSERT", "SELECT", "UPDATE"),
        "platform_entitlements": ("SELECT",),
        "platform_memberships": ("SELECT",),
        "platform_messages": ("INSERT", "SELECT", "UPDATE"),
        "platform_metering_jobs": ("INSERT", "SELECT", "UPDATE"),
        "platform_models": ("SELECT",),
        "platform_roles": ("SELECT",),
        "platform_run_events": ("INSERT", "SELECT", "UPDATE"),
        "platform_runs": ("INSERT", "SELECT", "UPDATE"),
        "platform_service_heartbeats": ("DELETE", "INSERT", "SELECT", "UPDATE"),
        "platform_sessions": ("SELECT",),
        "platform_tenant_settings": ("SELECT",),
        "platform_tenants": ("SELECT",),
        "platform_usage_receipts": ("INSERT", "SELECT"),
        "platform_users": ("SELECT",),
        "tenant_model_bindings": ("SELECT",),
    },
}


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    schema = connection.scalar(sa.text("SELECT current_schema()"))
    quote = connection.dialect.identifier_preparer.quote
    for service, role in ROLE_NAMES.items():
        exists = connection.scalar(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
        )
        if not exists:
            continue
        role_flags = connection.execute(
            sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=:role"),
            {"role": role},
        ).one()
        if any(role_flags):
            raise RuntimeError("Service identities must have NOSUPERUSER NOBYPASSRLS")
        connection.exec_driver_sql(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {quote(schema)} FROM {quote(role)}"
        )
        for table, privileges in PRIVILEGES[service].items():
            connection.exec_driver_sql(
                f"GRANT {', '.join(privileges)} ON {quote(schema)}.{quote(table)} TO {quote(role)}"
            )


def downgrade():
    raise RuntimeError(
        "Service permission narrowing has no automatic broad-grant downgrade"
    )
