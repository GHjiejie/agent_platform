"""Exercise the populated SQLite upgrade, including references into agents."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from agent_platform.infrastructure.db import Database


class AgentMigrationTest(unittest.TestCase):
    def test_upgrade_preserves_existing_agent_versions_and_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{Path(directory) / 'platform.db'}"
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            with patch.dict(os.environ, {"PLATFORM_DATABASE_URL": url}):
                command.upgrade(config, "0001")
                db = Database(url)
                spec = {
                    "model_id": "model",
                    "system_prompt": "Keep the published prompt",
                }
                with db.transaction() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO platform_tenants VALUES ('tenant', 'Existing tenant', 'stamp')"
                        )
                    )
                    conn.execute(
                        text(
                            "INSERT INTO platform_users (id,tenant_id,created_at,email,name,password_hash,role,active) VALUES ('user','tenant','stamp','old@example.test','Existing user','hash','admin',1)"
                        )
                    )
                    conn.execute(
                        text(
                            "INSERT INTO platform_models VALUES ('model','tenant','stamp','Existing model','original-model',1,'1','3',8192,1024,1)"
                        )
                    )
                    conn.execute(
                        text(
                            "INSERT INTO platform_agents VALUES ('agent','tenant','stamp','Existing agent','','Keep prompt','model',1,6,1024,'[]',1)"
                        )
                    )
                    conn.execute(
                        text(
                            "INSERT INTO platform_agent_versions VALUES ('agent',1,'tenant',:spec,'stamp')"
                        ),
                        {"spec": json.dumps(spec)},
                    )
                    conn.execute(
                        text(
                            "INSERT INTO platform_sessions VALUES ('session','tenant','stamp','user','agent','Keep conversation')"
                        )
                    )
                command.upgrade(config, "head")
                command.upgrade(config, "head")
                with db.read() as conn:
                    self.assertEqual(
                        conn.exec_driver_sql("PRAGMA foreign_key_check").all(), []
                    )
                    self.assertEqual(
                        conn.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1
                    )
                    agent = conn.execute(
                        text("SELECT model_id,builtin_key,name FROM platform_agents")
                    ).one()
                    self.assertEqual(tuple(agent), ("model", None, "Existing agent"))
                    self.assertEqual(
                        json.loads(
                            conn.scalar(
                                text("SELECT spec FROM platform_agent_versions")
                            )
                        ),
                        spec,
                    )
                    self.assertEqual(
                        conn.scalar(text("SELECT agent_id FROM platform_sessions")),
                        "agent",
                    )
                    model_column = next(
                        c
                        for c in inspect(conn).get_columns("platform_agents")
                        if c["name"] == "model_id"
                    )
                    self.assertTrue(model_column["nullable"])
                    self.assertEqual(
                        conn.scalar(text("SELECT version_num FROM alembic_version")),
                        ScriptDirectory.from_config(config).get_current_head(),
                    )
                db.close()
