"""Expose complete/partial delivery independently from root lifecycle status."""

import sqlalchemy as sa
from alembic import op

revision = "0013_collaboration_outcome"
down_revision = "0012_agent_registry"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("platform_runs", sa.Column("outcome", sa.String(16)))


def downgrade():
    raise RuntimeError(
        "Preserve historical delivery outcomes; drain and restore a backup."
    )
