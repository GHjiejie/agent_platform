"""Execution deadlines and SSE quotas using current multi-tenant services."""

import asyncio
import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import delete, insert, select, text, update

from agent_platform.apps.api.main import COOKIE, LeasedStreamingResponse, create_app
from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import runtime_tables as rt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.gateway_control import ALLOWED_ROUTES, GatewayService
from agent_platform.modules.platform import Platform, now, uid
from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply


class OfflineControl:
    """Replace external gateway I/O while running its real enrollment state machine."""

    def __init__(self):
        self.users, self.keys = {}, {}

    def user_info(self, identifier):
        return self.users.get(identifier)

    def create_user(self, identifier, routes):
        self.users[identifier] = {
            "user_id": identifier,
            "user_role": "internal_user",
            "models": routes,
        }

    def key_info(self, identifier):
        return self.keys.get(identifier)

    def create_key(self, credential, _key):
        self.keys[credential["external_key_id"]] = {
            "user_id": credential["external_user_id"],
            "models": credential["routes"],
            "allowed_routes": ALLOWED_ROUTES,
            **credential["limits"],
            "spend": 0,
            "expires": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }

    def delete_key(self, identifier):
        self.keys.pop(identifier, None)


class OfflineInference:
    def __init__(self, *, hang=False, after_result=None):
        self.calls = 0
        self.hang = hang
        self.after_result = after_result
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def stream(self, _messages, _model, _max_tokens, _temperature, _tools):
        self.calls += 1
        self.started.set()
        try:
            yield GatewayEvent("text", {"text": "Offline answer"})
            if self.hang:
                await asyncio.Event().wait()
            yield GatewayEvent(
                "result",
                ModelReply(
                    "Offline answer",
                    [],
                    20,
                    5,
                    {"prompt_tokens": 20, "completion_tokens": 5},
                ),
            )
            if self.after_result:
                await self.after_result()
        finally:
            self.closed.set()


class ExecutionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="execution-lifecycle-")
        self.directory = Path(self.temporary.name)
        self.settings = Settings(
            database_url=f"sqlite:///{self.directory / 'platform.db'}",
            admin_email="execution-operator@example.test",
            admin_password="execution-test-password",
            operator_emails=("execution-operator@example.test",),
            secret_encryption_key=Fernet.generate_key().decode(),
            litellm_url="http://127.0.0.1:4000",
            queue_timeout=30,
            run_timeout=5,
            tombstone_path=self.directory / "tombstones.jsonl",
            export_directory=self.directory / "exports",
            web_directory=self.directory / "unused-web",
        )
        self.config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with patch.dict(
            os.environ, {"PLATFORM_DATABASE_URL": self.settings.database_url}
        ):
            command.upgrade(self.config, "head")
        self.app = create_app(self.settings)
        self.platform = self.app.state.platform
        self.db = self.app.state.database
        self.platform.bootstrap()
        self.login = self.platform.login(
            self.settings.admin_email, self.settings.admin_password, "offline"
        )
        self.operator = self.login["user"]
        self.platform.gateway = GatewayService(
            self.db, self.settings, adapter=OfflineControl()
        )
        deployment = self.platform.gateway.create_deployment(
            {
                "name": "Offline text model",
                "internal_route": "offline-text",
                "capabilities": {"text": True, "tools": True},
            },
            actor_id=self.operator["id"],
        )
        self.contexts, self.agents = [], []
        for name in ("Execution tenant A", "Execution tenant B"):
            tenant = self.platform.identity.create_tenant(
                self.operator,
                name=name,
                owner_user_id=self.operator["id"],
                idempotency_key=uid(),
                limits={"max_sse_connections": 1},
            )
            context = self.platform.identity.context(self.operator["id"], tenant["id"])
            model = self.platform.gateway.grant_model(
                tenant["id"],
                deployment["id"],
                {
                    "name": "Offline model",
                    "alias": "offline-text",
                    "input_price": "1",
                    "output_price": "2",
                    "context_window": 32000,
                    "max_output_tokens": 256,
                    "active": True,
                },
                actor_id=self.operator["id"],
            )
            self.platform.gateway.enroll(tenant["id"], actor_id=self.operator["id"])
            while self.platform.gateway.process_one():
                pass
            self.platform.gateway.resolve(tenant["id"], model)
            agent = self.platform.save_agent(
                context,
                {
                    "name": "Execution test agent",
                    "description": "Offline lifecycle test",
                    "system_prompt": "Answer briefly.",
                    "model_id": model["id"],
                    "temperature": 0,
                    "max_steps": 2,
                    "max_tokens": 64,
                    "tools": [],
                },
            )
            self.platform.publish(context, agent["id"])
            self.platform.billing.credit(
                tenant["id"], self.operator["id"], "10", "Offline credit", uid()
            )
            self.contexts.append(context)
            self.agents.append(agent)
        self.user, self.other_user = self.contexts

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def enqueue(self, *, tenant=0):
        user, agent = self.contexts[tenant], self.agents[tenant]
        with self.db.tenant_scope(user["tenant_id"]):
            session = self.platform.create_session(user, agent["id"], None)
            return self.platform.enqueue(user, session["id"], "Offline question", uid())

    def stored(self, run_id):
        with self.db.tenant_scope(self.user["tenant_id"]), self.db.read() as connection:
            return dict(
                connection.execute(select(t.runs).where(t.runs.c.id == run_id))
                .mappings()
                .one()
            )

    def change_run(self, run_id, **changes):
        with self.db.transaction(f"tenant:{self.user['tenant_id']}") as connection:
            connection.execute(
                update(t.runs).where(t.runs.c.id == run_id).values(**changes)
            )

    async def test_claim_closes_expired_queue_and_advances_to_valid_run(self):
        expired = self.enqueue()
        valid = self.enqueue()
        self.change_run(expired["id"], queue_expires_at=time.time() - 1)
        gateway = OfflineInference()
        worker = Worker(self.platform, gateway=gateway)
        claimed = worker.claim()
        self.assertEqual(claimed["id"], valid["id"])
        self.assertEqual(self.stored(expired["id"])["status"], "expired")
        with self.db.read() as connection:
            self.assertEqual(connection.scalar(select(t.calls.c.id)), None)
        await worker.execute(claimed)
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(self.stored(valid["id"])["status"], "succeeded")

    async def test_frozen_queue_expiry_survives_settings_changes(self):
        queued = self.enqueue()
        original = self.stored(queued["id"])
        self.assertAlmostEqual(
            original["queue_expires_at"]
            - datetime.fromisoformat(original["created_at"]).timestamp(),
            30,
            places=2,
        )
        platform = Platform(self.db, replace(self.settings, queue_timeout=0.01))
        platform.gateway = self.platform.gateway
        await asyncio.sleep(0.02)
        worker = Worker(platform, gateway=OfflineInference())
        claimed = worker.claim()
        self.assertEqual(claimed["id"], queued["id"])
        self.assertEqual(claimed["queue_expires_at"], original["queue_expires_at"])

    async def test_legacy_queue_gets_frozen_before_execution(self):
        queued = self.enqueue()
        self.change_run(queued["id"], queue_expires_at=None)
        claimed = Worker(self.platform, gateway=OfflineInference()).claim()
        expected = (
            datetime.fromisoformat(claimed["created_at"]).timestamp()
            + self.settings.queue_timeout
        )
        self.assertEqual(claimed["queue_expires_at"], expected)
        self.assertEqual(self.stored(queued["id"])["queue_expires_at"], expected)

    async def test_delayed_executor_cannot_reset_execution_deadline(self):
        queued = self.enqueue()
        gateway = OfflineInference()
        short = Platform(self.db, replace(self.settings, run_timeout=0.05))
        short.gateway = self.platform.gateway
        worker = Worker(short, gateway=gateway)
        claimed = worker.claim()
        worker.settings = replace(self.settings, run_timeout=100)
        await asyncio.sleep(0.07)
        await worker.execute(claimed)
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(self.stored(queued["id"])["status"], "expired")

    async def test_renewal_and_recovery_enforce_execution_deadline(self):
        queued = self.enqueue()
        worker = Worker(self.platform, gateway=OfflineInference())
        claimed = worker.claim()
        deadline = time.time() + 1
        self.change_run(queued["id"], deadline=deadline)
        worker.renew(claimed)
        self.assertLessEqual(self.stored(queued["id"])["lease_until"], deadline)
        self.change_run(
            queued["id"], deadline=time.time() - 1, lease_until=time.time() + 20
        )
        with self.assertRaises(TimeoutError):
            worker.renew(claimed)
        replacement = Worker(self.platform, gateway=OfflineInference())
        replacement.recover()
        expired = self.stored(queued["id"])
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(expired["fence"], claimed["fence"] + 1)
        with self.assertRaises(PlatformError):
            worker.finish(claimed, "succeeded", "Stale result")

    async def test_inflight_timeout_retains_unknown_fee_and_closes_model(self):
        queued = self.enqueue()
        gateway = OfflineInference(hang=True)
        short = Platform(self.db, replace(self.settings, run_timeout=0.15))
        short.gateway = self.platform.gateway
        worker = Worker(short, gateway=gateway)
        await worker.execute(worker.claim())
        self.assertEqual(self.stored(queued["id"])["status"], "expired")
        self.assertTrue(gateway.closed.is_set())
        receipt = self.platform.billing.reservations(self.user["tenant_id"])[0]
        self.assertEqual(receipt["status"], "unresolved")
        self.assertGreater(
            Decimal(self.platform.billing.wallet(self.user["tenant_id"])["reserved"]), 0
        )

    async def test_unlimited_execution_renews_and_completes_after_old_time_limit(self):
        self.platform.settings = replace(self.settings, run_timeout=0)
        queued = self.enqueue()
        worker = Worker(self.platform, gateway=OfflineInference())
        claimed = worker.claim()
        self.assertIsNone(claimed["deadline"])
        self.assertIsNone(claimed["spec"]["execution_limits"]["timeout_seconds"])
        # Advance past the former 210-second deadline without waiting in real time.
        future = time.time() + 600
        self.change_run(queued["id"], lease_until=future + 1)
        with patch("agent_platform.apps.worker.main.time.time", return_value=future):
            worker.renew(claimed)
            worker.recover()
            self.assertFalse(worker.is_cancelled(claimed))
            self.assertEqual(self.stored(queued["id"])["status"], "running")
            self.assertEqual(self.stored(queued["id"])["lease_until"], future + 20)
        await asyncio.wait_for(worker.execute(claimed), 3)
        self.assertEqual(self.stored(queued["id"])["status"], "succeeded")

    async def test_unlimited_execution_still_cancels_and_detects_lost_workers(self):
        self.platform.settings = replace(self.settings, run_timeout=0)
        queued = self.enqueue()
        gateway = OfflineInference(hang=True)
        worker = Worker(self.platform, gateway=gateway)
        claimed = worker.claim()
        task = asyncio.create_task(worker.execute(claimed))
        try:
            await asyncio.wait_for(gateway.started.wait(), 3)
            await asyncio.to_thread(self.platform.cancel_run, self.user, queued["id"])
            await asyncio.wait_for(task, 3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(gateway.closed.is_set())
        self.assertEqual(self.stored(queued["id"])["status"], "cancelled")
        lost = self.enqueue()
        worker.claim()
        self.change_run(lost["id"], lease_until=time.time() - 1)
        worker.recover()
        self.assertEqual(self.stored(lost["id"])["status"], "failed")
        self.assertIsNone(worker.claim())

    async def test_unlimited_platform_keeps_an_explicit_agent_deadline(self):
        self.platform.settings = replace(self.settings, run_timeout=0)
        self.platform.save_agent(
            self.user,
            {"execution_policy": {"timeout_seconds": 2}},
            self.agents[0]["id"],
        )
        self.platform.publish(self.user, self.agents[0]["id"])
        self.enqueue()
        started = time.time()
        claimed = Worker(self.platform, gateway=OfflineInference()).claim()
        self.assertAlmostEqual(claimed["deadline"] - started, 2, delta=0.2)

    async def test_cancel_after_usage_preserves_settlement(self):
        queued = self.enqueue()

        async def cancel():
            await asyncio.to_thread(self.platform.cancel_run, self.user, queued["id"])

        gateway = OfflineInference(after_result=cancel)
        worker = Worker(self.platform, gateway=gateway)
        await worker.execute(worker.claim())
        self.assertEqual(self.stored(queued["id"])["status"], "cancelled")
        receipt = self.platform.billing.reservations(self.user["tenant_id"])[0]
        self.assertEqual(receipt["status"], "settled")
        self.assertEqual(receipt["input_tokens"], 20)

    async def test_expired_sse_lease_cannot_be_revived_or_cross_tenants(self):
        queued = self.enqueue()
        lease = self.platform.acquire_stream(self.user, queued["id"])
        with self.db.transaction(f"tenant:{self.user['tenant_id']}") as connection:
            connection.execute(
                update(rt.stream_leases)
                .where(rt.stream_leases.c.id == lease)
                .values(expires_at=time.time() - 1)
            )
        with self.assertRaises(PlatformError) as lost:
            self.platform.update_stream(self.user, lease)
        self.assertEqual(lost.exception.code, "stream_lease_lost")
        replacement = self.platform.acquire_stream(self.user, queued["id"])
        for context, identifier in ((self.user, lease), (self.other_user, replacement)):
            with self.assertRaises(PlatformError) as lost:
                self.platform.update_stream(context, identifier)
            self.assertEqual(lost.exception.code, "stream_lease_lost")
        self.platform.update_stream(self.user, replacement)
        with self.assertRaises(PlatformError) as full:
            self.platform.acquire_stream(self.user, queued["id"])
        self.assertEqual(full.exception.code, "stream_limit")

    async def test_blocked_sse_send_renews_then_stops_on_lost_lease(self):
        queued = self.enqueue()
        blocked, closed = asyncio.Event(), asyncio.Event()
        renewals = 0

        async def source():
            try:
                yield "data: first\n\n"
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                blocked.set()
                await asyncio.Event().wait()

        with patch("agent_platform.modules.platform.STREAM_LEASE_SECONDS", 0.2):
            valid_until = time.monotonic() + 0.2
            lease = self.platform.acquire_stream(self.user, queued["id"])

            async def renew():
                nonlocal renewals
                await asyncio.to_thread(self.platform.update_stream, self.user, lease)
                renewals += 1

            async def release():
                await asyncio.to_thread(
                    self.platform.update_stream, self.user, lease, release=True
                )

            response = LeasedStreamingResponse(
                source(),
                renew=renew,
                release=release,
                valid_until=valid_until,
                lease_seconds=0.2,
                renew_seconds=0.03,
            )
            task = asyncio.create_task(response.stream_response(send))
            await asyncio.wait_for(blocked.wait(), 1)
            await asyncio.sleep(0.25)
            self.assertGreaterEqual(renewals, 2)
            with self.assertRaises(PlatformError) as full:
                self.platform.acquire_stream(self.user, queued["id"])
            self.assertEqual(full.exception.code, "stream_limit")
            with self.db.transaction(f"tenant:{self.user['tenant_id']}") as connection:
                connection.execute(
                    delete(rt.stream_leases).where(rt.stream_leases.c.id == lease)
                )
            await asyncio.wait_for(task, 1)
            self.assertTrue(closed.is_set())
            self.platform.acquire_stream(self.user, queued["id"])

    async def test_stalled_renewal_cancels_send_at_last_confirmed_expiry(self):
        queued = self.enqueue()
        closed = asyncio.Event()
        lease = self.platform.acquire_stream(self.user, queued["id"])

        async def source():
            try:
                yield "data: blocked\n\n"
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def send(message):
            if message.get("body"):
                await asyncio.Event().wait()

        async def renew():
            await asyncio.Event().wait()

        async def release():
            await asyncio.to_thread(
                self.platform.update_stream, self.user, lease, release=True
            )

        response = LeasedStreamingResponse(
            source(),
            renew=renew,
            release=release,
            valid_until=time.monotonic() + 0.12,
            lease_seconds=0.12,
            renew_seconds=0.01,
        )
        await asyncio.wait_for(response.stream_response(send), 1)
        self.assertTrue(closed.is_set())
        self.platform.acquire_stream(self.user, queued["id"])

    async def test_terminal_sse_replays_all_pages_and_releases_quota(self):
        queued = self.enqueue()
        worker = Worker(self.platform, gateway=OfflineInference())
        claimed = worker.claim()
        with self.db.transaction(f"tenant:{self.user['tenant_id']}") as connection:
            connection.execute(
                insert(t.run_events),
                [
                    {
                        "run_id": queued["id"],
                        "tenant_id": self.user["tenant_id"],
                        "sequence": index,
                        "type": "message.delta",
                        "data": {"text": "x"},
                        "created_at": now(),
                    }
                    for index in range(2, 407)
                ],
            )
        worker.finish(claimed, "succeeded", "Complete")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://offline",
            cookies={COOKIE: self.login["token"]},
        ) as client:
            response = await client.get(
                f"/api/v2/tenants/{self.user['tenant_id']}/runs/{queued['id']}/events",
                headers={"Last-Event-ID": "1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count("id: "), 406)
        self.assertIn("event: run.completed", response.text)
        with self.db.read() as connection:
            self.assertEqual(
                list(connection.scalars(select(rt.stream_leases.c.id))), []
            )


class ExecutionDeadlineMigrationTests(unittest.TestCase):
    def test_new_database_migrates_with_unlimited_execution(self):
        with tempfile.TemporaryDirectory(
            prefix="unlimited-execution-migration-"
        ) as directory:
            url = f"sqlite:///{Path(directory) / 'platform.db'}"
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            with patch.dict(
                os.environ, {"PLATFORM_DATABASE_URL": url, "PLATFORM_RUN_TIMEOUT": "0"}
            ):
                command.upgrade(config, "head")

    def test_populated_upgrade_preserves_queue_and_execution_deadlines(self):
        with tempfile.TemporaryDirectory(prefix="execution-migration-") as directory:
            url = f"sqlite:///{Path(directory) / 'platform.db'}"
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            with patch.dict(
                os.environ, {"PLATFORM_DATABASE_URL": url, "PLATFORM_RUN_TIMEOUT": "42"}
            ):
                command.upgrade(config, "0007_support_access")
                app = create_app(
                    Settings(
                        database_url=url,
                        admin_email="migration@example.test",
                        admin_password="migration-test-password",
                    )
                )
                platform, db = app.state.platform, app.state.database
                try:
                    # Seed the historical schema without using today's Agent
                    # table (which now has additional policy columns).
                    platform._bootstrap_admin()
                    platform.identity.bootstrap()
                    with db.read() as connection:
                        principal = dict(
                            connection.execute(select(t.users)).mappings().one()
                        )
                    context = {
                        "id": principal["id"],
                        "tenant_id": principal["tenant_id"],
                    }
                    agent_id = uid()
                    session = {"id": uid()}
                    with db.transaction() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO platform_agents (id,tenant_id,created_at,name,description,system_prompt,model_id,builtin_key,temperature,max_steps,max_tokens,tools,published_version) VALUES (:id,:tenant,:stamp,'Historical','','Keep prompt',NULL,NULL,1,6,1024,'[]',1)"
                            ),
                            {
                                "id": agent_id,
                                "tenant": context["tenant_id"],
                                "stamp": now(),
                            },
                        )
                        connection.execute(
                            insert(t.sessions).values(
                                id=session["id"],
                                tenant_id=context["tenant_id"],
                                user_id=context["id"],
                                agent_id=agent_id,
                                title="Migration",
                                created_at=now(),
                            )
                        )
                    stamp = datetime.now(UTC).isoformat()
                    deadline = time.time() + 30
                    with db.transaction(f"tenant:{context['tenant_id']}") as connection:
                        connection.execute(
                            insert(t.runs).values(
                                id="historical-run",
                                tenant_id=context["tenant_id"],
                                user_id=context["id"],
                                created_at=stamp,
                                session_id=session["id"],
                                agent_name="Historical",
                                spec={},
                                message="Keep input",
                                status="running",
                                idempotency_key="historical-key",
                                request_hash="historical-hash",
                                owner="old-owner",
                                fence=7,
                                deadline=deadline,
                                lease_until=time.time() + 20,
                            )
                        )
                    command.upgrade(config, "0008_execution_deadlines")
                    command.upgrade(config, "0008_execution_deadlines")
                    with db.read() as connection:
                        run = connection.execute(select(t.runs)).mappings().one()
                        self.assertAlmostEqual(
                            run["queue_expires_at"],
                            datetime.fromisoformat(stamp).timestamp() + 42,
                            places=3,
                        )
                        self.assertEqual(
                            (
                                run["deadline"],
                                run["fence"],
                                run["owner"],
                                run["message"],
                            ),
                            (deadline, 7, "old-owner", "Keep input"),
                        )
                        self.assertEqual(
                            connection.exec_driver_sql(
                                "PRAGMA foreign_key_check"
                            ).all(),
                            [],
                        )
                finally:
                    db.close()
