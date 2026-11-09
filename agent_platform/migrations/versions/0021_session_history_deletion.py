"""Remove deleted conversations from history while retaining execution records."""

import sqlalchemy as sa
from alembic import op

revision = "0021_session_history_deletion"
down_revision = "0020_provider_catalog"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("platform_sessions", sa.Column("deleted_at", sa.String(40)))
    op.create_index(
        "ix_sessions_visible_history",
        "platform_sessions",
        ["tenant_id", "user_id", "deleted_at", "created_at", "id"],
    )


def downgrade():
    raise RuntimeError(
        "Preserve conversation deletion markers; drain and restore a backup."
    )
