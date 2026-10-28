"""Durable cursor replay with commit wakeups and bounded notification failures."""

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, select, update

from agent_platform.apps.api.main import COOKIE, create_app
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.events import EventNotifier, configure_notifications
from agent_platform.modules.billing.service import BillingService
from agent_platform.modules.costs import CostService
from agent_platform.modules.operational_metrics import operational_snapshot
from agent_platform.modules.run_records import append_event
from agent_platform.tests.fixtures import MultiTenantFixture, migrate


async def eventually(predicate, timeout=2):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Condition did not become true")


class RequestFixture:
    def __init__(self, token, headers=None):
        self.cookies = {COOKIE: token}
        self.headers = headers or {}

    async def is_disconnected(self):
        return False


class EventNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="platform-notifications-")
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(
            database_url=f"sqlite:///{Path(self.directory.name) / 'platform.db'}",
            admin_email="operator@notifications.test",
            admin_password="temporary-notification-test-password",
            operator_emails=("operator@notifications.test",),
            litellm_url="http://127.0.0.1:1",
            export_directory=Path(self.directory.name) / "exports",
            web_directory=Path(self.directory.name) / "web",
        )
        migrate(self.settings.database_url)
        self.app = create_app(self.settings)
        self.db, self.platform = self.app.state.database, self.app.state.platform
        self.addCleanup(self.db.close)
        self.fixture = MultiTenantFixture(self.db, self.settings)
        self.actor = self.fixture.owner
        self.tenant = self.actor["tenant_id"]
        self.session = self.fixture.create_session(self.actor)
        self.run = self.fixture.create_run(self.actor, self.session)
        self.login = self.platform.login(
            self.settings.admin_email, self.settings.admin_password, "isolated"
        )
        self.notifier = configure_notifications(self.db)

    def authorize_metrics(self, connection):
        self.platform.require_platform(
            connection, self.actor, "platform.tenants.manage"
        )
        self.platform.require_platform(connection, self.actor, "platform.costs.read")

    def append(self, kind="message.delta", data=None):
        with self.db.transaction("tenant:" + self.tenant) as connection:
            return append_event(
                connection, self.run["id"], kind, data or {"text": "private delta"}
            )

    def active(self):
        with self.db.transaction("tenant:" + self.tenant) as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == self.run["id"])
                .values(status="running", finished_at=None)
            )

    async def response(self, after=0):
        route = next(
            route
            for route in self.app.routes
            if route.path == "/api/v1/runs/{run_id}/events"
        )
        return await route.endpoint(
            self.run["id"],
            RequestFixture(self.login["token"]),
            after=after,
            user=self.actor,
        )

    async def test_commit_coalesces_wakeups_and_rollback_never_notifies(self):
        async with self.notifier.watch(self.tenant, self.run["id"]) as watch:
            with self.db.transaction("tenant:" + self.tenant) as connection:
                append_event(
                    connection, self.run["id"], "message.delta", {"text": "one"}
                )
                append_event(
                    connection, self.run["id"], "message.delta", {"text": "two"}
                )
                self.assertEqual(self.notifier.stats()["committed_wakeups"], 0)
            self.assertTrue(await watch.wait(0, 0.1))
            self.assertEqual(self.notifier.stats()["committed_wakeups"], 1)
            observed = watch.version
            with (
                self.assertRaisesRegex(RuntimeError, "rollback"),
                self.db.transaction("tenant:" + self.tenant) as connection,
            ):
                append_event(
                    connection,
                    self.run["id"],
                    "message.delta",
                    {"text": "never committed"},
                )
                raise RuntimeError("rollback")
            self.assertFalse(await watch.wait(observed, 0.02))
            self.assertEqual(
                [
                    row["sequence"]
                    for row in self.platform.events(self.actor, self.run["id"], 0)
                ],
                [1, 2],
            )

    async def test_local_notification_is_scoped_and_read_wait_race_is_closed(self):
        async with (
            self.notifier.watch(self.tenant, self.run["id"]) as own,
            self.notifier.watch(self.fixture.tenant_b, self.run["id"]) as other,
        ):
            observed = own.version
            self.append()
            await asyncio.sleep(0)
            self.assertTrue(await own.wait(observed, 0.02))
            self.assertFalse(await other.wait(other.version, 0.02))
        self.assertEqual(self.notifier.stats()["watchers"], 0)

    async def test_notification_runs_after_commit_and_connection_release(self):
        observed = []

        def publish(*_args):
            def concurrent_writer():
                with self.db.transaction("tenant:" + self.tenant) as connection:
                    observed.append(connection.scalar(select(t.run_events.c.sequence)))

            thread = threading.Thread(target=concurrent_writer)
            thread.start()
            thread.join(timeout=0.5)
            self.assertFalse(
                thread.is_alive(), "notification still holds the DB write lock"
            )

        with patch.object(self.notifier, "publish", side_effect=publish):
            self.append()
        self.assertEqual(observed, [1])

    async def test_notification_outage_does_not_delay_deltas_and_retries_back_off(self):
        attempts = []

        def unavailable(*_args, **_kwargs):
            attempts.append(time.monotonic())
            raise RedisConnectionError("isolated injected failure")

        failing = EventNotifier(
            "redis://127.0.0.1:1",
            redis_factory=unavailable,
            retry_seconds=0.04,
            retry_max_seconds=0.2,
        )
        self.addCleanup(failing.close)
        self.db.event_notifier = failing
        started = time.monotonic()
        for _ in range(30):
            self.append()
        self.assertLess(time.monotonic() - started, 1)
        await asyncio.sleep(0.3)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertLess(len(attempts), 7)
        self.assertEqual(failing.stats()["committed_wakeups"], 30)
        self.assertEqual(failing.stats()["pending"], 1)
        self.assertEqual(len(self.platform.events(self.actor, self.run["id"], 0)), 30)

    async def test_lost_notification_still_replays_committed_final_event(self):
        self.active()
        response = await self.response()
        bodies = []

        async def send(message):
            if message.get("body"):
                bodies.append(message["body"].decode())

        task = asyncio.create_task(response.stream_response(send))
        await eventually(lambda: self.notifier.stats()["watchers"] == 1)
        await asyncio.sleep(0.05)
        with (
            patch.object(self.notifier, "publish"),
            self.db.transaction("tenant:" + self.tenant) as connection,
        ):
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == self.run["id"])
                .values(status="succeeded")
            )
            append_event(
                connection, self.run["id"], "run.completed", {"status": "succeeded"}
            )
        await asyncio.wait_for(task, 3)
        self.assertIn("event: run.completed", "".join(bodies))
        self.assertEqual(self.notifier.stats()["watchers"], 0)

    async def test_sse_last_event_id_and_after_replay_all_persisted_pages(self):
        with self.db.transaction("tenant:" + self.tenant) as connection:
            for index in range(405):
                append_event(
                    connection, self.run["id"], "message.delta", {"text": str(index)}
                )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://isolated",
            cookies={COOKIE: self.login["token"]},
        ) as client:
            response = await client.get(
                f"/api/v2/tenants/{self.tenant}/runs/{self.run['id']}/events?after=2",
                headers={"Last-Event-ID": "3"},
            )
        self.assertEqual(response.status_code, 200)
        ids = [
            int(line[4:])
            for line in response.text.splitlines()
            if line.startswith("id: ")
        ]
        self.assertEqual(ids, list(range(4, 406)))
        self.assertEqual(self.notifier.stats()["watchers"], 0)

    async def test_blocked_client_is_cancelled_when_session_is_revoked(self):
        self.active()
        self.append()
        response = await self.response()
        blocked = asyncio.Event()

        async def send(message):
            if message.get("body"):
                blocked.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(response.stream_response(send))
        await asyncio.wait_for(blocked.wait(), 1)
        with self.db.transaction("identity:test-revocation") as connection:
            connection.execute(
                delete(t.auth_sessions).where(
                    t.auth_sessions.c.user_id == self.actor["id"]
                )
            )
        revoked_at = time.monotonic()
        await asyncio.wait_for(task, 0.7)
        self.assertLess(time.monotonic() - revoked_at, 0.6)
        self.assertEqual(self.notifier.stats()["watchers"], 0)

    async def test_idle_stream_rechecks_membership_without_waiting_for_replay(self):
        self.active()
        response = await self.response()

        async def send(_message):
            pass

        task = asyncio.create_task(response.stream_response(send))
        await eventually(lambda: self.notifier.stats()["watchers"] == 1)
        with self.db.transaction("tenant:" + self.tenant) as connection:
            connection.execute(
                update(nt.memberships)
                .where(nt.memberships.c.id == self.actor["membership_id"])
                .values(
                    role="finance_viewer",
                    authz_version=nt.memberships.c.authz_version + 1,
                )
            )
        await asyncio.wait_for(task, 0.7)
        self.assertEqual(self.notifier.stats()["watchers"], 0)

    async def test_suspended_tenant_does_not_replay_with_readonly_context(self):
        self.append()
        with self.db.transaction("tenant:" + self.tenant) as connection:
            connection.execute(
                update(nt.tenant_settings)
                .where(nt.tenant_settings.c.tenant_id == self.tenant)
                .values(status="suspended")
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://isolated",
            cookies={COOKIE: self.login["token"]},
        ) as client:
            response = await client.get(
                f"/api/v2/tenants/{self.tenant}/runs/{self.run['id']}/events"
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private delta", response.text)
        self.assertEqual(self.notifier.stats()["watchers"], 0)

    async def test_operational_snapshot_is_tenant_scoped_and_contains_no_payload(self):
        self.active()
        with self.db.transaction("tenant:" + self.tenant) as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == self.run["id"])
                .values(status="queued")
            )
        own = operational_snapshot(
            self.db, self.tenant, authorize=self.authorize_metrics
        )
        other = operational_snapshot(
            self.db, self.fixture.tenant_b, authorize=self.authorize_metrics
        )
        self.assertEqual(own["queue"]["count"], 1)
        self.assertEqual(other["queue"]["count"], 0)
        self.assertEqual(own["unresolved"]["amount"], "0")
        self.assertIsNone(own["storage"]["advisory_waiters"])
        self.assertNotIn("Fixture private content", json.dumps(own))

    async def test_operational_snapshot_rechecks_operator_before_reading(self):
        with self.assertRaises(PlatformError):
            operational_snapshot(self.db, self.tenant)
        with self.db.transaction("identity:revoke-metrics") as connection:
            connection.execute(
                update(nt.platform_roles)
                .where(nt.platform_roles.c.user_id == self.actor["id"])
                .values(revoked_at="2026-01-01T00:00:00+00:00")
            )
        with self.assertRaises(PlatformError):
            operational_snapshot(self.db, self.tenant, authorize=self.authorize_metrics)

    async def test_operational_snapshot_preserves_exact_unknown_and_supplier_holds(
        self,
    ):
        model = self.fixture.create_model()
        costs = CostService(
            self.db,
            authorize=lambda connection: self.platform.identity._platform_actor(
                connection, self.actor, "platform.billing.manage"
            ),
        )
        costs.configure_price(
            model["deployment_id"], "333.333333333333", "0", self.actor["id"]
        )
        costs.configure_budget("platform", "platform", "10", self.actor["id"])
        billing = BillingService(self.db)
        billing.credit(
            self.tenant, self.actor["id"], "10", "isolated diagnostic funds", "credit"
        )
        billing.reserve(
            self.tenant,
            self.actor["id"],
            model["id"],
            self.run["id"],
            "unknown",
            3,
            0,
            "333.333333333333",
            "0",
            deployment_id=model["deployment_id"],
            require_cost_policy=True,
        )
        billing.unresolved(self.tenant, "unknown", "isolated provider timeout")
        own = operational_snapshot(
            self.db, self.tenant, authorize=self.authorize_metrics
        )
        other = operational_snapshot(
            self.db, self.fixture.tenant_b, authorize=self.authorize_metrics
        )
        self.assertEqual(own["unresolved"]["amount"], "0.001000000000")
        self.assertEqual(own["supplier_frozen"]["amount"], "0.001000000000")
        self.assertEqual(own["supplier_frozen"]["count"], 1)
        self.assertGreaterEqual(own["unresolved"]["oldest_age_seconds"], 0)
        self.assertEqual(other["supplier_frozen"]["amount"], "0")


class RedisEventNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.url = os.getenv("PLATFORM_ACCEPTANCE_REDIS_URL")
        if not self.url:
            self.skipTest("Explicit isolated acceptance Redis is not configured")
        if os.getenv("PLATFORM_ACCEPTANCE_REDIS_ISOLATED") != "1":
            raise RuntimeError("Acceptance Redis must be explicitly marked isolated")
        self.namespace = str(time.time_ns())

    async def test_independent_notifiers_receive_real_redis_wakeups(self):
        publisher = EventNotifier(self.url, namespace=self.namespace)
        subscriber = EventNotifier(self.url, namespace=self.namespace)
        self.addCleanup(publisher.close)
        self.addCleanup(subscriber.close)
        await eventually(
            lambda: (
                subscriber.stats()["redis_connected"]
                and publisher.stats()["redis_connected"]
            )
        )
        async with subscriber.watch("tenant", "run") as watch:
            await asyncio.sleep(0.06)
            observed = watch.version
            publisher.publish("tenant", "run", 12)
            self.assertTrue(await watch.wait(observed, 1))
            self.assertGreaterEqual(subscriber.stats()["redis_received"], 1)
            self.assertGreaterEqual(publisher.stats()["redis_published"], 1)

    async def test_real_redis_reconnects_after_injected_connection_loss(self):
        unavailable = threading.Event()

        class ConnectionProxy:
            def __init__(self, client):
                self.client = client

            def pubsub(self, **kwargs):
                wrapped = self.client.pubsub(**kwargs)

                class SubscriberProxy:
                    def subscribe(self, channel):
                        return wrapped.subscribe(channel)

                    def get_message(self, **kwargs):
                        if unavailable.is_set():
                            raise RedisConnectionError(
                                "injected connected socket failure"
                            )
                        return wrapped.get_message(**kwargs)

                    def close(self):
                        wrapped.close()

                return SubscriberProxy()

            def publish(self, *args):
                if unavailable.is_set():
                    raise RedisConnectionError("injected publish socket failure")
                return self.client.publish(*args)

            def close(self):
                self.client.close()

        def factory(*args, **kwargs):
            if unavailable.is_set():
                raise RedisConnectionError("injected reconnectable failure")
            return ConnectionProxy(Redis.from_url(*args, **kwargs))

        publisher = EventNotifier(
            self.url,
            namespace=self.namespace,
            redis_factory=factory,
            retry_seconds=0.02,
            retry_max_seconds=0.1,
        )
        subscriber = EventNotifier(self.url, namespace=self.namespace)
        self.addCleanup(publisher.close)
        self.addCleanup(subscriber.close)
        await eventually(
            lambda: (
                publisher.stats()["redis_connected"]
                and subscriber.stats()["redis_connected"]
            )
        )
        unavailable.set()
        await eventually(
            lambda: (
                publisher.stats()["redis_failures"] >= 2
                and not publisher.stats()["redis_connected"]
            )
        )
        async with subscriber.watch("tenant", "run") as watch:
            await asyncio.sleep(0.06)
            observed = watch.version
            publisher.publish("tenant", "run", 1)
            unavailable.clear()
            self.assertTrue(await watch.wait(observed, 1))
            self.assertGreaterEqual(publisher.stats()["redis_failures"], 2)
            self.assertTrue(publisher.stats()["redis_connected"])


if __name__ == "__main__":
    unittest.main()
