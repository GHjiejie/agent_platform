"""Global identities, memberships and tenant control plane.

Revision ID: 0003
Revises: 0002

Run in a maintenance window. Original identity IDs, money and content survive.
Platform authority is never inferred from a legacy organization admin role.
"""

import hashlib
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _stable_id(kind, *parts):
    return hashlib.sha256(":".join((kind, *parts)).encode()).hexdigest()[:32]


def upgrade() -> None:
    connection = op.get_bind()
    with op.batch_alter_table("platform_users") as batch:
        batch.add_column(
            sa.Column(
                "global_status", sa.String(20), nullable=False, server_default="active"
            )
        )
        batch.add_column(
            sa.Column("auth_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(sa.Column("email_verified_at", sa.String(40)))
    connection.execute(
        sa.text(
            "UPDATE platform_users SET global_status='disabled' WHERE active=:inactive"
        ),
        {"inactive": False},
    )

    op.create_table(
        "platform_memberships",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("authz_version", sa.Integer(), nullable=False),
        sa.Column("joined_at", sa.String(40), nullable=False),
        sa.Column("revoked_at", sa.String(40)),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_membership_tenant_id"),
        sa.UniqueConstraint("tenant_id", "id", "user_id", name="uq_membership_actor"),
        sa.ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["platform_users.id"]),
        sa.CheckConstraint(
            "role IN ('owner','tenant_admin','member','finance_viewer')",
            name="ck_membership_role",
        ),
        sa.CheckConstraint(
            "status IN ('active','revoked')", name="ck_membership_status"
        ),
    )
    op.create_index(
        "ix_membership_user_status", "platform_memberships", ["user_id", "status"]
    )
    op.create_table(
        "platform_roles",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("role", sa.String(32), primary_key=True),
        sa.Column("granted_by", sa.String(64)),
        sa.Column("granted_at", sa.String(40), nullable=False),
        sa.Column("revoked_at", sa.String(40)),
        sa.ForeignKeyConstraint(["user_id"], ["platform_users.id"]),
        sa.CheckConstraint(
            "role IN ('platform_admin','platform_finance','platform_support')",
            name="ck_platform_role",
        ),
    )
    op.create_table(
        "platform_tenant_settings",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("default_model_id", sa.String(64)),
        sa.Column("ordered_model_ids", sa.JSON(), nullable=False),
        sa.Column("owner_unavailable", sa.Boolean(), nullable=False),
        sa.Column("suspension_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "default_model_id"],
            ["platform_models.tenant_id", "platform_models.id"],
        ),
        sa.CheckConstraint(
            "status IN ('provisioning','active','suspended','closing','deleted')",
            name="ck_tenant_status",
        ),
    )
    op.create_table(
        "platform_entitlements",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("max_members", sa.Integer(), nullable=False),
        sa.Column("max_agents", sa.Integer(), nullable=False),
        sa.Column("max_sse_connections", sa.Integer(), nullable=False),
        sa.Column("max_export_jobs", sa.Integer(), nullable=False),
        sa.Column("max_queued_runs", sa.Integer(), nullable=False),
        sa.Column("max_concurrent_runs", sa.Integer(), nullable=False),
        sa.Column("rpm", sa.Integer(), nullable=False),
        sa.Column("tpm", sa.Integer(), nullable=False),
        sa.Column("max_budget", sa.String(40)),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.Column("updated_by", sa.String(64)),
        sa.ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
        sa.CheckConstraint(
            "max_members >= 1 AND max_agents >= 0 AND max_sse_connections >= 0 "
            "AND max_export_jobs >= 0 AND max_queued_runs >= 0 AND max_concurrent_runs >= 0 "
            "AND rpm >= 0 AND tpm >= 0",
            name="ck_entitlement_limits",
        ),
    )
    op.create_table(
        "platform_invitations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("invited_by", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.String(40)),
        sa.Column("accepted_by", sa.String(64)),
        sa.Column("revoked_at", sa.String(40)),
        sa.ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
        sa.CheckConstraint(
            "role IN ('tenant_admin','member','finance_viewer')",
            name="ck_invitation_role",
        ),
    )
    op.create_index(
        "ix_invitation_tenant_email", "platform_invitations", ["tenant_id", "email"]
    )
    op.create_table(
        "platform_operation_audits",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_user_id", sa.String(64)),
        sa.Column("tenant_id", sa.String(64)),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target", sa.String(200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_index(
        "ix_platform_operations_created", "platform_operation_audits", ["created_at"]
    )
    op.create_table(
        "platform_tenant_creation_requests",
        sa.Column("actor_user_id", sa.String(64), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
    )

    # Reflect this revision's schema, not the application's future metadata.
    schema = sa.MetaData()
    schema.reflect(
        connection,
        only=[
            "platform_users",
            "platform_tenants",
            "platform_models",
            "platform_memberships",
            "platform_tenant_settings",
            "platform_entitlements",
            "platform_operation_audits",
        ],
    )
    users, tenants = schema.tables["platform_users"], schema.tables["platform_tenants"]
    members = schema.tables["platform_memberships"]
    settings = schema.tables["platform_tenant_settings"]
    limits = schema.tables["platform_entitlements"]
    audits = schema.tables["platform_operation_audits"]
    stamp = datetime.now(UTC).isoformat()
    for tenant in connection.execute(sa.select(tenants)).mappings():
        tenant_id = tenant["id"]
        rows = list(
            connection.execute(
                sa.select(users)
                .where(users.c.tenant_id == tenant_id)
                .order_by(users.c.created_at, users.c.id)
            ).mappings()
        )
        owner = next(
            (user for user in rows if user["active"] and user["role"] == "admin"), None
        )
        for user in rows:
            member_id = _stable_id("membership", tenant_id, user["id"])
            if not connection.scalar(
                sa.select(members.c.id).where(members.c.id == member_id)
            ):
                connection.execute(
                    members.insert().values(
                        id=member_id,
                        tenant_id=tenant_id,
                        user_id=user["id"],
                        role="owner"
                        if owner and owner["id"] == user["id"]
                        else ("tenant_admin" if user["role"] == "admin" else "member"),
                        status="active" if user["active"] else "revoked",
                        authz_version=1,
                        joined_at=user["created_at"],
                        revoked_at=None if user["active"] else stamp,
                    )
                )
        if not connection.scalar(
            sa.select(settings.c.tenant_id).where(settings.c.tenant_id == tenant_id)
        ):
            connection.execute(
                settings.insert().values(
                    tenant_id=tenant_id,
                    status="active" if owner else "suspended",
                    version=1,
                    default_model_id=None,
                    ordered_model_ids=[],
                    owner_unavailable=not bool(owner),
                    suspension_reason="" if owner else "owner_unavailable",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
        if not connection.scalar(
            sa.select(limits.c.tenant_id).where(limits.c.tenant_id == tenant_id)
        ):
            connection.execute(
                limits.insert().values(
                    tenant_id=tenant_id,
                    version=1,
                    max_members=100,
                    max_agents=100,
                    max_sse_connections=20,
                    max_export_jobs=2,
                    max_queued_runs=100,
                    max_concurrent_runs=10,
                    rpm=120,
                    tpm=200000,
                    max_budget=None,
                    updated_at=stamp,
                    updated_by=None,
                )
            )
        audit_id = _stable_id("identity-migration", tenant_id)
        if not connection.scalar(sa.select(audits.c.id).where(audits.c.id == audit_id)):
            connection.execute(
                audits.insert().values(
                    id=audit_id,
                    actor_user_id=None,
                    tenant_id=tenant_id,
                    action="identity.migrated",
                    target=tenant_id,
                    reason="保留原自部署组织控制权；平台权限须显式配置。",
                    details={"owner_user_id": owner["id"] if owner else None},
                    created_at=stamp,
                )
            )

    convention = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
    foreign_keys = sa.inspect(connection).get_foreign_keys("platform_sessions")
    old_user_key = next(
        (
            key
            for key in foreign_keys
            if key["referred_table"] == "platform_users"
            and key["constrained_columns"] == ["tenant_id", "user_id"]
        ),
        None,
    )
    with op.batch_alter_table(
        "platform_sessions", naming_convention=convention
    ) as batch:
        if old_user_key:
            batch.drop_constraint(
                old_user_key["name"] or "fk_platform_sessions_tenant_id_platform_users",
                type_="foreignkey",
            )
        batch.create_foreign_key(
            "fk_sessions_membership",
            "platform_memberships",
            ["tenant_id", "user_id"],
            ["tenant_id", "user_id"],
        )
    with op.batch_alter_table("platform_runs") as batch:
        batch.create_unique_constraint("uq_runs_tenant_id", ["tenant_id", "id"])
    op.add_column("platform_run_events", sa.Column("tenant_id", sa.String(64)))
    connection.execute(
        sa.text(
            "UPDATE platform_run_events SET tenant_id=(SELECT tenant_id "
            "FROM platform_runs WHERE platform_runs.id=platform_run_events.run_id)"
        )
    )
    if connection.scalar(
        sa.text("SELECT COUNT(*) FROM platform_run_events WHERE tenant_id IS NULL")
    ):
        raise RuntimeError(
            "Orphan run events require repair before tenant identity migration."
        )
    with op.batch_alter_table("platform_run_events") as batch:
        batch.alter_column("tenant_id", existing_type=sa.String(64), nullable=False)
        batch.create_foreign_key(
            "fk_events_tenant_run",
            "platform_runs",
            ["tenant_id", "run_id"],
            ["tenant_id", "id"],
        )
        batch.create_index(
            "ix_events_tenant_run_sequence", ["tenant_id", "run_id", "sequence"]
        )
    # Old cookies encoded the old identity semantics; require a new login.
    connection.execute(sa.text("DELETE FROM platform_auth_sessions"))


def downgrade() -> None:
    raise RuntimeError(
        "Tenant memberships and platform authority cannot be safely collapsed into "
        "legacy users. Use a compatible application rollback or a verified pre-migration backup."
    )
