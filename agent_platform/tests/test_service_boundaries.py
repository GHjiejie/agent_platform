"""Service rollout/health boundaries with offline models and temporary storage."""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from agent_platform.apps.api.main import create_app
from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.runtime_tables import service_heartbeats
from agent_platform.modules.health import (
    platform_status,
    readiness,
    run_service,
    service_instance_id,
)
from agent_platform.modules.operations import OperationsService
from agent_platform.modules.service_contexts import (
    maintenance_services,
    worker_services,
)
from agent_platform.scripts.service_probe import probe

ROOT = Path(__file__).resolve().parents[2]


def migrate(url):
    config = Config(str(ROOT / "agent_platform/alembic.ini"))
    with patch.dict(os.environ, {"PLATFORM_DATABASE_URL": url}):
        command.upgrade(config, "head")


class ServiceReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.settings = Settings(
            database_url=f"sqlite:///{cls.temporary.name}/platform.db",
            export_directory=Path(cls.temporary.name) / "exports",
            web_directory=Path(cls.temporary.name) / "web",
        )
        migrate(cls.settings.database_url)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.db = Database(self.settings.database_url)

    def tearDown(self):
        self.db.close()

    def test_api_ready_does_not_require_other_process_heartbeats(self):
        result = readiness(self.db, self.settings)
        self.assertEqual(result, {"status": "ready", "service": "api"})
        status = platform_status(self.db, self.settings)
        self.assertEqual(status["status"], "degraded")
        self.assertEqual(status["missing_services"], ["maintenance", "worker"])
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(client.get("/api/v1/ready").status_code, 200)
            self.assertEqual(client.get("/api/v1/platform-status").status_code, 401)

    def test_liveness_survives_database_dependency_failure(self):
        app = create_app(self.settings)
        with (
            TestClient(app) as client,
            patch.object(
                app.state.database, "assert_schema", side_effect=RuntimeError("offline")
            ),
        ):
            self.assertEqual(client.get("/api/v1/health").status_code, 200)
            self.assertEqual(client.get("/api/v1/ready").status_code, 503)

    def test_probe_requires_own_live_instance_not_another_replica(self):
        with patch.dict(os.environ, {"PLATFORM_SERVICE_INSTANCE": "other-pod"}):
            other = service_instance_id("worker")
        with self.db.transaction() as connection:
            connection.execute(
                insert(service_heartbeats).values(
                    id=other, role="worker", expires_at=time.time() + 30
                )
            )
        environment = {
            "PLATFORM_DATABASE_URL": self.settings.database_url,
            "PLATFORM_MODE": "local",
            "PLATFORM_SERVICE_INSTANCE": "this-pod",
        }
        with patch.dict(os.environ, environment):
            self.assertFalse(probe("worker"))
            own = service_instance_id("worker")
            with self.db.transaction() as connection:
                connection.execute(
                    insert(service_heartbeats).values(
                        id=own, role="worker", expires_at=time.time() + 30
                    )
                )
            self.assertTrue(probe("worker"))
            with self.db.transaction() as connection:
                connection.execute(
                    update(service_heartbeats)
                    .where(service_heartbeats.c.id == own)
                    .values(expires_at=time.time() - 1)
                )
            self.assertFalse(probe("worker"))
        with self.db.transaction() as connection:
            connection.execute(service_heartbeats.delete())

    def test_export_read_path_never_writes_storage(self):
        service = OperationsService(maintenance_services(self.db, self.settings))
        filename = "a" * 32 + "-1-" + "b" * 32 + ".csv"
        with patch("agent_platform.modules.operations.os.chmod") as chmod:
            path = service._file("tenant1", filename)
            self.assertFalse(path.parent.exists())
            chmod.assert_not_called()
            self.assertEqual(service._file("tenant1", filename, create=True), path)
            self.assertTrue(path.parent.exists())
        with patch("agent_platform.modules.operations.os.chmod") as chmod:
            self.assertEqual(service._file("tenant1", filename), path)
            chmod.assert_not_called()

    def test_background_context_has_only_required_services(self):
        maintenance = maintenance_services(self.db, self.settings)
        self.assertFalse(hasattr(maintenance, "billing"))
        self.assertFalse(hasattr(maintenance, "bootstrap"))
        worker = worker_services(self.db, self.settings)
        self.assertIsNotNone(worker.billing)
        self.assertIsNotNone(worker.identity)
        self.assertIsNotNone(worker.gateway)
        self.assertFalse(hasattr(worker, "login"))


class ServiceConfigurationTests(unittest.TestCase):
    def test_saas_dependencies_are_checked_per_role(self):
        base = {
            "mode": "saas",
            "database_url": "postgresql://runtime@database/platform",
            "secret_encryption_key": Fernet.generate_key().decode(),
        }
        for role in ("maintenance", "gateway-sync"):
            settings = Settings(service_role=role, **base)
            with self.assertRaisesRegex(ValueError, "API configuration"):
                create_app(settings)
        Settings(service_role="worker", redis_url="redis://redis/0", **base)
        with self.assertRaisesRegex(ValueError, "PLATFORM_REDIS_URL"):
            Settings(service_role="worker", **base)
        with self.assertRaisesRegex(ValueError, "secure cookies"):
            Settings(service_role="api", redis_url="redis://redis/0", **base)
        Settings(
            service_role="api",
            redis_url="redis://redis/0",
            secure_cookies=True,
            **base,
        )

    def test_background_imports_do_not_load_http_facade(self):
        script = (
            "import agent_platform.__main__; "
            "import agent_platform.apps.worker.main; "
            "import agent_platform.apps.gateway_sync.main; "
            "import agent_platform.apps.maintenance.main; "
            "import sys; "
            "assert 'agent_platform.modules.platform' not in sys.modules; "
            "assert 'agent_platform.apps.api.main' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class WorkerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_stops_claims_and_drains_existing_run(self):
        worker = object.__new__(Worker)
        worker.settings = SimpleNamespace(
            worker_concurrency=1, worker_poll_seconds=0.01
        )
        worker.recover = Mock()
        worker.claim = Mock(return_value={"id": "offline-run"})
        started, finish = asyncio.Event(), asyncio.Event()

        async def execute(_run):
            started.set()
            await finish.wait()

        worker.execute = execute
        stop = asyncio.Event()
        task = asyncio.create_task(worker.serve(stop))
        await asyncio.wait_for(started.wait(), timeout=2)
        stop.set()
        count = worker.claim.call_count
        await asyncio.sleep(0.03)
        self.assertFalse(task.done())
        self.assertEqual(worker.claim.call_count, count)
        finish.set()
        await asyncio.wait_for(task, timeout=2)

    async def test_heartbeat_does_not_cancel_graceful_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            url = f"sqlite:///{folder}/platform.db"
            migrate(url)
            db = Database(url)
            stop, draining, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
            stop.set()

            async def service(signal):
                await signal.wait()
                draining.set()
                await finish.wait()

            try:
                with patch.dict(os.environ, {"PLATFORM_SERVICE_INSTANCE": "draining"}):
                    task = asyncio.create_task(run_service(db, "worker", service, stop))
                    await asyncio.wait_for(draining.wait(), timeout=2)
                    await asyncio.sleep(0.03)
                    self.assertFalse(task.done())
                    finish.set()
                    await asyncio.wait_for(task, timeout=2)
                    with db.read() as connection:
                        self.assertIsNone(
                            connection.scalar(service_heartbeats.select())
                        )
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
