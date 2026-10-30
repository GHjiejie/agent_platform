"""Real PostgreSQL release gate, restricted to an explicitly disposable database.

Set PLATFORM_ACCEPTANCE_POSTGRES_URL to a dedicated database named
agent_platform_acceptance (or agent_platform_acceptance_<suffix>) and set
PLATFORM_ACCEPTANCE_POSTGRES_ISOLATED=1. The URL must name an administrative test
identity able to create temporary roles/schemas. Never use a deployment URL.
Each test creates a random schema and non-owner service identities and removes
only those resources on completion. Missing configuration is a visible skip in
ordinary discovery; scripts.acceptance rejects this skip for release validation.
PLATFORM_ACCEPTANCE_DOCKER_ISOLATED=1 additionally permits a fresh PostgreSQL
container, validating real bootstrap credentials and frozen migration grants.
"""

import asyncio
import csv
import io
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import metering_tables as mt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import BillingService, reservations_table
from agent_platform.modules.gateway_control import GatewayService
from agent_platform.modules.health import platform_status, readiness, run_service
from agent_platform.modules.operations import OperationsService
from agent_platform.modules.platform import Platform, now
from agent_platform.modules.service_contexts import (
    maintenance_services,
    worker_services,
)
from agent_platform.modules.support import SupportService
from agent_platform.tests.fixtures import PASSWORD, MultiTenantFixture, migrate
from agent_platform.tests.test_gateway_convergence import ControlFixture

SERVICES = ("api", "worker", "gateway-sync", "maintenance")
BOOTSTRAP_IMAGE = (
    "postgres:17-alpine@sha256:"
    "b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24"
)


def isolated_postgres_url() -> str:
    raw = os.environ.get("PLATFORM_ACCEPTANCE_POSTGRES_URL", "")
    if not raw:
        raise unittest.SkipTest(
            "Real PostgreSQL unverified: configure a disposable acceptance database"
        )
    url = make_url(raw)
    connection_options = {
        "application_name",
        "connect_timeout",
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
    }
    if (
        url.drivername not in {"postgresql", "postgresql+psycopg"}
        or not re.fullmatch(
            r"agent_platform_acceptance(?:_[a-z0-9]+)?", url.database or ""
        )
        or os.environ.get("PLATFORM_ACCEPTANCE_POSTGRES_ISOLATED") != "1"
        or set(url.query) - connection_options
    ):
        raise ValueError(
            "PostgreSQL acceptance requires an explicitly isolated database named "
            "agent_platform_acceptance[_suffix] and POSTGRES_ISOLATED=1"
        )
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)


class PostgresSandbox:
    def __init__(self, url: str, *, focus_role="api"):
        if focus_role not in SERVICES:
            raise ValueError("Unsupported acceptance service role")
        self.focus_role = focus_role
        token = uuid4().hex[:16]
        self.schema = "apacc_" + token
        self.owner_name = self.schema + "_migrator"
        self.role_names = {
            service: self.schema + "_" + service.replace("-", "_")
            for service in (
                focus_role,
                *(role for role in SERVICES if role != focus_role),
            )
        }
        self.admin = create_engine(url, pool_pre_ping=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="pg-acceptance-")
        self.databases = []
        self.created_roles = []
        self.schema_created = False
        self.owner_db = None
        try:
            passwords = {
                name: secrets.token_urlsafe(24)
                for name in (self.owner_name, *self.role_names.values())
            }
            with self.admin.begin() as connection:
                major = int(connection.scalar(text("SHOW server_version_num"))) // 10000
                if major != 17:
                    raise RuntimeError("The release gate requires PostgreSQL 17")
                for name, password in passwords.items():
                    # Identifiers are generated locally; literal quotes handle secrets.
                    quoted_password = "'" + password.replace("'", "''") + "'"
                    connection.exec_driver_sql(
                        f'CREATE ROLE "{name}" LOGIN PASSWORD {quoted_password} '
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                    )
                    self.created_roles.append(name)
                connection.exec_driver_sql(
                    f'CREATE SCHEMA "{self.schema}" AUTHORIZATION "{self.owner_name}"'
                )
                self.schema_created = True
                connection.exec_driver_sql(
                    f'REVOKE ALL ON SCHEMA "{self.schema}" FROM PUBLIC'
                )
                for name in self.role_names.values():
                    connection.exec_driver_sql(
                        f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{name}"'
                    )
            owner_url = self._url(url, self.owner_name, passwords[self.owner_name])
            migrate(owner_url)
            self.owner_db = self.database(owner_url)
            self.fixture = MultiTenantFixture(
                self.owner_db,
                Settings(
                    database_url=owner_url,
                    admin_email="pg-owner@fixture.test",
                    admin_password="temporary-postgres-fixture-password",
                    secret_encryption_key=Fernet.generate_key().decode(),
                    export_directory=Path(self.temporary.name) / "exports",
                    tombstone_path=Path(self.temporary.name) / "tombstones.jsonl",
                    web_directory=Path(self.temporary.name) / "unused-web",
                ),
            )
            # Match the deployed service matrix instead of inventing broad test grants.
            from agent_platform.deploy.database_permissions import (
                privileges_for,
                render_grants,
            )

            self.privileges = {}
            with self.admin.begin() as connection:
                for service, name in self.role_names.items():
                    self.privileges[service] = privileges_for(service)
                    for statement in render_grants(
                        service,
                        schema=self.schema,
                        database=make_url(url).database,
                        role_name=name,
                    ).split(";"):
                        if statement.strip():
                            connection.exec_driver_sql(statement)
            self.runtime_urls = {
                service: self._url(url, name, passwords[name])
                for service, name in self.role_names.items()
            }
            self.db = self.database(self.runtime_urls["api"])
            self.platform = Platform(
                self.db,
                replace(self.fixture.settings, database_url=self.runtime_urls["api"]),
            )
        except BaseException:
            self.close()
            raise

    def _url(self, raw, username, password):
        return (
            make_url(raw)
            .set(username=username, password=password)
            .update_query_dict(
                {
                    "options": f"-csearch_path={self.schema},public "
                    "-clock_timeout=5000 -cstatement_timeout=15000"
                }
            )
            .render_as_string(hide_password=False)
        )

    def database(self, url):
        db = Database(url)
        self.databases.append(db)
        return db

    def close(self):
        for db in reversed(self.databases):
            db.close()
        try:
            with self.admin.begin() as connection:
                if self.schema_created:
                    connection.exec_driver_sql(
                        f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'
                    )
                for name in reversed(self.created_roles):
                    exists = connection.scalar(
                        text("SELECT 1 FROM pg_roles WHERE rolname=:role"),
                        {"role": name},
                    )
                    if exists:
                        connection.exec_driver_sql(f'DROP OWNED BY "{name}"')
                        connection.exec_driver_sql(f'DROP ROLE "{name}"')
        finally:
            self.admin.dispose()
            self.temporary.cleanup()


