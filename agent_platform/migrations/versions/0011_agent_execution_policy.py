"""Persist editable Agent execution policy; published snapshots remain immutable."""

import sqlalchemy as sa
from alembic import op

revision = "0011_agent_execution_policy"
down_revision = "0010_service_permissions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "platform_agents",
        sa.Column(
            "execution_policy",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade():
    op.drop_column("platform_agents", "execution_policy")
