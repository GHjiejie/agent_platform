"""Reviewed table privileges shared by deployment and real PostgreSQL acceptance.

Roles remain non-owner/NOBYPASSRLS. Gateway control only deposits usage evidence;
Workers settle it, so control credentials cannot mutate a customer wallet.
"""

import argparse
import re

ROLE_NAMES = {
    "api": "platform_api",
    "worker": "platform_worker",
    "gateway-sync": "platform_gateway_sync",
    "maintenance": "platform_maintenance",
}

CONTROL_READ = {
    "platform_users",
    "platform_memberships",
    "platform_roles",
    "platform_tenants",
    "platform_tenant_settings",
    "platform_entitlements",
}
GATEWAY_READ = {
    "model_deployments",
    "model_price_versions",
    "tenant_model_bindings",
    "platform_models",
    "gateway_bindings",
    "gateway_credential_versions",
    "gateway_outbox",
}
FINANCE_READ = {
    "billing_wallets",
    "billing_quotas",
    "billing_reservations",
    "billing_transactions",
    "billing_entries",
    "billing_audit",
    "billing_reconciliations",
    "billing_budget_totals",
    "platform_cost_budgets",
    "platform_cost_price_versions",
    "platform_cost_reservations",
    "platform_usage_receipts",
    "platform_metering_jobs",
}
CONTENT_READ = {
    "platform_agents",
    "platform_agent_versions",
    "platform_sessions",
    "platform_messages",
    "platform_runs",
    "platform_run_events",
    "platform_calls",
    "platform_call_attempts",
}
OPERATIONS_READ = {"export_jobs", "export_artifacts", "tenant_lifecycle_jobs"}
APPEND_ONLY = {
    "billing_transactions",
    "billing_entries",
    "billing_audit",
    "billing_reconciliations",
    "platform_usage_receipts",
    "platform_cost_price_versions",
    "gateway_control_audits",
    "platform_operation_audits",
    "platform_audits",
}


def _matrix(read, write=(), append=(), delete=(), update=()):
    result = {table: {"SELECT"} for table in set(read) | {"alembic_version"}}
    for table in write:
        result.setdefault(table, set()).update({"SELECT", "INSERT", "UPDATE"})
    for table in update:
        result.setdefault(table, set()).update({"SELECT", "UPDATE"})
    for table in append:
        result.setdefault(table, set()).update({"SELECT", "INSERT"})
    for table in delete:
        result.setdefault(table, set()).add("DELETE")
    for table in APPEND_ONLY:
        if table in result:
            result[table].difference_update({"UPDATE", "DELETE"})
    return {
        table: tuple(sorted(privileges)) for table, privileges in sorted(result.items())
    }


ROLE_PRIVILEGES = {
    "api": _matrix(
        CONTROL_READ
        | GATEWAY_READ
        | FINANCE_READ
        | CONTENT_READ
        | OPERATIONS_READ
        | {
            "platform_auth_sessions",
            "platform_login_attempts",
            "platform_invitations",
            "platform_support_grants",
            "platform_tenant_creation_requests",
            "platform_audits",
            "platform_operation_audits",
            "gateway_control_audits",
            "platform_stream_leases",
            "platform_service_heartbeats",
        },
        write=CONTROL_READ
        | GATEWAY_READ
        | FINANCE_READ
        | CONTENT_READ
        | OPERATIONS_READ
        | {
            "platform_auth_sessions",
            "platform_login_attempts",
            "platform_invitations",
            "platform_support_grants",
            "platform_tenant_creation_requests",
            "platform_stream_leases",
        },
        append={
            "platform_operation_audits",
            "gateway_control_audits",
            "platform_audits",
        },
        delete={
            "platform_auth_sessions",
            "platform_login_attempts",
            "platform_stream_leases",
        },
    ),
    "worker": _matrix(
        CONTROL_READ | GATEWAY_READ | FINANCE_READ | CONTENT_READ,
        write={
            "platform_runs",
            "platform_messages",
            "platform_run_events",
            "platform_calls",
            "platform_call_attempts",
            "billing_wallets",
            "billing_reservations",
            "billing_budget_totals",
            "platform_cost_budgets",
            "platform_cost_reservations",
            "platform_metering_jobs",
            "platform_service_heartbeats",
        },
        append={
            "billing_transactions",
            "billing_entries",
            "billing_audit",
            "platform_usage_receipts",
        },
        delete={"platform_service_heartbeats"},
    ),
    "gateway-sync": _matrix(
        {
            "platform_tenants",
            "platform_tenant_settings",
            "platform_entitlements",
            "platform_models",
            "tenant_model_bindings",
            "model_deployments",
            "gateway_bindings",
            "gateway_credential_versions",
            "gateway_outbox",
            "platform_call_attempts",
            "billing_reservations",
            "platform_metering_jobs",
        },
        write={
            "gateway_bindings",
            "gateway_credential_versions",
            "gateway_outbox",
            "platform_metering_jobs",
            "platform_service_heartbeats",
        },
        append={"gateway_control_audits", "platform_usage_receipts"},
        delete={"platform_service_heartbeats"},
    ),
    "maintenance": _matrix(
        CONTROL_READ
        | GATEWAY_READ
        | CONTENT_READ
        | OPERATIONS_READ
        | {"billing_reservations", "platform_auth_sessions"},
        write={
            "export_jobs",
            "export_artifacts",
            "tenant_lifecycle_jobs",
            "platform_tenant_settings",
            "platform_runs",
            "platform_messages",
            "platform_run_events",
            "platform_agents",
            "platform_agent_versions",
            "platform_sessions",
            "gateway_bindings",
            "gateway_credential_versions",
            "gateway_outbox",
            "platform_service_heartbeats",
        },
        update={
            "platform_calls",
            "platform_invitations",
            "billing_reservations",
            "platform_memberships",
        },
        append={"gateway_control_audits", "platform_operation_audits"},
        delete={"export_artifacts", "platform_service_heartbeats"},
    ),
}


