"""Upgrade populated legacy agents without losing conversations or model charges."""

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, insert, inspect, select, update

from agent_platform.apps.api.responses import SessionDetailResponse
from agent_platform.apps.api.schemas import AgentCreate, AgentPatch
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.modules.runtime.contracts import validate_run_snapshot_version
from agent_platform.tests.fixtures import MultiTenantFixture


class NormalOnlyTests(unittest.TestCase):
    def test_removed_modes_and_tools_are_rejected_at_request_and_runtime_boundaries(
        self,
    ):
        for values in (
            {"execution_mode": "team"},
            {"collaboration_spec": {"members": []}},
            {"remote_ref": {"registration_id": "old"}},
            {"tools": ["generate_sandboxed_ui"]},
        ):
            with self.subTest(values=values):
                for model in (AgentCreate, AgentPatch):
                    with self.assertRaises(ValueError):
                        model.model_validate({"name": "normal", **values})
        for spec in (
            {"snapshot_schema_version": 3},
            {"snapshot_schema_version": 2, "execution_mode": "team"},
            {"remote_ref": {"registration_id": "old"}},
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                validate_run_snapshot_version(spec)

    def test_upgrade_removes_all_other_agents_and_storage_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assert_upgrade(f"sqlite:///{Path(directory) / 'legacy.db'}")

    def assert_upgrade(self, url):
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with patch.dict(os.environ, {"PLATFORM_DATABASE_URL": url}):
            command.upgrade(config, "0022_member_api_keys")
            db = Database(url)
            try:
                fixture = MultiTenantFixture(db, Settings(database_url=url))
                owner, tenant_id = fixture.owner, fixture.tenant_id
                platform = fixture.platform
                with db.read() as connection:
                    normal_id = connection.scalar(
                        select(t.agents.c.id).where(
                            t.agents.c.tenant_id == tenant_id,
                            t.agents.c.builtin_key == "normal",
                        )
                    )
                platform.save_agent(
                    owner, {"system_prompt": "Keep my normal instructions"}, normal_id
                )
                platform.publish(owner, normal_id)
                model = fixture.create_model()
                old_ids = []
                for name in (
                    "整理分析图表团队",
                    "图表成员",
                    "数据分析成员",
                    "数据整理成员",
                    "可视化界面助手",
                ):
                    old = platform.save_agent(
                        owner, AgentCreate(name=name).model_dump()
                    )
                    platform.publish(owner, old["id"])
                    old_ids.append(old["id"])
                session = platform.create_session(
                    owner, old_ids[0], "Keep my conversation"
                )
                snapshot = {
                    "snapshot_schema_version": 3,
                    "execution_mode": "team",
                    "name": "旧团队",
                }
                with db.transaction(f"tenant:{tenant_id}") as connection:
                    connection.execute(
                        insert(t.runs).values(
                            id="old-run",
                            tenant_id=tenant_id,
                            user_id=owner["id"],
                            session_id=session["id"],
                            created_at=session["created_at"],
                            agent_name="旧团队",
                            spec=snapshot,
                            message="Keep my question",
                            status="running",
                            error="",
                            idempotency_key="old-run",
                            request_hash="f" * 64,
                            owner="stale-worker",
                            fence=7,
                        )
                    )
                    connection.execute(
                        insert(t.messages).values(
                            id="old-message",
                            tenant_id=tenant_id,
                            session_id=session["id"],
                            run_id="old-run",
                            created_at=session["created_at"],
                            role="user",
                            content="Keep my question",
                        )
                    )
                    connection.execute(
                        insert(t.calls).values(
                            id="old-call",
                            tenant_id=tenant_id,
                            user_id=owner["id"],
                            run_id="old-run",
                            model_id=model["id"],
                            model=model["alias"],
                            agent_name="旧团队",
                            user_email=owner["email"],
                            created_at=session["created_at"],
                            status="confirmed",
                            input_tokens=10,
                            output_tokens=5,
                            cost="0.000025",
                            price_version=1,
                        )
                    )
                    # Exercise both member-version FKs and call attribution FKs.
                    metadata = MetaData()
                    plans = Table(
                        "collaboration_plans", metadata, autoload_with=connection
                    )
                    tasks = Table("agent_tasks", metadata, autoload_with=connection)
                    executions = Table(
                        "agent_executions", metadata, autoload_with=connection
                    )
                    calls = Table("platform_calls", metadata, autoload_with=connection)
                    base = {
                        "tenant_id": tenant_id,
                        "root_run_id": "old-run",
                        "created_at": session["created_at"],
                    }
                    connection.execute(
                        plans.insert().values(
                            **base, id="plan", revision=1, plan={}, digest="d"
                        )
                    )
                    connection.execute(
                        tasks.insert().values(
                            **base,
                            id="task",
                            plan_id="plan",
                            logical_key="task",
                            member_key="member",
                            agent_id=old_ids[1],
                            agent_version=1,
                            task_type="analysis",
                            objective="old",
                            dependencies=[],
                            required=True,
                            status="running",
                            revision=0,
                            error="",
                        )
                    )
                    connection.execute(
                        executions.insert().values(
                            **base,
                            id="execution",
                            task_id="task",
                            attempt=1,
                            role="member",
                            agent_id=old_ids[1],
                            agent_version=1,
                            spec={},
                            input={},
                            input_digest="d",
                            status="running",
                            error="",
                            fence=1,
                        )
                    )
                    connection.execute(
                        calls.update()
                        .where(calls.c.id == "old-call")
                        .values(
                            task_id="task",
                            execution_id="execution",
                        )
                    )
                    # Existing ordinary normal agents are adopted during upgrade.
                    connection.execute(
                        update(t.agents)
                        .where(t.agents.c.id == normal_id)
                        .values(builtin_key=None)
                    )
                command.upgrade(config, "head")
                command.upgrade(config, "head")
                migration = importlib.import_module(
                    "agent_platform.migrations.versions.0023_normal_only"
                )
                with db.read() as connection:
                    inspector = inspect(connection)
                    self.assertFalse(
                        set(migration.RETIRED_TABLES) & set(inspector.get_table_names())
                    )
                    self.assertFalse(
                        set(migration.RETIRED_AGENT_COLUMNS)
                        & {
                            column["name"]
                            for column in inspector.get_columns("platform_agents")
                        }
                    )
                    agents = list(
                        connection.execute(
                            select(t.agents).where(t.agents.c.tenant_id == tenant_id)
                        ).mappings()
                    )
                    self.assertEqual([agent["id"] for agent in agents], [normal_id])
                    self.assertEqual(
                        agents[0]["system_prompt"], "Keep my normal instructions"
                    )
                    self.assertEqual(
                        connection.scalar(
                            select(t.sessions.c.agent_id).where(
                                t.sessions.c.id == session["id"]
                            )
                        ),
                        normal_id,
                    )
                    self.assertEqual(
                        connection.scalar(
                            select(t.messages.c.content).where(
                                t.messages.c.id == "old-message"
                            )
                        ),
                        "Keep my question",
                    )
                    run = (
                        connection.execute(
                            select(t.runs).where(t.runs.c.id == "old-run")
                        )
                        .mappings()
                        .one()
                    )
                    self.assertEqual((run["status"], run["fence"]), ("cancelled", 8))
                    self.assertEqual(run["spec"], snapshot)
                    self.assertEqual(
                        connection.scalar(
                            select(t.calls.c.cost).where(t.calls.c.id == "old-call")
                        ),
                        "0.000025",
                    )
                    if db.sqlite:
                        self.assertEqual(
                            connection.exec_driver_sql(
                                "PRAGMA foreign_key_check"
                            ).all(),
                            [],
                        )
                detail = SessionDetailResponse.model_validate(
                    platform.session_detail(owner, session["id"])
                )
                self.assertEqual(detail.runs[0].execution_mode, "single")
                self.assertEqual(detail.messages[0].content, "Keep my question")
            finally:
                db.close()
