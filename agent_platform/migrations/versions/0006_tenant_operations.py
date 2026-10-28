"""Durable tenant exports and closure.

Revision ID: 0006_tenant_operations
Revises: 0005_tenant_runtime
"""

from alembic import op
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

revision = "0006_tenant_operations"
down_revision = "0005_tenant_runtime"
branch_labels = None
depends_on = None

# Frozen migration metadata, independent of current application imports.
metadata = MetaData()
Table("platform_tenants", metadata, Column("id", String(64), primary_key=True))
Table(
    "platform_memberships",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("user_id", String(64), nullable=False),
    UniqueConstraint("tenant_id", "id", "user_id"),
)

exports = Table(
    "export_jobs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("requested_by", String(64), nullable=False),
    Column("membership_id", String(64), nullable=False),
    Column("membership_version", Integer, nullable=False),
    Column("identity_version", Integer, nullable=False),
    Column("kind", String(24), nullable=False),
    Column("scope", String(16), nullable=False),
    Column("idempotency_key", String(160), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("cutoff", String(40), nullable=False),
    Column("cursor_created_at", String(40)),
    Column("cursor_id", String(128)),
    Column("row_count", Integer, nullable=False),
    Column("chunk_count", Integer, nullable=False),
    Column("owner", String(64)),
    Column("fence", Integer, nullable=False),
    Column("lease_until", Float),
    Column("expires_at", Float, nullable=False),
    Column("error_code", String(80), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "requested_by", "idempotency_key"),
    ForeignKeyConstraint(
        ["tenant_id", "membership_id", "requested_by"],
        [
            "platform_memberships.tenant_id",
            "platform_memberships.id",
            "platform_memberships.user_id",
        ],
    ),
)
artifacts = Table(
    "export_artifacts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("job_id", String(64), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("filename", String(160), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("byte_count", Integer, nullable=False),
    Column("row_count", Integer, nullable=False),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("tenant_id", "job_id", "sequence"),
    ForeignKeyConstraint(
        ["tenant_id", "job_id"], ["export_jobs.tenant_id", "export_jobs.id"]
    ),
)
closures = Table(
    "tenant_lifecycle_jobs",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("id", String(64), nullable=False, unique=True),
    Column("requested_by", String(64), nullable=False),
    Column("reason", Text, nullable=False),
    Column("status", String(24), nullable=False),
    Column("retain_until", Float, nullable=False),
    Column("purge_phase", Integer, nullable=False),
    Column("cursor_id", String(128)),
    Column("owner", String(64)),
    Column("fence", Integer, nullable=False),
    Column("lease_until", Float),
    Column("legacy_revocation_confirmed", Boolean, nullable=False, default=False),
    Column("legacy_key_fingerprint", String(64)),
    Column("tombstone_written_at", String(40)),
    Column("error_code", String(80), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("finished_at", String(40)),
    ForeignKeyConstraint(["tenant_id"], ["platform_tenants.id"]),
)
Index("export_jobs_queue", exports.c.tenant_id, exports.c.status, exports.c.updated_at)
Index("export_jobs_expiry", exports.c.tenant_id, exports.c.expires_at)
Index("tenant_lifecycle_work", closures.c.status, closures.c.updated_at)

TABLES = (exports, artifacts, closures)


def upgrade() -> None:
    connection = op.get_bind()
    for table in TABLES:
        table.create(connection, checkfirst=False)
        if connection.dialect.name == "postgresql":
            name = table.name
            connection.execute(text(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY"))
            connection.execute(text(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY"))
            connection.execute(
                text(
                    f"CREATE POLICY tenant_scope ON {name} "
                    "USING (tenant_id = current_setting('app.tenant_id', true)) "
                    "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
                )
            )


def downgrade() -> None:
    connection = op.get_bind()
    for table in TABLES:
        if connection.scalar(text("SELECT COUNT(*) FROM " + table.name)):
            raise RuntimeError(
                "Operation, export and deletion histories cannot be destructively downgraded."
            )
    for table in reversed(TABLES):
        table.drop(connection, checkfirst=False)