ROLE_PRIVILEGES["worker"]["agent_task_leases"] = ("INSERT", "SELECT", "UPDATE")
ROLE_PRIVILEGES["maintenance"]["agent_task_leases"] = ("SELECT",)

for _table in (
    "remote_agent_credentials",
    "remote_agent_versions",
    "remote_agent_endpoints",
):
    ROLE_PRIVILEGES["api"][_table] = ("INSERT", "SELECT", "UPDATE")
    ROLE_PRIVILEGES["worker"][_table] = (
        ("SELECT", "UPDATE") if _table == "remote_agent_endpoints" else ("SELECT",)
    )
    ROLE_PRIVILEGES["maintenance"][_table] = ("SELECT", "UPDATE")
ROLE_PRIVILEGES["api"]["remote_agent_credential_secrets"] = ("INSERT",)
ROLE_PRIVILEGES["worker"]["remote_agent_credential_secrets"] = ("SELECT",)
ROLE_PRIVILEGES["maintenance"]["remote_agent_credential_secrets"] = ("SELECT", "UPDATE")

# Private journal content is not a public execution projection.
ROLE_PRIVILEGES["worker"]["execution_journal"] = ("INSERT", "SELECT", "UPDATE")
ROLE_PRIVILEGES["maintenance"]["execution_journal"] = ("SELECT", "UPDATE")
for _table in ("execution_checkpoints", "execution_checkpoint_writes"):
    ROLE_PRIVILEGES["worker"][_table] = ("INSERT", "SELECT", "UPDATE")
    ROLE_PRIVILEGES["maintenance"][_table] = ("SELECT", "UPDATE")

# Collaboration authority stays with the Worker; API only projects execution state.
for _table in (
    "collaboration_plans",
    "agent_tasks",
    "agent_executions",
    "collaboration_artifacts",
):
    ROLE_PRIVILEGES["api"][_table] = ("SELECT",)
    ROLE_PRIVILEGES["worker"][_table] = ("INSERT", "SELECT", "UPDATE")
    ROLE_PRIVILEGES["maintenance"][_table] = ("SELECT", "UPDATE")


def privileges_for(role):
    role = next((key for key, name in ROLE_NAMES.items() if name == role), role)
    return ROLE_PRIVILEGES[role]


def render_grants(role, *, schema="public", database="platform", role_name=None):
    name = role_name or ROLE_NAMES[role]
    for value in (name, schema, database):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("Invalid SQL identifier")
    statements = [
        f'GRANT CONNECT ON DATABASE "{database}" TO "{name}";',
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{name}";',
        f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema}" FROM "{name}";',
    ]
    for table, privileges in privileges_for(role).items():
        statements.append(
            f'GRANT {", ".join(privileges)} ON "{schema}"."{table}" TO "{name}";'
        )
    return "\n".join(statements)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=tuple(ROLE_NAMES), required=True)
    parser.add_argument("--schema", default="public")
    parser.add_argument("--database", default="platform")
    args = parser.parse_args()
    print(render_grants(args.role, schema=args.schema, database=args.database))


if __name__ == "__main__":
    main()

ROLE_PRIVILEGES["api"]["remote_agent_calls"] = ("SELECT", "UPDATE")
ROLE_PRIVILEGES["worker"]["remote_agent_calls"] = ("INSERT", "SELECT", "UPDATE")
ROLE_PRIVILEGES["maintenance"]["remote_agent_calls"] = ("SELECT",)
