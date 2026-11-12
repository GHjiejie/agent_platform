"""Retire obsolete agent implementations and their private execution storage."""

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0023_normal_only"
down_revision = "0022_member_api_keys"
branch_labels = None
depends_on = None

# Existing migrations remain immutable so populated deployments can upgrade.
RETIRED_TABLES = (
    "remote_agent_calls",
    "agent_task_leases",
    "execution_checkpoint_writes",
    "execution_checkpoints",
    "execution_journal",
    "collaboration_artifacts",
    "agent_executions",
    "agent_tasks",
    "collaboration_plans",
    "remote_agent_endpoints",
    "remote_agent_versions",
    "remote_agent_credential_secrets",
    "remote_agent_credentials",
)
RETIRED_AGENT_COLUMNS = (
    "execution_mode",
    "capability_keys",
    "task_contracts",
    "collaboration_spec",
    "remote_ref",
)
PROMPT = "你是一位可靠的工作助手。默认用用户使用的语言回答。直接、清晰地回答用户的问题，根据实际需求调整回答长度与格式。区分事实与推断，不捏造数据或已完成的操作。"


def _clean_spec(spec):
    return {
        **{
            key: value
            for key, value in spec.items()
            if key
            not in {
                *RETIRED_AGENT_COLUMNS,
                "team_snapshot",
                "remote_snapshot",
            }
        },
        "name": "normal",
        "builtin_key": "normal",
        "tools": [
            name
            for name in spec.get("tools", [])
            if name in {"calculator", "current_time"}
        ],
    }


def upgrade():
    bind = op.get_bind()
    # Model calls keep their billing and run identity; retired member links go.
    with op.batch_alter_table("platform_calls") as batch:
        batch.drop_constraint("fk_calls_task", type_="foreignkey")
        batch.drop_constraint("fk_calls_execution", type_="foreignkey")
        batch.drop_column("task_id")
        batch.drop_column("execution_id")
    for name in RETIRED_TABLES:
        op.drop_table(name)

    metadata = sa.MetaData()
    agents = sa.Table("platform_agents", metadata, autoload_with=bind)
    versions = sa.Table("platform_agent_versions", metadata, autoload_with=bind)
    sessions = sa.Table("platform_sessions", metadata, autoload_with=bind)
    runs = sa.Table("platform_runs", metadata, autoload_with=bind)
    events = sa.Table("platform_run_events", metadata, autoload_with=bind)
    tenants = sa.Table("platform_tenants", metadata, autoload_with=bind)
    stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
    for tenant_id in bind.scalars(sa.select(tenants.c.id)).all():
        if bind.dialect.name == "postgresql":
            bind.execute(
                sa.text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": tenant_id},
            )
        rows = list(
            bind.execute(
                sa.select(agents)
                .where(agents.c.tenant_id == tenant_id)
                .order_by(agents.c.created_at, agents.c.id)
            ).mappings()
        )
        candidates = [
            row
            for row in rows
            if row["execution_mode"] == "single"
            and not row["remote_ref"]
            and (row["builtin_key"] == "normal" or row["name"] == "normal")
        ]
        candidate = next(
            (row for row in candidates if row["builtin_key"] == "normal"),
            candidates[0] if candidates else None,
        )
        retired = [
            row["id"] for row in rows if not candidate or row["id"] != candidate["id"]
        ]
        # Free the unique built-in key before inserting or adopting normal.
        if retired:
            bind.execute(
                agents.update()
                .where(agents.c.tenant_id == tenant_id, agents.c.id.in_(retired))
                .values(builtin_key=None)
            )
        if candidate:
            normal = _clean_spec(dict(candidate))
            bind.execute(
                agents.update()
                .where(agents.c.tenant_id == tenant_id, agents.c.id == normal["id"])
                .values(name="normal", builtin_key="normal", tools=normal["tools"])
            )
        else:
            normal = {
                "id": uuid4().hex,
                "tenant_id": tenant_id,
                "created_at": stamp,
                "name": "normal",
                "description": "通用聊天助手，直接回答问题并协助处理文字内容。",
                "system_prompt": PROMPT,
                "model_id": None,
                "builtin_key": "normal",
                "temperature": 1,
                "max_steps": 6,
                "max_tokens": 131072,
                "tools": [],
                "execution_policy": {},
                "published_version": 1,
            }
            bind.execute(agents.insert().values(**normal))
        if not normal["published_version"]:
            normal["published_version"] = 1
            bind.execute(
                agents.update()
                .where(agents.c.tenant_id == tenant_id, agents.c.id == normal["id"])
                .values(published_version=1)
            )
        normal_versions = list(
            bind.execute(
                sa.select(versions).where(
                    versions.c.tenant_id == tenant_id,
                    versions.c.agent_id == normal["id"],
                )
            ).mappings()
        )
        for version in normal_versions:
            bind.execute(
                versions.update()
                .where(
                    versions.c.tenant_id == tenant_id,
                    versions.c.agent_id == normal["id"],
                    versions.c.version == version["version"],
                )
                .values(spec=_clean_spec(version["spec"]))
            )
        if not any(
            row["version"] == normal["published_version"] for row in normal_versions
        ):
            bind.execute(
                versions.insert().values(
                    agent_id=normal["id"],
                    version=normal["published_version"],
                    tenant_id=tenant_id,
                    spec=normal,
                    created_at=stamp,
                )
            )
        if not retired:
            continue
        session_ids = sa.select(sessions.c.id).where(
            sessions.c.tenant_id == tenant_id, sessions.c.agent_id.in_(retired)
        )
        for run in bind.execute(
            sa.select(runs).where(
                runs.c.tenant_id == tenant_id,
                runs.c.session_id.in_(session_ids),
                runs.c.status.in_(("queued", "running", "cancelling")),
            )
        ).mappings():
            message = "该 Agent 已移除，请使用 normal 继续对话。"
            bind.execute(
                runs.update()
                .where(runs.c.tenant_id == tenant_id, runs.c.id == run["id"])
                .values(
                    status="cancelled",
                    cancel_requested=True,
                    fence=run["fence"] + 1,
                    lease_until=None,
                    finished_at=stamp,
                    error=message,
                )
            )
            sequence = bind.scalar(
                sa.select(
                    sa.func.coalesce(sa.func.max(events.c.sequence), 0) + 1
                ).where(events.c.tenant_id == tenant_id, events.c.run_id == run["id"])
            )
            bind.execute(
                events.insert().values(
                    tenant_id=tenant_id,
                    run_id=run["id"],
                    sequence=sequence,
                    type="run.cancelled",
                    data={"status": "cancelled", "error": message},
                    created_at=stamp,
                )
            )
        bind.execute(
            sessions.update()
            .where(sessions.c.tenant_id == tenant_id, sessions.c.agent_id.in_(retired))
            .values(agent_id=normal["id"])
        )
        bind.execute(
            versions.delete().where(
                versions.c.tenant_id == tenant_id, versions.c.agent_id.in_(retired)
            )
        )
        bind.execute(
            agents.delete().where(
                agents.c.tenant_id == tenant_id, agents.c.id.in_(retired)
            )
        )
    with op.batch_alter_table("platform_agents") as batch:
        for name in RETIRED_AGENT_COLUMNS:
            batch.drop_column(name)
    with op.batch_alter_table("platform_tenant_settings") as batch:
        batch.drop_column("collaboration_enabled")


def downgrade():
    raise RuntimeError(
        "Retired agent implementations cannot be restored; use a reviewed backup."
    )
