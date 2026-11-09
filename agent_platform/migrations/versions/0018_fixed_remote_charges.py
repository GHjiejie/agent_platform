"""Explicit remote amount charges and supplier cost bounds, without fake tokens."""

import sqlalchemy as sa
from alembic import op

revision = "0018_fixed_remote_charges"
down_revision = "0017_remote_agent_registry"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "billing_reservations",
        sa.Column(
            "charge_kind",
            sa.String(16),
            nullable=False,
            server_default="tokens",
        ),
    )
    op.add_column("billing_reservations", sa.Column("quote_id", sa.String(64)))
    with op.batch_alter_table("platform_cost_reservations") as batch:
        batch.add_column(
            sa.Column(
                "charge_kind", sa.String(16), nullable=False, server_default="tokens"
            )
        )
        batch.alter_column("deployment_id", existing_type=sa.String(64), nullable=True)
        batch.alter_column(
            "price_version_id", existing_type=sa.String(64), nullable=True
        )


def downgrade():
    raise RuntimeError("Fixed monetary evidence requires a reviewed backup rollback.")
