"""Time-limited support authorization with real-actor audit evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0007_support_access"
down_revision = "0006_tenant_operations"
branch_labels = None
depends_on = None


def upgrade():
    # Global control metadata must be enumerable by the assigned support identity.
    # All content queries remain tenant scoped under the existing forced RLS.
    op.create_table(
        "platform_support_grants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("staff_user_id", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(64), nullable=False),
        sa.Column("approver_membership_id", sa.String(64), nullable=False),
        sa.Column("approver_membership_version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("allow_content", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.String(40)),
        sa.Column("revoked_by", sa.String(64)),
        sa.ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
        sa.ForeignKeyConstraint(["staff_user_id"], ["platform_users.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approver_membership_id", "approved_by"],
            [
                "platform_memberships.tenant_id",
                "platform_memberships.id",
                "platform_memberships.user_id",
            ],
        ),
        sa.CheckConstraint(
            "approver_membership_version >= 1", name="ck_support_approver_version"
        ),
    )
    op.create_index(
        "ix_support_staff_expiry",
        "platform_support_grants",
        ["staff_user_id", "expires_at"],
    )
    op.create_index(
        "ix_support_tenant_created",
        "platform_support_grants",
        ["tenant_id", "created_at"],
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM platform_support_grants")):
        raise RuntimeError(
            "Support authorization evidence must not be destructively downgraded."
        )
    op.drop_index("ix_support_tenant_created", table_name="platform_support_grants")
    op.drop_index("ix_support_staff_expiry", table_name="platform_support_grants")
    op.drop_table("platform_support_grants")
