"""Tenant model grants and gateway control-plane state; no plaintext secrets."""

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from agent_platform.infrastructure.db import metadata
from agent_platform.modules.billing.service import ExactMoney


def identity():
    return [
        Column("id", String(64), primary_key=True),
        Column("created_at", String(40), nullable=False),
    ]


deployments = Table(
    "model_deployments",
    metadata,
    *identity(),
    Column("name", String(120), nullable=False),
    Column("owner_scope", String(16), nullable=False),
    Column("owner_tenant_id", String(64)),
    Column("gateway_id", String(64), nullable=False),
    Column("internal_route", String(200), nullable=False),
    Column("base_url", String(500), nullable=False),
    Column("protocol", String(40), nullable=False),
    Column("capabilities", JSON, nullable=False),
    Column("status", String(24), nullable=False),
    Column("config_version", Integer, nullable=False),
    UniqueConstraint("gateway_id", "internal_route"),
    CheckConstraint(
        "(owner_scope = 'platform' AND owner_tenant_id IS NULL) OR "
        "(owner_scope = 'tenant' AND owner_tenant_id IS NOT NULL)",
        name="gateway_deployment_owner",
    ),
)
model_bindings = Table(
    "tenant_model_bindings",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("model_id", String(64), primary_key=True),
    Column("deployment_id", String(64), nullable=False),
    Column("price_version_id", String(64), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("policy_version", Integer, nullable=False),
    Column("created_at", String(40), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "model_id"], ["platform_models.tenant_id", "platform_models.id"]
    ),
    ForeignKeyConstraint(["deployment_id"], ["model_deployments.id"]),
    ForeignKeyConstraint(
        ["tenant_id", "price_version_id"],
        ["model_price_versions.tenant_id", "model_price_versions.id"],
    ),
)
prices = Table(
    "model_price_versions",
    metadata,
    *identity(),
    Column("tenant_id", String(64), nullable=False),
    Column("model_id", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("unit", String(32), nullable=False),
    Column("input_price", ExactMoney(), nullable=False),
    Column("output_price", ExactMoney(), nullable=False),
    Column("created_by", String(64), nullable=False),
    UniqueConstraint("tenant_id", "model_id", "version"),
    UniqueConstraint("tenant_id", "id"),
    ForeignKeyConstraint(
        ["tenant_id", "model_id"], ["platform_models.tenant_id", "platform_models.id"]
    ),
)
bindings = Table(
    "gateway_bindings",
    metadata,
    *identity(),
    Column("tenant_id", String(64), nullable=False),
    Column("gateway_id", String(64), nullable=False),
    Column("purpose", String(32), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("active_credential_version_id", String(64)),
    Column("desired_version", Integer, nullable=False),
    Column("applied_version", Integer, nullable=False),
    Column("status", String(24), nullable=False),
    Column("limits", JSON, nullable=False),
    Column("error_code", String(80), nullable=False, default=""),
    UniqueConstraint("tenant_id", "gateway_id", "purpose"),
    UniqueConstraint("tenant_id", "id"),
)
credentials = Table(
    "gateway_credential_versions",
    metadata,
    *identity(),
    Column("tenant_id", String(64), nullable=False),
    Column("binding_id", String(64), nullable=False),
    Column("generation", Integer, nullable=False),
    Column("external_user_id", String(120), nullable=False),
    Column("external_key_id", String(128), nullable=False),
    Column("key_alias", String(160), nullable=False),
    Column("secret_ciphertext", Text),
    Column("fingerprint", String(64), nullable=False),
    Column("routes", JSON, nullable=False),
    Column("limits", JSON, nullable=False),
    Column("status", String(24), nullable=False),
    Column("expires_at", String(40)),
    Column("activated_at", String(40)),
    Column("retired_at", String(40)),
    Column("revoked_at", String(40)),
    UniqueConstraint("binding_id", "generation"),
    UniqueConstraint("tenant_id", "id"),
    ForeignKeyConstraint(
        ["tenant_id", "binding_id"],
        ["gateway_bindings.tenant_id", "gateway_bindings.id"],
    ),
)
outbox = Table(
    "gateway_outbox",
    metadata,
    *identity(),
    Column("tenant_id", String(64), nullable=False),
    Column("binding_id", String(64), nullable=False),
    Column("credential_version_id", String(64), nullable=False),
    Column("config_version", Integer, nullable=False),
    Column("action", String(24), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("status", String(24), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("next_attempt_at", Float, nullable=False),
    Column("owner", String(64)),
    Column("fence", Integer, nullable=False),
    Column("lease_until", Float),
    Column("external_resource_id", String(128), nullable=False),
    Column("error_code", String(80), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("binding_id", "config_version", "action", "credential_version_id"),
    ForeignKeyConstraint(
        ["tenant_id", "credential_version_id"],
        ["gateway_credential_versions.tenant_id", "gateway_credential_versions.id"],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "binding_id"],
        ["gateway_bindings.tenant_id", "gateway_bindings.id"],
    ),
)
audits = Table(
    "gateway_control_audits",
    metadata,
    *identity(),
    Column("tenant_id", String(64), nullable=False),
    Column("actor_id", String(64), nullable=False),
    Column("action", String(80), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("details", JSON, nullable=False),
)
Index("gateway_outbox_poll", outbox.c.status, outbox.c.next_attempt_at)
Index("gateway_outbox_binding", outbox.c.binding_id, outbox.c.status)

TABLES = (deployments, prices, model_bindings, bindings, credentials, outbox, audits)
