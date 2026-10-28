"""Freeze queue expiry independently of the execution deadline.

Existing runs retain the former PLATFORM_RUN_TIMEOUT queue policy at upgrade.
PostgreSQL backfill selects each tenant explicitly under the existing RLS.
"""

import math
import os

import sqlalchemy as sa
from alembic import op

revision = "0008_execution_deadlines"
down_revision = "0007_support_access"
branch_labels = None
depends_on = None


def upgrade():
    legacy_timeout = float(os.getenv("PLATFORM_RUN_TIMEOUT", "300"))
    if legacy_timeout == 0:
        # New installations can disable execution deadlines. Queue admission
        # remains bounded independently of that setting.
        legacy_timeout = float(os.getenv("PLATFORM_QUEUE_TIMEOUT", "300"))
    if not math.isfinite(legacy_timeout) or legacy_timeout <= 0:
        raise ValueError("PLATFORM_RUN_TIMEOUT must be finite and positive")
    op.add_column("platform_runs", sa.Column("queue_expires_at", sa.Float()))
    if op.get_bind().dialect.name == "postgresql":
        # A migration owner can still be subject to FORCE RLS. Bind each tenant
        # rather than disabling isolation or silently skipping historical rows.
        op.execute(
            sa.text(
                f"""
                DO $deadline_backfill$
                DECLARE tenant_scope text;
                BEGIN
                    FOR tenant_scope IN SELECT id FROM platform_tenants LOOP
                        PERFORM set_config('app.tenant_id', tenant_scope, true);
                        UPDATE platform_runs
                        SET queue_expires_at = EXTRACT(EPOCH FROM created_at::timestamptz)
                            + {legacy_timeout}
                        WHERE tenant_id = tenant_scope AND queue_expires_at IS NULL;
                    END LOOP;
                END
                $deadline_backfill$;
                """
            )
        )
    else:
        op.execute(
            sa.text(
                "UPDATE platform_runs SET queue_expires_at = "
                "ROUND((julianday(created_at) - 2440587.5) * 86400, 6) "
                f"+ {legacy_timeout} WHERE queue_expires_at IS NULL"
            )
        )


def downgrade():
    op.drop_column("platform_runs", "queue_expires_at")
