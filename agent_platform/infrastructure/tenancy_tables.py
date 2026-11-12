"""Identity and tenant control-plane tables, separate from tenant content."""

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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

memberships = Table(
    "platform_memberships",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("user_id", String(64), nullable=False),
    Column("role", String(24), nullable=False),
    Column("status", String(20), nullable=False, default="active"),
    Column("authz_version", Integer, nullable=False, default=1),
    Column("joined_at", String(40), nullable=False),
    Column("revoked_at", String(40)),
    UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
    UniqueConstraint("tenant_id", "id", name="uq_membership_tenant_id"),
    UniqueConstraint("tenant_id", "id", "user_id", name="uq_membership_actor"),
    ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
    ForeignKeyConstraint(["user_id"], ["platform_users.id"]),
    CheckConstraint(
        "role IN ('owner','tenant_admin','member','finance_viewer')",
        name="ck_membership_role",
    ),
    CheckConstraint("status IN ('active','revoked')", name="ck_membership_status"),
)
Index("ix_membership_user_status", memberships.c.user_id, memberships.c.status)

platform_roles = Table(
    "platform_roles",
    metadata,
    Column("user_id", String(64), primary_key=True),
    Column("role", String(32), primary_key=True),
    Column("granted_by", String(64)),
    Column("granted_at", String(40), nullable=False),
    Column("revoked_at", String(40)),
    ForeignKeyConstraint(["user_id"], ["platform_users.id"]),
    CheckConstraint(
        "role IN ('platform_admin','platform_finance','platform_support')",
        name="ck_platform_role",
    ),
)

tenant_settings = Table(
    "platform_tenant_settings",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("status", String(20), nullable=False, default="active"),
    Column("version", Integer, nullable=False, default=1),
    Column("default_model_id", String(64)),
    Column("ordered_model_ids", JSON, nullable=False, default=list),
    Column("owner_unavailable", Boolean, nullable=False, default=False),
    Column("suspension_reason", Text, nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
    ForeignKeyConstraint(
        ["tenant_id", "default_model_id"],
        ["platform_models.tenant_id", "platform_models.id"],
    ),
    CheckConstraint(
        "status IN ('provisioning','active','suspended','closing','deleted')",
        name="ck_tenant_status",
    ),
)

entitlements = Table(
    "platform_entitlements",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("version", Integer, nullable=False, default=1),
    Column("max_members", Integer, nullable=False, default=100),
    Column("max_agents", Integer, nullable=False, default=100),
    Column("max_sse_connections", Integer, nullable=False, default=20),
    Column("max_export_jobs", Integer, nullable=False, default=2),
    Column("max_queued_runs", Integer, nullable=False, default=100),
    Column("max_concurrent_runs", Integer, nullable=False, default=10),
    Column("rpm", Integer, nullable=False, default=120),
    Column("tpm", Integer, nullable=False, default=200000),
    Column("max_budget", String(40)),
    Column("updated_at", String(40), nullable=False),
    Column("updated_by", String(64)),
    ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
    CheckConstraint(
        "max_members >= 1 AND max_agents >= 0 AND max_sse_connections >= 0 "
        "AND max_export_jobs >= 0 AND max_queued_runs >= 0 AND max_concurrent_runs >= 0 "
        "AND rpm >= 0 AND tpm >= 0",
        name="ck_entitlement_limits",
    ),
)

invitations = Table(
    "platform_invitations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("email", String(254), nullable=False),
    Column("role", String(24), nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("expires_at", Float, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("invited_by", String(64), nullable=False),
    Column("accepted_at", String(40)),
    Column("accepted_by", String(64)),
    Column("revoked_at", String(40)),
    ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
    CheckConstraint(
        "role IN ('tenant_admin','member','finance_viewer')",
        name="ck_invitation_role",
    ),
)
Index("ix_invitation_tenant_email", invitations.c.tenant_id, invitations.c.email)

operation_audits = Table(
    "platform_operation_audits",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("actor_user_id", String(64)),
    Column("tenant_id", String(64)),
    Column("action", String(100), nullable=False),
    Column("target", String(200), nullable=False),
    Column("reason", Text, nullable=False, default=""),
    Column("details", JSON, nullable=False, default=dict),
    Column("created_at", String(40), nullable=False),
)
Index("ix_platform_operations_created", operation_audits.c.created_at)

tenant_creation_requests = Table(
    "platform_tenant_creation_requests",
    metadata,
    Column("actor_user_id", String(64), primary_key=True),
    Column("idempotency_key", String(160), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("created_at", String(40), nullable=False),
    ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
)
