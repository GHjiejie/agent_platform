"""Model-independent built-in agents.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("platform_agents") as batch:
        batch.alter_column("model_id", existing_type=sa.String(64), nullable=True)
        batch.add_column(sa.Column("builtin_key", sa.String(64), nullable=True))
        batch.create_unique_constraint(
            "uq_agent_builtin_tenant", ["tenant_id", "builtin_key"]
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM platform_agents WHERE model_id IS NULL")
    ):
        raise RuntimeError(
            "Assign a model to automatic agents before downgrading; no agents were removed."
        )
    with op.batch_alter_table("platform_agents") as batch:
        batch.drop_constraint("uq_agent_builtin_tenant", type_="unique")
        batch.drop_column("builtin_key")
        batch.alter_column("model_id", existing_type=sa.String(64), nullable=False)
