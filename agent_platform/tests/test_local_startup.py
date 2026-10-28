"""Focused local lifecycle checks; no real model services are contacted."""

import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, MetaData, Table, inspect, text

from agent_platform.infrastructure.db import Database
from agent_platform.scripts.local import (
    Supervisor,
    check_port,
    load_runtime_environment,
    managed_state,
    prepare_config,
    setup_database,
)


class LocalStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.url = f"sqlite:///{self.root / 'platform.db'}"

    def tearDown(self):
        self.temporary.cleanup()

    def test_fresh_config_is_private_random_and_existing_config_unchanged(self):
        path = self.root / ".env"
        self.assertTrue(prepare_config(path))
        contents = path.read_text()
        password = next(
            line.split("=", 1)[1]
            for line in contents.splitlines()
            if line.startswith("PLATFORM_ADMIN_PASSWORD=")
        )
        self.assertGreaterEqual(len(password), 24)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(prepare_config(path))
        self.assertEqual(contents, path.read_text())
        path.write_text("PLATFORM_ADMIN_PASSWORD=\n# preserve me\n")
        self.assertFalse(prepare_config(path))
        self.assertEqual(path.read_text(), "PLATFORM_ADMIN_PASSWORD=\n# preserve me\n")

    def test_start_preserves_all_shared_dependency_groups(self):
        source = Path(__file__).resolve().parents[1] / "scripts/local.py"
        self.assertIn('["uv", "sync", "--frozen", "--all-groups"]', source.read_text())
        self.assertTrue(hasattr(Supervisor, "start"))

    def test_root_upstream_credentials_stay_available_only_for_managed_gateway(self):
        root_env = self.root / "root.env"
        platform_env = self.root / "platform.env"
        root_env.write_text(
            "OPENAI_BASE_URL=https://gateway.example/v1\n"
            "OPENAI_API_KEY=root-secret\n"
            "MODEL=root-model\n"
        )
        platform_env.write_text(
            "PLATFORM_ADMIN_EMAIL=owner@example.com\n"
            "PLATFORM_LITELLM_URL=\n"
            "PLATFORM_LITELLM_KEY=\n"
        )

        env = load_runtime_environment(platform_env, root_env, {})

        self.assertEqual(env["PLATFORM_LITELLM_URL"], "")
        self.assertEqual(env["OPENAI_BASE_URL"], "https://gateway.example/v1")
        self.assertEqual(env["PLATFORM_LITELLM_KEY"], "")
        self.assertEqual(env["OPENAI_API_KEY"], "root-secret")
        self.assertEqual(env["PLATFORM_DEFAULT_MODEL"], "root-model")
        self.assertNotIn("root-secret", platform_env.read_text())

    def test_platform_and_process_gateway_settings_override_root_fallback(self):
        root_env = self.root / "root.env"
        platform_env = self.root / "platform.env"
        root_env.write_text(
            "OPENAI_BASE_URL=https://root.example/v1\nOPENAI_API_KEY=root-key\n"
        )
        platform_env.write_text(
            "PLATFORM_LITELLM_URL=https://platform.example/v1\n"
            "PLATFORM_LITELLM_KEY=platform-key\n"
        )

        env = load_runtime_environment(
            platform_env,
            root_env,
            {"PLATFORM_LITELLM_KEY": "process-key"},
        )

        self.assertEqual(env["PLATFORM_LITELLM_URL"], "https://platform.example/v1")
        self.assertEqual(env["PLATFORM_LITELLM_KEY"], "process-key")

    def test_occupied_port_is_rejected_without_touching_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError, "PORT="):
                check_port("127.0.0.1", port)
            self.assertEqual(listener.getsockname()[1], port)

    def test_process_state_must_match_owned_identity(self):
        file = self.root / "process.json"
        file.write_text(
            json.dumps({"pid": 12345, "identity": "yesterday python unrelated"})
        )
        with patch(
            "agent_platform.scripts.local.identity",
            return_value="yesterday python unrelated",
        ):
            self.assertIsNone(managed_state(self.root))
        owned = "today python -m agent_platform.scripts.local start --port 18000"
        file.write_text(json.dumps({"pid": 12345, "identity": owned}))
        with patch("agent_platform.scripts.local.identity", return_value=owned):
            self.assertEqual(managed_state(self.root)["pid"], 12345)
        with patch(
            "agent_platform.scripts.local.identity", return_value="tomorrow " + owned
        ):
            self.assertIsNone(managed_state(self.root))

    def test_fresh_database_migrates_and_repeated_setup_is_safe(self):
        with patch.dict(os.environ, {"PLATFORM_DATABASE_URL": self.url}):
            setup_database()
            setup_database()
        db = Database(self.url)
        try:
            with db.read() as connection:
                self.assertIn("platform_users", inspect(connection).get_table_names())
                self.assertEqual(
                    connection.scalar(text("SELECT version_num FROM alembic_version")),
                    ScriptDirectory.from_config(
                        Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
                    ).get_current_head(),
                )
        finally:
            db.close()

    def test_matching_legacy_schema_is_adopted_without_losing_data(self):
        db = Database(self.url)
        db.create_schema()
        try:
            with db.transaction() as connection:
                connection.execute(
                    text(
                        "INSERT INTO platform_tenants (id,name,created_at) VALUES ('saved','Keep me','2026-09-22')"
                    )
                )
            with patch.dict(os.environ, {"PLATFORM_DATABASE_URL": self.url}):
                setup_database()
            with db.read() as connection:
                self.assertEqual(
                    connection.scalar(
                        text("SELECT name FROM platform_tenants WHERE id='saved'")
                    ),
                    "Keep me",
                )
                self.assertEqual(
                    connection.scalar(text("SELECT version_num FROM alembic_version")),
                    ScriptDirectory.from_config(
                        Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
                    ).get_current_head(),
                )
        finally:
            db.close()

    def test_partial_legacy_schema_is_rejected_without_patching(self):
        db = Database(self.url)
        try:
            with db.transaction() as connection:
                connection.execute(
                    text("CREATE TABLE platform_users (id TEXT PRIMARY KEY)")
                )
            with (
                patch.dict(os.environ, {"PLATFORM_DATABASE_URL": self.url}),
                self.assertRaisesRegex(RuntimeError, "结构不匹配"),
            ):
                setup_database()
            with db.read() as connection:
                self.assertEqual(
                    inspect(connection).get_table_names(), ["platform_users"]
                )
        finally:
            db.close()

    def test_legacy_missing_primary_key_is_not_stamped(self):
        from agent_platform.infrastructure.tables import run_events

        db = Database(self.url)
        db.create_schema()
        try:
            with db.transaction() as connection:
                connection.execute(text("DROP TABLE platform_run_events"))
                damaged = Table(
                    "platform_run_events",
                    MetaData(),
                    *[
                        Column(column.name, column.type, nullable=column.nullable)
                        for column in run_events.columns
                    ],
                )
                damaged.create(connection)
            with (
                patch.dict(os.environ, {"PLATFORM_DATABASE_URL": self.url}),
                self.assertRaisesRegex(RuntimeError, "结构不匹配"),
            ):
                setup_database()
            with db.read() as connection:
                self.assertNotIn(
                    "alembic_version", inspect(connection).get_table_names()
                )
        finally:
            db.close()
