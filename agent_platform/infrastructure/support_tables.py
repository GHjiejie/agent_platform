"""Explicit, expiring support grants are control-plane metadata, never memberships."""

from sqlalchemy import (
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
)

from .db import metadata

support_grants = Table(
    "platform_support_grants",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("staff_user_id", String(64), nullable=False),
    Column("approved_by", String(64), nullable=False),
    Column("approver_membership_id", String(64), nullable=False),
    Column("approver_membership_version", Integer, nullable=False),
    Column("reason", Text, nullable=False),
    Column("allow_content", Boolean, nullable=False, default=False),
    Column("created_at", String(40), nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("revoked_at", String(40)),
    Column("revoked_by", String(64)),
    ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
    ForeignKeyConstraint(["staff_user_id"], ["platform_users.id"]),
    ForeignKeyConstraint(
        ["tenant_id", "approver_membership_id", "approved_by"],
        [
            "platform_memberships.tenant_id",
            "platform_memberships.id",
            "platform_memberships.user_id",
        ],
    ),
    CheckConstraint(
        "approver_membership_version >= 1", name="ck_support_approver_version"
    ),
)
Index(
    "ix_support_staff_expiry",
    support_grants.c.staff_user_id,
    support_grants.c.expires_at,
)
Index(
    "ix_support_tenant_created", support_grants.c.tenant_id, support_grants.c.created_at
)
