"""Initial platform schema. Financial tables live in the billing module."""

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
    return [
        Column("id", String(64), primary_key=True),
        Column("tenant_id", String(64), nullable=False, index=True),
        Column("created_at", String(40), nullable=False),
    ]


tenants = Table(
    "platform_tenants",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("created_at", String(40), nullable=False),
)
users = Table(
    "platform_users",
    metadata,
    *resource_columns(),
    Column("email", String(254), nullable=False, unique=True),
    Column("name", String(120), nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("role", String(20), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column(
        "global_status",
        String(20),
        nullable=False,
        default="active",
        server_default="active",
    ),
    Column("auth_version", Integer, nullable=False, default=1, server_default="1"),
    Column("email_verified_at", String(40)),
    UniqueConstraint("tenant_id", "id"),
)
auth_sessions = Table(
    "platform_auth_sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False, index=True),
    Column("csrf_token", String(128), nullable=False),
    Column("expires_at", Float, nullable=False),
)
login_attempts = Table(
    "platform_login_attempts",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("window", Integer, nullable=False),
    Column("count", Integer, nullable=False),
)
models = Table(
    "platform_models",
    metadata,
    *resource_columns(),
    Column("name", String(120), nullable=False),
    Column("alias", String(200), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("input_price", String(40), nullable=False),
    Column("output_price", String(40), nullable=False),
    Column("context_window", Integer, nullable=False),
    Column("max_output_tokens", Integer, nullable=False),
    Column("price_version", Integer, nullable=False, default=1),
    UniqueConstraint("tenant_id", "alias"),
    UniqueConstraint("tenant_id", "id"),
)
agents = Table(
    "platform_agents",
    metadata,
    *resource_columns(),
    Column("name", String(120), nullable=False),
    Column("description", Text, nullable=False),
    Column("system_prompt", Text, nullable=False),
    # NULL selects a model at run creation, independent of the published role.
    Column("model_id", String(64)),
    Column("builtin_key", String(64)),
    Column("temperature", Float, nullable=False),
    Column("max_steps", Integer, nullable=False),
    Column("max_tokens", Integer, nullable=False),
    Column("tools", JSON, nullable=False),
    Column("execution_policy", JSON, nullable=False, default=dict, server_default="{}"),
    Column(
        "execution_mode",
        String(16),
        nullable=False,
        default="single",
        server_default="single",
    ),
    Column("capability_keys", JSON, nullable=False, default=list, server_default="[]"),
    Column("task_contracts", JSON, nullable=False, default=list, server_default="[]"),
    Column("collaboration_spec", JSON),
    Column("available", Boolean, nullable=False, default=True, server_default="true"),
    Column("published_version", Integer, nullable=False, default=0),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "builtin_key", name="uq_agent_builtin_tenant"),
    ForeignKeyConstraint(
        ["tenant_id", "model_id"], ["platform_models.tenant_id", "platform_models.id"]
    ),
)
agent_versions = Table(
    "platform_agent_versions",
    metadata,
    Column("agent_id", String(64), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("spec", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint(
        "tenant_id", "agent_id", "version", name="uq_agent_versions_tenant_version"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "agent_id"], ["platform_agents.tenant_id", "platform_agents.id"]
    ),
)
sessions = Table(
    "platform_sessions",
    metadata,
    *resource_columns(),
    Column("user_id", String(64), nullable=False),
    Column("agent_id", String(64), nullable=False),
    Column("title", String(160), nullable=False),
    UniqueConstraint("tenant_id", "id"),
    ForeignKeyConstraint(
        ["tenant_id", "user_id"],
        ["platform_memberships.tenant_id", "platform_memberships.user_id"],
        name="fk_sessions_membership",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "agent_id"], ["platform_agents.tenant_id", "platform_agents.id"]
    ),
)
runs = Table(
    "platform_runs",
    metadata,
    *resource_columns(),
    Column("session_id", String(64), nullable=False, index=True),
    Column("user_id", String(64), nullable=False),
    Column("agent_name", String(120), nullable=False),
    Column("outcome", String(16)),
    Column("spec", JSON, nullable=False),
    Column("message", Text, nullable=False),
    Column("status", String(24), nullable=False, index=True),
    Column("error", Text, nullable=False, default=""),
    Column("idempotency_key", String(160), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("cancel_requested", Boolean, nullable=False, default=False),
    Column("owner", String(64)),
    # Nullable only for legacy writers; enqueue freezes this before publication.
    Column("queue_expires_at", Float),
    Column("lease_until", Float),
    Column("deadline", Float),
    Column("fence", Integer, nullable=False, default=0),
    Column("finished_at", String(40)),
    UniqueConstraint("tenant_id", "user_id", "idempotency_key"),
    UniqueConstraint("tenant_id", "id", name="uq_runs_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "session_id"],
        ["platform_sessions.tenant_id", "platform_sessions.id"],
    ),
)
messages = Table(
    "platform_messages",
    metadata,
    *resource_columns(),
    Column("session_id", String(64), nullable=False, index=True),
    Column("run_id", String(64), nullable=False),
    Column("role", String(20), nullable=False),
    Column("content", Text, nullable=False),
    UniqueConstraint("run_id", "role"),
    ForeignKeyConstraint(
        ["tenant_id", "session_id"],
        ["platform_sessions.tenant_id", "platform_sessions.id"],
        name="fk_messages_session",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"],
        ["platform_runs.tenant_id", "platform_runs.id"],
        name="fk_messages_run",
    ),
)
run_events = Table(
    "platform_run_events",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("sequence", Integer, primary_key=True),
    Column("type", String(64), nullable=False),
    Column("data", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"],
        ["platform_runs.tenant_id", "platform_runs.id"],
        name="fk_events_tenant_run",
    ),
)
calls = Table(
    "platform_calls",
    metadata,
    *resource_columns(),
    Column("user_id", String(64), nullable=False),
    Column("run_id", String(64), nullable=False, index=True),
    Column("model_id", String(64), nullable=False),
    Column("model", String(200), nullable=False),
    Column("task_id", String(64)),
    Column("execution_id", String(64)),
    Column("agent_name", String(120), nullable=False),
    Column("user_email", String(254), nullable=False),
    Column("status", String(24), nullable=False),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("cost", String(40), nullable=False, default="0"),
    Column("provider_cost", String(40)),
    Column("price_version", Integer, nullable=False),
    Column("raw_usage", JSON),
    Column("error", Text, nullable=False, default=""),
    Column("finished_at", String(40)),
    ForeignKeyConstraint(
        ["tenant_id", "run_id", "task_id"],
        ["agent_tasks.tenant_id", "agent_tasks.root_run_id", "agent_tasks.id"],
        name="fk_calls_task",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "run_id", "execution_id"],
        [
            "agent_executions.tenant_id",
            "agent_executions.root_run_id",
            "agent_executions.id",
        ],
        name="fk_calls_execution",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"],
        ["platform_runs.tenant_id", "platform_runs.id"],
        name="fk_calls_run",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "model_id"],
        ["platform_models.tenant_id", "platform_models.id"],
        name="fk_calls_model",
    ),
)
audits = Table(
    "platform_audits",
    metadata,
    *resource_columns(),
    Column("actor_id", String(64), nullable=False),
    Column("actor_email", String(254), nullable=False),
    Column("action", String(100), nullable=False),
    Column("target", String(200), nullable=False),
)
Index("platform_calls_tenant_date", calls.c.tenant_id, calls.c.created_at)
Index("platform_runs_tenant_date", runs.c.tenant_id, runs.c.created_at)
Index(
    "ix_events_tenant_run_sequence",
    run_events.c.tenant_id,
    run_events.c.run_id,
    run_events.c.sequence,
)
Index(
    "ix_runs_tenant_status_created",
    runs.c.tenant_id,
    runs.c.status,
    runs.c.created_at,
    runs.c.id,
)