class PostgreSQLIsolationTests(unittest.TestCase):
    def setUp(self):
        url = isolated_postgres_url()
        self.sandbox = PostgresSandbox(
            url, focus_role=os.environ.get("PLATFORM_ACCEPTANCE_RUNTIME_ROLE", "api")
        )
        self.addCleanup(self.sandbox.close)
        self.fixture = self.sandbox.fixture
        self.db = self.sandbox.db
        self.platform = self.sandbox.platform
        self.tenant_a = self.fixture.tenant_id
        self.tenant_b = self.fixture.tenant_b
        self.session_a = self.fixture.create_session()
        self.session_b = self.fixture.create_session(
            self.fixture.context(self.fixture.owner["id"], self.tenant_b)
        )

    def test_collaboration_records_are_run_bound_and_rls_protected(self):
        from agent_platform.infrastructure import collaboration_tables as ct

        run = self.fixture.create_run(self.fixture.owner, self.session_a)
        other = self.fixture.create_run(
            self.fixture.context(self.fixture.owner["id"], self.tenant_b),
            self.session_b,
        )
        worker = self.sandbox.database(self.sandbox.runtime_urls["worker"])
        plan = {
            "id": uuid4().hex,
            "tenant_id": self.tenant_a,
            "root_run_id": run["id"],
            "created_at": now(),
            "revision": 1,
            "plan": {},
            "digest": "0" * 64,
        }
        with worker.transaction("tenant:" + self.tenant_a) as connection:
            connection.execute(insert(ct.plans).values(**plan))
        with self.db.tenant_scope(self.tenant_b), self.db.read() as connection:
            self.assertEqual(connection.execute(select(ct.plans)).all(), [])
        with self.db.read() as connection:
            self.assertEqual(connection.execute(select(ct.plans)).all(), [])
        with self.db.tenant_scope(self.tenant_a), self.db.read() as connection:
            self.assertEqual(connection.scalar(select(ct.plans.c.id)), plan["id"])
        with (
            self.assertRaises(IntegrityError),
            worker.transaction("tenant:" + self.tenant_a) as connection,
        ):
            connection.execute(
                insert(ct.plans).values(
                    **{**plan, "id": uuid4().hex, "root_run_id": other["id"]}
                )
            )
        with (
            self.assertRaises(DBAPIError),
            self.db.transaction("tenant:" + self.tenant_a) as connection,
        ):
            connection.execute(
                insert(ct.plans).values(**{**plan, "id": uuid4().hex, "revision": 2})
            )
        for table in ct.COLLABORATION_TABLES:
            with worker.transaction("tenant:" + self.tenant_b) as connection:
                self.assertEqual(connection.execute(select(table)).all(), [])

    def test_sequential_team_executes_with_deployed_service_roles(self):
        self._team_with_service_roles(durable=False)

    def test_durable_team_journal_uses_worker_only_permissions_and_rls(self):
        self._team_with_service_roles(durable=True)

    def test_distributed_team_uses_independent_worker_leases_and_rls(self):
        self._team_with_service_roles(durable=True, distributed=True)

    def test_remote_member_executes_across_workers_with_shared_financial_projections(
        self,
    ):
        self._team_with_service_roles(durable=True, distributed=True, remote=True)

    def _team_with_service_roles(self, *, durable, distributed=False, remote=False):
        from sqlalchemy import update

        from agent_platform.infrastructure import tenancy_tables as nt
        from agent_platform.modules.collaboration.queries import detail
        from agent_platform.modules.collaboration.templates import install
        from agent_platform.modules.runtime.gateway import ModelReply
        from agent_platform.tests.generative_ui_fixture import COUNTER_UI
        from agent_platform.tests.test_collaboration_runtime import ScriptedGateway

        control = ControlFixture()
        self.addCleanup(control.close)
        settings = replace(
            self.platform.settings,
            collaboration_enabled=True,
            collaboration_durable_enabled=durable,
            collaboration_distributed_enabled=distributed,
            remote_agents_enabled=remote,
            remote_agent_allowed_origins=("https://remote.example.test",),
            litellm_url=control.url,
            gateway_control_url=control.url,
            gateway_control_key="temporary-acceptance-control-key",
        )
        platform = Platform(self.db, settings)
        deployment = platform.gateway.create_deployment(
            {
                "name": "Team acceptance",
                "internal_route": "team-offline",
                "capabilities": {"text": True, "tools": True},
            },
            actor_id=self.fixture.owner["id"],
        )
        platform.gateway.grant_model(
            self.tenant_a,
            deployment["id"],
            {
                "name": "Team model",
                "alias": "team-model",
                "input_price": "1",
                "output_price": "2",
                "context_window": 32000,
                "max_output_tokens": 4096,
            },
            actor_id=self.fixture.owner["id"],
        )
        platform.gateway.enroll(self.tenant_a, actor_id=self.fixture.owner["id"])
        gateway_db = self.sandbox.database(self.sandbox.runtime_urls["gateway-sync"])
        self.assertTrue(GatewayService(gateway_db, settings).process_one())
        with self.db.transaction("tenant:" + self.tenant_a) as connection:
            connection.execute(
                update(nt.tenant_settings)
                .where(nt.tenant_settings.c.tenant_id == self.tenant_a)
                .values(collaboration_enabled=True)
            )
        with self.db.tenant_scope(self.tenant_a):
            installed = install(platform, self.fixture.owner)
            remote_transport = None
            if remote:
                from agent_platform.modules.collaboration.templates import DATA, TEXT
                from agent_platform.modules.costs import CostService
                from agent_platform.modules.remote_agents.registry import (
                    RemoteHealthQueue,
                    RemoteRegistry,
                )
                from agent_platform.tests.test_remote_agent_execution import (
                    TaskTransport,
                )
                from agent_platform.tests.test_remote_agent_registry import (
                    HealthTransport,
                )

                costs = CostService(self.db, authorize=lambda conn: None)
                costs.configure_budget(
                    "platform", "platform", "10", self.fixture.owner["id"]
                )
                costs.configure_price(
                    deployment["id"], "1", "2", self.fixture.owner["id"]
                )
                registry = RemoteRegistry(platform)
                credential = registry.create_credential(
                    self.fixture.owner,
                    {
                        "name": "Remote acceptance",
                        "token": "temporary-remote-test-secret",
                    },
                )
                registered = registry.register(
                    self.fixture.owner,
                    {
                        "idempotency_key": uuid4().hex,
                        "descriptor": {
                            "remote_agent_id": "remote",
                            "remote_version": "v1",
                            "name": "Remote normalize",
                            "task_contracts": [
                                {
                                    "task_type": "normalize",
                                    "input": TEXT,
                                    "output": DATA,
                                }
                            ],
                            "quote_id": "q1",
                            "max_charge": "0.02",
                            "idempotency": True,
                            "status_lookup": True,
                            "cancellation": True,
                        },
                        "endpoints": [
                            {
                                "url": "https://remote.example.test/agent",
                                "credential_id": credential["id"],
                            }
                        ],
                    },
                )
                probe_db = self.sandbox.database(self.sandbox.runtime_urls["worker"])
                probe_worker = Worker(worker_services(probe_db, settings))
                probes = RemoteHealthQueue(
                    probe_worker, transport=HealthTransport(registered)
                )
                asyncio.run(probes.execute(probes.claim()))
                with self.db.read() as conn:
                    definition = conn.scalar(
                        select(t.agents.c.collaboration_spec).where(
                            t.agents.c.id == installed["id"]
                        )
                    )
                definition["members"][0].update(
                    agent_id=registered["agent_id"], version=1
                )
                platform.save_agent(
                    self.fixture.owner,
                    {"collaboration_spec": definition},
                    installed["id"],
                )
                platform.publish(self.fixture.owner, installed["id"])
                remote_transport = TaskTransport("lost")
                remote_transport.result_value = {
                    "facts": [],
                    "missing": ["No source data"],
                }
            session = platform.create_session(
                self.fixture.owner, installed["id"], "Sequential team"
            )
            queued = platform.enqueue(
                self.fixture.owner,
                session["id"],
                "No business data supplied",
                uuid4().hex,
            )
        platform.billing.credit(
            self.tenant_a, self.fixture.owner["id"], "1", "Team acceptance", uuid4().hex
        )
        output = '{"facts":[],"missing":["No source data"]}'
        gateway = ScriptedGateway(
            [
                output,
                output,
                ModelReply(
                    "",
                    [{"id": "ui", "name": "generate_sandboxed_ui", "args": COUNTER_UI}],
                    20,
                    10,
                    {},
                ),
                output,
                "No business data supplied",
            ]
        )
        if remote:
            next(gateway.replies)
        worker_db = self.sandbox.database(self.sandbox.runtime_urls["worker"])
        worker = Worker(
            worker_services(worker_db, settings),
            gateway=gateway,
            remote_transport=remote_transport,
        )
        claimed = worker.claim()
        self.assertEqual(claimed["id"], queued["id"])
        if distributed:
            member_db = self.sandbox.database(self.sandbox.runtime_urls["worker"])
            member = Worker(
                worker_services(member_db, settings),
                gateway=gateway,
                remote_transport=remote_transport,
            )

            async def execute_distributed():
                parent = asyncio.create_task(worker.execute(claimed))
                async with asyncio.timeout(10):
                    while not parent.done():
                        child = await asyncio.to_thread(member.member_queue.claim)
                        if child:
                            await member.member_queue.execute(child)
                        await asyncio.sleep(0.01)
                await parent

            asyncio.run(execute_distributed())
            from agent_platform.infrastructure.task_lease_tables import leases

            with member_db.tenant_scope(self.tenant_a), member_db.read() as connection:
                children = connection.execute(select(leases)).mappings().all()
                self.assertEqual(len(children), 3)
                self.assertEqual({child["owner"] for child in children}, {member.owner})
                self.assertNotEqual(member.owner, worker.owner)
            with member_db.tenant_scope(self.tenant_b), member_db.read() as connection:
                self.assertEqual(connection.execute(select(leases)).all(), [])
        else:
            asyncio.run(worker.execute(claimed))
        result = detail(platform, self.fixture.owner, queued["id"])
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(
            [task["status"] for task in result["tasks"]], ["succeeded"] * 3
        )
        self.assertEqual(len(result["executions"]), 4)
        self.assertEqual(len(result["artifacts"]), 4)
        self.assertEqual(len(gateway.calls), 4 if remote else 5)
        self.assertGreater(Decimal(result["fees"]["confirmed"]), 0)
        from agent_platform.infrastructure.journal_tables import journal

        with worker_db.tenant_scope(self.tenant_a), worker_db.read() as connection:
            records = connection.execute(select(journal)).mappings().all()
        self.assertEqual(len(records), 6 if durable else 0)
        if durable:
            self.assertEqual({record["state"] for record in records}, {"confirmed"})
            self.assertEqual(
                {record["kind"] for record in records},
                {"tool", "model", "remote"} if remote else {"tool", "model"},
            )
            with worker_db.tenant_scope(self.tenant_b), worker_db.read() as connection:
                self.assertEqual(connection.execute(select(journal)).all(), [])
            for service in ("api", "gateway-sync"):
                restricted = self.sandbox.database(self.sandbox.runtime_urls[service])
                with (
                    restricted.tenant_scope(self.tenant_a),
                    self.assertRaises(DBAPIError),
                    restricted.read() as connection,
                ):
                    connection.execute(select(journal))

        if remote:
            from agent_platform.infrastructure.remote_call_tables import calls

            self.assertEqual(len(remote_transport.submissions), 1)
            with self.db.tenant_scope(self.tenant_a):
                usage = platform.call_records(self.fixture.owner, run_id=queued["id"])
                remote_rows = [row for row in usage if row["call_kind"] == "remote"]
                self.assertEqual(len(remote_rows), 1)
                self.assertEqual(Decimal(remote_rows[0]["cost"]), Decimal("0.01"))
                dashboard = platform.dashboard(self.fixture.owner)
                self.assertEqual(
                    Decimal(dashboard["cost"]), Decimal(result["fees"]["confirmed"])
                )
                api_exports = OperationsService(platform)
                export = api_exports.request_export(
                    self.fixture.owner, kind="calls", idempotency_key=uuid4().hex
                )
            maintenance_db = self.sandbox.database(
                self.sandbox.runtime_urls["maintenance"]
            )
            maintenance = OperationsService(
                maintenance_services(maintenance_db, settings)
            )
            outcome = maintenance.process_batch()
            self.assertEqual(outcome["errors"], 0)
            self.assertEqual(outcome["exports"], 1)
            with self.db.tenant_scope(self.tenant_a):
                content = b"".join(
                    api_exports.download(self.fixture.owner, export["id"])["chunks"]
                ).decode()
                exported = list(csv.DictReader(io.StringIO(content)))
                self.assertEqual(
                    {row["id"] for row in exported}, {row["id"] for row in usage}
                )
                self.assertEqual(
                    sum(Decimal(row["cost"]) for row in exported),
                    Decimal(result["fees"]["confirmed"]),
                )
                self.assertNotIn("temporary-remote", content)
                self.assertNotIn("remote.example.test", content)
            with worker_db.tenant_scope(self.tenant_b), worker_db.read() as conn:
                self.assertEqual(conn.execute(select(calls)).all(), [])

    def test_all_services_are_non_owner_and_all_tenant_tables_force_rls(self):
        for service, url in self.sandbox.runtime_urls.items():
            with self.subTest(service=service):
                db = self.sandbox.database(url)
                db.assert_schema(saas=True)
                with db.read() as connection:
                    role = connection.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                        )
                    ).one()
                    self.assertEqual(tuple(role), (False, False))
        with self.sandbox.owner_db.read() as connection:
            tables = connection.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=:schema AND c.relrowsecurity"
                ),
                {"schema": self.sandbox.schema},
            ).all()
        self.assertGreaterEqual(len(tables), 20)
        self.assertTrue(all(row[1] and row[2] for row in tables))
        with self.assertRaisesRegex(RuntimeError, "non-owner"):
            self.sandbox.owner_db.assert_schema(saas=True)

    def test_each_service_has_exact_deployed_table_privileges(self):
        with self.sandbox.admin.connect() as connection:
            table_names = connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"),
                {"schema": self.sandbox.schema},
            ).all()
            for service in SERVICES:
                for table_name in table_names:
                    expected = set(self.sandbox.privileges[service].get(table_name, ()))
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        actual = connection.scalar(
                            text(
                                "SELECT has_table_privilege(:role, :table, :privilege)"
                            ),
                            {
                                "role": self.sandbox.role_names[service],
                                "table": self.sandbox.schema + "." + table_name,
                                "privilege": privilege,
                            },
                        )
                        self.assertEqual(
                            actual,
                            privilege in expected,
                            (service, table_name, privilege),
                        )

    def test_financial_and_content_boundaries_hold_independently_of_matrix(self):
        # Assert policy independently: a mistaken matrix edit must fail acceptance.
        denied = {
            service: [
                "UPDATE billing_transactions SET id=id WHERE false",
                "DELETE FROM billing_transactions WHERE false",
            ]
            for service in SERVICES
        }
        denied["gateway-sync"].extend(
            [
                "SELECT * FROM billing_wallets LIMIT 1",
                "UPDATE billing_wallets SET balance=balance WHERE false",
                "SELECT * FROM platform_messages LIMIT 1",
                "SELECT * FROM platform_runs LIMIT 1",
            ]
        )
        denied["maintenance"].append(
            "UPDATE billing_wallets SET balance=balance WHERE false"
        )
        for service, statements in denied.items():
            db = self.sandbox.database(self.sandbox.runtime_urls[service])
            for statement in statements:
                with (
                    self.subTest(service=service, statement=statement),
                    self.assertRaises(DBAPIError) as caught,
                    db.transaction("tenant:" + self.tenant_a) as connection,
                ):
                    connection.exec_driver_sql(statement)
                self.assertEqual(caught.exception.orig.sqlstate, "42501")

    def test_service_roles_execute_gateway_worker_and_settlement_paths(self):
        control = ControlFixture()
        self.addCleanup(control.close)
        settings = replace(
            self.platform.settings,
            litellm_url=control.url,
            gateway_control_url=control.url,
            gateway_control_key="temporary-acceptance-control-key",
        )
        platform = Platform(self.db, settings)
        deployment = platform.gateway.create_deployment(
            {
                "name": "Acceptance deployment",
                "internal_route": "acceptance-offline-route",
                "capabilities": {"text": True, "tools": True},
            },
            actor_id=self.fixture.owner["id"],
        )
        model = platform.gateway.grant_model(
            self.tenant_a,
            deployment["id"],
            {
                "name": "Acceptance model",
                "alias": "acceptance-model",
                "input_price": "1",
                "output_price": "2",
                "context_window": 32000,
                "max_output_tokens": 256,
            },
            actor_id=self.fixture.owner["id"],
        )
        platform.gateway.enroll(self.tenant_a, actor_id=self.fixture.owner["id"])
        gateway_db = self.sandbox.database(self.sandbox.runtime_urls["gateway-sync"])
        gateway = GatewayService(gateway_db, settings)
        self.assertTrue(gateway.process_one())
        with self.db.tenant_scope(self.tenant_a):
            self.assertEqual(
                platform.gateway.resolve(self.tenant_a, model)["deployment_id"],
                deployment["id"],
            )
        queued = platform.enqueue(
            self.fixture.owner,
            self.session_a["id"],
            "Acceptance run",
            uuid4().hex,
            model_id=model["id"],
        )
        platform.billing.credit(
            self.tenant_a,
            self.fixture.owner["id"],
            "1",
            "Acceptance credit",
            uuid4().hex,
        )
        worker_db = self.sandbox.database(self.sandbox.runtime_urls["worker"])
        services = worker_services(worker_db, settings)
        worker = Worker(services)
        run = worker.claim()
        self.assertIsNotNone(run)
        self.assertEqual(run["id"], queued["id"])
        worker.event(run, "acceptance.probe", {"verified": True})
        call_id = uuid4().hex
        services.billing.reserve(
            self.tenant_a,
            self.fixture.owner["id"],
            model["id"],
            run["id"],
            call_id,
            20,
            5,
            "1",
            "2",
        )
        settled = services.billing.settle(
            self.tenant_a, call_id, 20, 5, raw_usage={"source": "gateway_stream"}
        )
        self.assertEqual(settled["status"], "settled")
        worker.finish(run, "succeeded", result="Acceptance result")
        with self.db.tenant_scope(self.tenant_a):
            self.assertEqual(
                platform.get_run(self.fixture.owner, run["id"])["status"], "succeeded"
            )

    def test_maintenance_role_generates_export_readable_by_api(self):
        run = self.fixture.create_run(self.fixture.owner, self.session_a)
        api = OperationsService(self.platform)
        job = api.request_export(
            self.fixture.owner, kind="runs", idempotency_key=uuid4().hex
        )
        db = self.sandbox.database(self.sandbox.runtime_urls["maintenance"])
        maintenance = OperationsService(
            maintenance_services(db, self.platform.settings)
        )
        outcome = maintenance.process_batch()
        self.assertEqual(outcome["errors"], 0)
        self.assertEqual(outcome["exports"], 1)
        exports = api.list_exports(self.fixture.owner)
        self.assertEqual(exports[0]["status"], "ready")
        download = api.download(self.fixture.owner, job["id"])
        content = b"".join(download["chunks"]).decode()
        self.assertIn(run["id"], content)
        self.assertNotIn("Fixture private content", content)

    def test_service_heartbeats_are_visible_to_api_and_stop_cleanly(self):
        # Aggregate background status uses SQL; this URL satisfies SaaS settings.
        settings = replace(
            self.platform.settings,
            mode="saas",
            secure_cookies=True,
            redis_url="redis://127.0.0.1:1/0",
        )
        databases = {
            role: self.sandbox.database(self.sandbox.runtime_urls[role])
            for role in ("worker", "gateway-sync", "maintenance")
        }

        async def check():
            stop = asyncio.Event()

            async def serve(stop):
                await stop.wait()

            tasks = [
                asyncio.create_task(run_service(db, role, serve, stop))
                for role, db in databases.items()
            ]
            try:
                for _ in range(100):
                    status = await asyncio.to_thread(platform_status, self.db, settings)
                    if status["status"] == "ready":
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(status["status"], "ready")
                for role, db in databases.items():
                    result = await asyncio.to_thread(readiness, db, settings, role=role)
                    self.assertEqual(result["status"], "ready")
            finally:
                stop.set()
                await asyncio.gather(*tasks)

        asyncio.run(check())
        self.assertEqual(platform_status(self.db, settings)["live_services"], [])

    def test_no_context_and_wrong_tenant_cannot_read_or_write_sessions(self):
        with self.db.read() as connection:
            self.assertEqual(connection.execute(select(t.sessions)).all(), [])
        with self.db.tenant_scope(self.tenant_b), self.db.read() as connection:
            self.assertEqual(
                connection.execute(
                    select(t.sessions).where(t.sessions.c.id == self.session_a["id"])
                ).all(),
                [],
            )
        for scope in ("acceptance:no-tenant", "tenant:" + self.tenant_b):
            with (
                self.subTest(scope=scope),
                self.assertRaises(DBAPIError) as caught,
                self.db.transaction(scope) as connection,
            ):
                connection.execute(
                    insert(t.sessions).values(
                        id=uuid4().hex,
                        tenant_id=self.tenant_a,
                        user_id=self.fixture.owner["id"],
                        agent_id=self.session_a["agent_id"],
                        title="Cross-tenant write must fail",
                        created_at=now(),
                    )
                )
            self.assertEqual(caught.exception.orig.sqlstate, "42501")

    def test_pool_context_is_cleared_after_reads_commits_and_rollback(self):
        # A new pool with one used connection proves cleanup on the same backend.
        db = self.sandbox.database(self.sandbox.runtime_urls["api"])
        with db.tenant_scope(self.tenant_a), db.read() as connection:
            backend_id = connection.scalar(text("SELECT pg_backend_pid()"))
            self.assertEqual(
                connection.scalar(select(t.sessions.c.id)), self.session_a["id"]
            )
        with db.read() as connection:
            self.assertEqual(
                connection.scalar(text("SELECT pg_backend_pid()")), backend_id
            )
            self.assertEqual(connection.execute(select(t.sessions)).all(), [])
        with db.transaction("tenant:" + self.tenant_b) as connection:
            self.assertEqual(
                connection.scalar(select(t.sessions.c.id)), self.session_b["id"]
            )
        with (
            self.assertRaisesRegex(RuntimeError, "deliberate rollback"),
            db.tenant_scope(self.tenant_a),
            db.transaction() as connection,
        ):
            self.assertEqual(
                connection.scalar(select(t.sessions.c.id)), self.session_a["id"]
            )
            raise RuntimeError("deliberate rollback")
        with db.read() as connection:
            self.assertEqual(
                connection.scalar(text("SELECT pg_backend_pid()")), backend_id
            )
            self.assertIn(
                connection.scalar(
                    text("SELECT current_setting('app.tenant_id', true)")
                ),
                (None, ""),
            )
            self.assertEqual(connection.execute(select(t.sessions)).all(), [])

    def test_composite_foreign_key_rejects_cross_tenant_resource_association(self):
        with (
            self.assertRaises(IntegrityError) as caught,
            self.db.transaction("tenant:" + self.tenant_a) as connection,
        ):
            connection.execute(
                insert(t.sessions).values(
                    id=uuid4().hex,
                    tenant_id=self.tenant_a,
                    user_id=self.fixture.owner["id"],
                    agent_id=self.session_b["agent_id"],
                    title="Invalid association",
                    created_at=now(),
                )
            )
        self.assertEqual(caught.exception.orig.sqlstate, "23503")

    def test_remote_registry_separates_api_secrets_and_worker_discovery(self):
        from agent_platform.infrastructure import remote_agent_tables as remote
        from agent_platform.modules.remote_agents.registry import (
            RemoteHealthQueue,
            RemoteRegistry,
        )
        from agent_platform.tests.test_remote_agent_registry import (
            HealthTransport,
            RemoteAgentRegistryTests,
        )

        settings = replace(
            self.platform.settings,
            remote_agents_enabled=True,
            remote_agent_allowed_origins=("https://remote.example.test",),
        )
        api = Platform(self.db, settings)
        registry = RemoteRegistry(api)
        credential = registry.create_credential(
            self.fixture.owner,
            {
                "name": "Acceptance remote",
                "token": "temporary-remote-pg-credential",
            },
        )
        registration = registry.register(
            self.fixture.owner, RemoteAgentRegistryTests.body(None, credential)
        )
        with (
            self.assertRaises(DBAPIError) as denied,
            self.db.tenant_scope(self.tenant_a),
            self.db.read() as conn,
        ):
            conn.execute(select(remote.secrets))
        self.assertEqual(denied.exception.orig.sqlstate, "42501")
        db = self.sandbox.database(self.sandbox.runtime_urls["worker"])
        worker = Worker(
            worker_services(
                db, replace(settings, database_url=self.sandbox.runtime_urls["worker"])
            )
        )
        worker_registry = RemoteRegistry(worker.platform)
        with db.tenant_scope(self.tenant_a), db.read() as conn:
            self.assertEqual(
                worker_registry.credential(conn, self.tenant_a, credential["id"]),
                "temporary-remote-pg-credential",
            )
        probes = RemoteHealthQueue(worker, transport=HealthTransport(registration))
        claim = probes.claim()
        self.assertIsNotNone(claim)
        asyncio.run(probes.execute(claim))
        with db.tenant_scope(self.tenant_a), db.read() as conn:
            version, endpoints = worker_registry.eligible(
                conn, self.tenant_a, {"id": registration["id"], "version": 1}
            )
            self.assertEqual(version["id"], registration["id"])
            self.assertEqual(len(endpoints), 1)
        with db.tenant_scope(self.tenant_b), db.read() as conn:
            self.assertEqual(conn.execute(select(remote.secrets)).all(), [])
            with self.assertRaises(PlatformError):
                worker_registry.credential(conn, self.tenant_b, credential["id"])
        other = registry.create_credential(
            self.fixture.context(self.fixture.owner["id"], self.tenant_b),
            {
                "name": "Other tenant",
                "token": "other-temporary-pg-credential",
            },
        )
        with (
            self.assertRaises(IntegrityError) as invalid,
            self.db.transaction("tenant:" + self.tenant_a) as conn,
        ):
            conn.execute(
                remote.endpoints.update()
                .where(remote.endpoints.c.id == endpoints[0]["id"])
                .values(credential_id=other["id"])
            )
        self.assertEqual(invalid.exception.orig.sqlstate, "23503")

    def test_remote_fixed_fees_use_worker_permissions_and_tenant_rls(self):
        from agent_platform.modules.costs import CostService

        CostService(self.db, authorize=lambda conn: None).configure_budget(
            "platform",
            "platform",
            "1",
            self.fixture.owner["id"],
        )
        self.platform.billing.credit(
            self.tenant_a,
            self.fixture.owner["id"],
            "2",
            "Acceptance credit",
            uuid4().hex,
        )
        worker = self.sandbox.database(self.sandbox.runtime_urls["worker"])
        billing = BillingService(worker)
        call_id = uuid4().hex
        billing.reserve_fixed(
            self.tenant_a,
            self.fixture.owner["id"],
            "remote-registration",
            "acceptance-run",
            call_id,
            quote_id="quote-1",
            max_charge="0.200000000001",
        )
        fee = {
            "receipt_id": "receipt-1",
            "request_id": call_id,
            "quote_id": "quote-1",
            "amount": "0.125000000001",
            "currency": "USD",
            "final": True,
        }
        for _ in range(2):
            result = billing.settle_fixed(self.tenant_a, call_id, fee)
            self.assertEqual(Decimal(result["cost"]), Decimal("0.125000000001"))
        self.assertEqual(
            Decimal(billing.wallet(self.tenant_a)["balance"]), Decimal("1.874999999999")
        )
        with worker.tenant_scope(self.tenant_a), worker.read() as conn:
            receipt = conn.execute(select(mt.usage_receipts)).mappings().one()
            self.assertEqual(receipt["source"], "remote_agent")
            hold = conn.execute(select(mt.cost_holds)).mappings().one()
            self.assertEqual(hold["charge_kind"], "fixed")
            self.assertIsNone(hold["deployment_id"])
        with worker.tenant_scope(self.tenant_b), worker.read() as conn:
            self.assertEqual(conn.execute(select(mt.usage_receipts)).all(), [])
            self.assertEqual(conn.execute(select(mt.cost_holds)).all(), [])
        with self.assertRaises(PlatformError):
            billing.settle_fixed(self.tenant_b, call_id, fee)

    def test_concurrent_wallet_admission_cannot_spend_same_balance_twice(self):
        billing = BillingService(self.db)
        billing.credit(
            self.tenant_a,
            self.fixture.owner["id"],
            "1",
            "Acceptance credit",
            uuid4().hex,
        )
        ready = Barrier(2)

        def reserve(call_id):
            ready.wait(timeout=5)
            try:
                return billing.reserve(
                    self.tenant_a,
                    self.fixture.member["id"],
                    "acceptance-model",
                    "acceptance-run",
                    call_id,
                    1,
                    0,
                    "1000000",
                    "0",
                )["status"]
            except PlatformError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reserve, uuid4().hex) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(outcomes, ["reserved", "insufficient_balance"])
        self.assertEqual(Decimal(billing.wallet(self.tenant_a)["reserved"]), Decimal(1))
        with self.db.tenant_scope(self.tenant_a), self.db.read() as connection:
            self.assertEqual(
                len(connection.execute(select(reservations_table)).all()), 1
            )

    def test_long_provider_request_ids_settle_distinctly_and_idempotently(self):
        billing = BillingService(
            self.sandbox.database(self.sandbox.runtime_urls["worker"])
        )
        self.platform.billing.credit(
            self.tenant_a,
            self.fixture.owner["id"],
            "10",
            "Acceptance credit",
            uuid4().hex,
        )
        model = self.fixture.create_model()
        run = self.fixture.create_run(self.fixture.owner, self.session_a)
        prefix = "provider-request-" + "x" * 211
        expected_ids = {}
        for key, suffix in (("gateway_response_id", "A"), ("gateway_request_id", "B")):
            call_id = uuid4().hex
            request_id = prefix + suffix
            expected_ids[call_id] = request_id
            evidence = {"usage_source": "litellm_gateway", key: request_id}
            billing.reserve(
                self.tenant_a,
                self.fixture.owner["id"],
                model["id"],
                run["id"],
                call_id,
                10,
                0,
                "100000",
                "0",
            )
            for _ in range(2):
                settled = billing.settle(
                    self.tenant_a, call_id, 10, 0, raw_usage=evidence
                )
                self.assertEqual(settled["status"], "settled")
        with self.db.tenant_scope(self.tenant_a), self.db.read() as connection:
            receipts = connection.execute(select(mt.usage_receipts)).mappings().all()
        self.assertEqual(len(receipts), 2)
        self.assertEqual(len({row["gateway_request_id"] for row in receipts}), 2)
        for row in receipts:
            self.assertLessEqual(len(row["gateway_request_id"]), 200)
            self.assertLessEqual(len(row["source_event_id"]), 200)
            self.assertIn(expected_ids[row["call_id"]], row["evidence"].values())
        wallet = self.platform.billing.wallet(self.tenant_a)
        self.assertEqual(Decimal(wallet["balance"]), Decimal(8))
        self.assertEqual(Decimal(wallet["reserved"]), Decimal(0))
        ledger = self.platform.billing.ledger(self.tenant_a)
        self.assertEqual(len(ledger), 3)

    def test_revocation_committed_before_admission_prevents_money_reservation(self):
        billing = BillingService(self.db)
        billing.credit(
            self.tenant_a,
            self.fixture.owner["id"],
            "1",
            "Acceptance credit",
            uuid4().hex,
        )
        stale_member = self.fixture.member
        revoked = Event()

        def revoke():
            self.platform.identity.update_member(
                self.fixture.owner,
                stale_member["membership_id"],
                {"status": "revoked"},
                expected_version=stale_member["membership_version"],
            )
            revoked.set()

        def admit():
            self.assertTrue(revoked.wait(timeout=5))
            checked = BillingService(
                self.db,
                authorize=lambda connection: self.platform.identity.assert_current(
                    connection, stale_member, "runs.execute"
                ),
            )
            with self.assertRaises(PlatformError):
                checked.reserve(
                    self.tenant_a,
                    stale_member["id"],
                    "model",
                    "run",
                    "revoked-call",
                    1,
                    0,
                    "1",
                    "0",
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(revoke), pool.submit(admit)]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(Decimal(billing.wallet(self.tenant_a)["reserved"]), Decimal(0))

    def test_support_owner_transfer_revokes_content_on_real_postgres(self):
        self.fixture.create_run(self.fixture.owner, self.session_a)
        support = SupportService(self.platform)
        grant = support.create(
            self.fixture.owner,
            staff_email=self.fixture.support["email"],
            reason="Real PostgreSQL support acceptance",
            allow_content=True,
        )
        self.assertEqual(
            support.session(self.fixture.support, grant["id"], self.session_a["id"])[
                "id"
            ],
            self.session_a["id"],
        )
        self.platform.identity.transfer_owner(
            self.fixture.owner, self.fixture.member["membership_id"]
        )
        with self.assertRaises(PlatformError) as caught:
            support.session(self.fixture.support, grant["id"], self.session_a["id"])
        self.assertEqual(caught.exception.code, "support_grant_inactive")
        self.assertEqual(support.my_grants(self.fixture.support), [])

    def test_support_reads_and_role_revocation_serialize_without_deadlock(self):
        self.fixture.create_run(self.fixture.owner, self.session_a)
        support = SupportService(self.platform)
        for iteration in range(3):
            if iteration:
                support.set_role(
                    self.fixture.principal,
                    self.fixture.support["id"],
                    role="platform_support",
                    active=True,
                    reason="Fresh acceptance support role",
                )
            staff = self.platform.identity.principal(self.fixture.support["id"])
            grant = support.create(
                self.fixture.owner,
                staff_email=staff["email"],
                reason="Concurrent acceptance grant",
                allow_content=True,
            )
            ready = Barrier(2)

            def read_content(ready, staff, grant):
                ready.wait(timeout=5)
                try:
                    support.session(staff, grant["id"], self.session_a["id"])
                    return "read_before_revocation"
                except PlatformError as exc:
                    return exc.code

            def revoke_role(ready, staff):
                ready.wait(timeout=5)
                support.set_role(
                    self.fixture.principal,
                    staff["id"],
                    role="platform_support",
                    active=False,
                    reason="Concurrent acceptance revocation",
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                read = pool.submit(read_content, ready, staff, grant)
                revoke = pool.submit(revoke_role, ready, staff)
                outcome = read.result(timeout=20)
                revoke.result(timeout=20)
            self.assertIn(
                outcome,
                (
                    "read_before_revocation",
                    "support_role_required",
                    "support_grant_inactive",
                ),
            )
            with self.assertRaises(PlatformError):
                support.session(staff, grant["id"], self.session_a["id"])


class PostgreSQLAcceptanceConfigurationTests(unittest.TestCase):
    def test_connection_options_cannot_redirect_to_a_deployment_database(self):
        for parameter in (
            "dbname=platform",
            "service=production",
            "options=-crole=postgres",
        ):
            with (
                self.subTest(parameter=parameter),
                patch.dict(
                    os.environ,
                    {
                        "PLATFORM_ACCEPTANCE_POSTGRES_URL": "postgresql://not-used@localhost/agent_platform_acceptance?"
                        + parameter,
                        "PLATFORM_ACCEPTANCE_POSTGRES_ISOLATED": "1",
                    },
                ),
                self.assertRaises(ValueError),
            ):
                isolated_postgres_url()

    def test_deployment_database_names_are_rejected_before_connecting(self):
        with (
            patch.dict(
                os.environ,
                {
                    "PLATFORM_ACCEPTANCE_POSTGRES_URL": "postgresql://not-used@localhost/platform",
                    "PLATFORM_ACCEPTANCE_POSTGRES_ISOLATED": "1",
                },
            ),
            self.assertRaisesRegex(ValueError, "isolated"),
        ):
            isolated_postgres_url()


class PostgreSQLBootstrapTests(unittest.TestCase):
    def test_actual_role_bootstrap_and_frozen_migration_grants(self):
        if os.environ.get("PLATFORM_ACCEPTANCE_DOCKER_ISOLATED") != "1":
            self.skipTest(
                "Fresh PostgreSQL role bootstrap unverified: opt into disposable Docker"
            )
        docker = shutil.which("docker")
        if not docker:
            self.skipTest(
                "Fresh PostgreSQL role bootstrap unverified: Docker unavailable"
            )
        from agent_platform.deploy.database_permissions import (
            ROLE_NAMES,
            ROLE_PRIVILEGES,
        )

        container = "agent-platform-bootstrap-" + uuid4().hex[:16]
        bootstrap = Path(__file__).resolve().parents[1] / "deploy/bootstrap-roles.sh"
        names = {
            "api": "PLATFORM_API_DB_PASSWORD",
            "worker": "PLATFORM_WORKER_DB_PASSWORD",
            "gateway-sync": "PLATFORM_GATEWAY_SYNC_DB_PASSWORD",
            "maintenance": "PLATFORM_MAINTENANCE_DB_PASSWORD",
        }
        credentials = {
            key: secrets.token_urlsafe(24)
            for key in (
                "POSTGRES_PASSWORD",
                "PLATFORM_MIGRATION_DB_PASSWORD",
                "LITELLM_DB_PASSWORD",
                *names.values(),
            )
        }
        self.addCleanup(
            subprocess.run,
            [docker, "rm", "--force", "--volumes", container],
            capture_output=True,
            check=False,
            timeout=30,
        )
        # Docker inherits these test-only values by name; no password is in argv.
        command = [
            docker,
            "run",
            "--detach",
            "--rm",
            "--name",
            container,
            "--publish",
            "127.0.0.1::5432",
            "--mount",
            f"type=bind,source={bootstrap},target=/docker-entrypoint-initdb.d/10-platform.sh,readonly",
        ]
        for key in credentials:
            command.extend(("--env", key))
        command.append(
            os.environ.get("PLATFORM_ACCEPTANCE_POSTGRES_IMAGE", BOOTSTRAP_IMAGE)
        )
        subprocess.run(
            command,
            env={**os.environ, **credentials},
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        port = subprocess.run(
            [docker, "port", container, "5432/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        self.assertRegex(port, r"^127\.0\.0\.1:\d+$")
        port = int(port.rsplit(":", 1)[1])

        def url(role, password, database="platform"):
            return URL.create(
                "postgresql+psycopg",
                username=role,
                password=password,
                host="127.0.0.1",
                port=port,
                database=database,
                query={"connect_timeout": "2"},
            ).render_as_string(hide_password=False)

        admin = create_engine(url("postgres", credentials["POSTGRES_PASSWORD"]))
        self.addCleanup(admin.dispose)
        deadline = time.monotonic() + 45
        while True:
            try:
                with admin.connect() as connection:
                    self.assertEqual(
                        int(connection.scalar(text("SHOW server_version_num")))
                        // 10000,
                        17,
                    )
                break
            except DBAPIError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Disposable PostgreSQL role bootstrap did not finish"
                    ) from None
                time.sleep(0.1)
        migrator_url = url(
            "platform_migrator", credentials["PLATFORM_MIGRATION_DB_PASSWORD"]
        )
        migrate(migrator_url)
        owner = Database(migrator_url)
        self.addCleanup(owner.close)
        temporary = tempfile.TemporaryDirectory(prefix="pg-bootstrap-acceptance-")
        self.addCleanup(temporary.cleanup)
        fixture = MultiTenantFixture(
            owner,
            Settings(
                database_url=migrator_url,
                admin_email="bootstrap-owner@fixture.test",
                admin_password=PASSWORD,
                export_directory=Path(temporary.name) / "exports",
                tombstone_path=Path(temporary.name) / "tombstones.jsonl",
            ),
        )
        session = fixture.create_session()
        self.assertEqual(len(set(credentials.values())), len(credentials))
        with admin.connect() as connection:
            table_names = connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            ).all()
            self.assertIsNone(
                connection.scalar(
                    text("SELECT 1 FROM pg_roles WHERE rolname='platform_runtime'")
                )
            )
            for service, role in ROLE_NAMES.items():
                for table_name in table_names:
                    expected = set(ROLE_PRIVILEGES[service].get(table_name, ()))
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        granted = connection.scalar(
                            text(
                                "SELECT has_table_privilege(:role, :table, :privilege)"
                            ),
                            {
                                "role": role,
                                "table": "public." + table_name,
                                "privilege": privilege,
                            },
                        )
                        self.assertEqual(
                            granted,
                            privilege in expected,
                            (service, table_name, privilege),
                        )
        for service, role in ROLE_NAMES.items():
            db = Database(url(role, credentials[names[service]]))
            self.addCleanup(db.close)
            db.assert_schema(saas=True)
            with (
                self.assertRaises(DBAPIError) as caught,
                db.transaction() as connection,
            ):
                connection.exec_driver_sql(
                    "UPDATE billing_transactions SET id=id WHERE false"
                )
            self.assertEqual(caught.exception.orig.sqlstate, "42501")
            if service == "gateway-sync":
                with self.assertRaises(DBAPIError) as caught, db.read() as connection:
                    connection.execute(select(t.messages))
                self.assertEqual(caught.exception.orig.sqlstate, "42501")
            if service == "api":
                with db.read() as connection:
                    self.assertEqual(connection.execute(select(t.sessions)).all(), [])
                with db.tenant_scope(fixture.tenant_id), db.read() as connection:
                    self.assertEqual(
                        connection.scalar(select(t.sessions.c.id)), session["id"]
                    )
        wrong_password = create_engine(
            url("platform_api", credentials[names["worker"]])
        )
        self.addCleanup(wrong_password.dispose)
        with self.assertRaises(DBAPIError) as caught, wrong_password.connect():
            pass
        # libpq connection failures do not expose server SQLSTATE through psycopg.
        self.assertIn("password authentication failed", str(caught.exception.orig))


if __name__ == "__main__":
    unittest.main()
