"""Member API keys and model requests reuse the leased metered worker."""

import sqlalchemy as sa
from alembic import op

revision = "0022_member_api_keys"
down_revision = "0021_session_history_deletion"
branch_labels = None
depends_on = None


def upgrade():
    # A model request is a run without an Agent conversation.
    with op.batch_alter_table("platform_runs") as batch:
        batch.alter_column("session_id", existing_type=sa.String(64), nullable=True)
    from agent_platform.infrastructure.api_key_tables import (
        API_KEY_PRIVILEGES,
        keys,
        requests,
    )

    connection = op.get_bind()
    for table in (keys, requests):
        table.create(connection)
        if connection.dialect.name != "postgresql":
            continue
        op.execute(sa.text(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table.name}" FORCE ROW LEVEL SECURITY'))
        expression = "tenant_id = nullif(current_setting('app.tenant_id', true), '')"
        op.execute(
            sa.text(
                f'CREATE POLICY tenant_isolation ON "{table.name}" USING ({expression}) WITH CHECK ({expression})'
            )
        )
        for service, tables in API_KEY_PRIVILEGES.items():
            role = "platform_" + service
            if connection.scalar(
                sa.text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
            ):
                op.execute(
                    sa.text(
                        f'GRANT {", ".join(tables[table.name])} ON "{table.name}" TO "{role}"'
                    )
                )


def downgrade():
    raise RuntimeError(
        "Preserve revoked credentials and model usage evidence; use a reviewed backup rollback."
    )
