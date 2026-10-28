"""Real isolated SQL and HTTP control-plane convergence, without paid model I/O."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from sqlalchemy import select

from agent_platform.infrastructure import gateway_tables as g
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.gateway_control import (
    ControlFailure,
    GatewayService,
)


class ControlFixture:
    """Exercise the actual HTTP adapter against a private controllable gateway."""

    def __init__(self):
        self.users, self.keys, self.creates, self.deletes = {}, {}, [], []
        self.unknown_create = False
        self.hide_keys = False
        self.on_create_key = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, status, result):
                content = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                user_id = parse_qs(urlsplit(self.path).query)["user_id"][0]
                user = fixture.users.get(user_id)
                self.respond(200 if user else 404, {"user_info": user})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/user/new":
                    fixture.users[body["user_id"]] = body
                    self.respond(200, {})
                elif self.path == "/v2/key/info":
                    info = (
                        None if fixture.hide_keys else fixture.keys.get(body["keys"][0])
                    )
                    self.respond(200, {"info": [info] if info else []})
                elif self.path == "/key/generate":
                    identifier = hashlib.sha256(body["key"].encode()).hexdigest()
                    fixture.creates.append(identifier)
                    fixture.keys[identifier] = {
                        **body,
                        "expires": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                        "spend": 0,
                    }
                    if fixture.on_create_key:
                        callback, fixture.on_create_key = fixture.on_create_key, None
                        callback()
                    self.respond(
                        504 if fixture.unknown_create else 200,
                        {} if fixture.unknown_create else {"key": body["key"]},
                    )
                elif self.path == "/key/delete":
                    for identifier in body["keys"]:
                        fixture.keys.pop(identifier, None)
                        fixture.deletes.append(identifier)
                    self.respond(200, {})
                else:
                    self.respond(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class GatewayConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gateway-convergence-")
        self.control = ControlFixture()
        self.settings = Settings(
            database_url=f"sqlite:///{Path(self.temp.name) / 'platform.db'}",
            litellm_url=self.control.url,
            gateway_control_url=self.control.url,
            gateway_control_key="isolated-control-master-key",
            secret_encryption_key=Fernet.generate_key().decode(),
            model_timeout=0,
        )
        self.db = Database(self.settings.database_url)
        self.db.create_schema()
        stamp = datetime.now(UTC).isoformat()
        with self.db.transaction() as connection:
            for tenant in ("tenant-a", "tenant-b"):
                connection.execute(
                    t.tenants.insert().values(id=tenant, name=tenant, created_at=stamp)
                )
                connection.execute(
                    nt.tenant_settings.insert().values(
                        tenant_id=tenant,
                        status="active",
                        version=1,
                        ordered_model_ids=[],
                        owner_unavailable=False,
                        suspension_reason="",
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
                connection.execute(
                    nt.entitlements.insert().values(
                        tenant_id=tenant,
                        rpm=120,
                        tpm=200000,
                        max_concurrent_runs=10,
                        version=1,
                        updated_at=stamp,
                    )
                )
            connection.execute(
                t.users.insert().values(
                    id="operator",
                    tenant_id="tenant-a",
                    email="operator@example.test",
                    name="Operator",
                    password_hash="unused",
                    role="admin",
                    active=True,
                    global_status="active",
                    auth_version=1,
                    created_at=stamp,
                )
            )
            connection.execute(
                nt.platform_roles.insert().values(
                    user_id="operator",
                    role="platform_admin",
                    granted_at=stamp,
                )
            )
            connection.execute(
                nt.memberships.insert().values(
                    id="member-a",
                    tenant_id="tenant-a",
                    user_id="operator",
                    role="owner",
                    status="active",
                    authz_version=1,
                    joined_at=stamp,
                )
            )
        self.gateway = GatewayService(self.db, self.settings)
        self.actor = {"id": "operator", "tenant_id": "tenant-a"}
        self.deployment = self.gateway.create_deployment(
            {"internal_route": "route-a"}, actor_id="operator"
        )
        self.model = self.grant(self.deployment)

    def tearDown(self):
        self.control.close()
        self.db.close()
        self.temp.cleanup()

    def grant(self, deployment, tenant="tenant-a", **values):
        return self.gateway.grant_model(
            tenant, deployment["id"], values, actor_id="operator"
        )

    def enroll(self, tenant="tenant-a"):
        self.gateway.enroll(tenant, actor_id="operator")
        self.assertTrue(self.gateway.process_one())
        self.assertEqual(self.gateway.status(tenant)["status"], "ready")

    def deployment_b(self):
        return self.gateway.create_deployment(
            {"internal_route": "route-b"}, actor_id="operator"
        )

    def rows(self, table):
        with self.db.read() as connection:
            return [dict(row) for row in connection.execute(select(table)).mappings()]

    def active(self, tenant="tenant-a"):
        return next(
            row
            for row in self.rows(g.credentials)
            if row["tenant_id"] == tenant and row["status"] == "active"
        )

    def test_runtime_credentials_use_responses_routes_and_reject_chat_permissions(
        self,
    ):
        self.assertEqual(self.deployment["protocol"], "responses")
        self.enroll()
        credential = self.active()
        issued = self.control.keys[credential["external_key_id"]]
        self.assertEqual(issued["allowed_routes"], ["/responses", "/v1/responses"])
        old_permissions = {
            **issued,
            "allowed_routes": ["/chat/completions", "/v1/chat/completions"],
        }
        with self.assertRaisesRegex(ControlFailure, "gateway_key_permissions_mismatch"):
            GatewayService._validate_key(old_permissions, credential)

    def test_grants_do_not_implicitly_enroll_and_rotation_keeps_existing_route_usable(
        self,
    ):
        self.assertEqual(self.rows(g.outbox), [])
        self.enroll()
        original = self.active()
        extra = self.grant(self.deployment_b())
        status = self.gateway.status("tenant-a")
        self.assertEqual((status["desired_version"], status["applied_version"]), (2, 1))
        self.assertTrue(self.gateway.availability("tenant-a", self.model)["ready"])
        self.assertFalse(self.gateway.availability("tenant-a", extra)["ready"])
        self.assertTrue(self.gateway.process_one())
        self.assertTrue(self.gateway.availability("tenant-a", extra)["ready"])
        self.assertEqual(self.active()["routes"], ["route-a", "route-b"])
        self.assertIn(
            original["id"],
            [
                row["id"]
                for row in self.rows(g.credentials)
                if row["status"] == "retiring"
            ],
        )
        self.assertNotIn(original["external_key_id"], self.control.deletes)

    def test_unissued_snapshots_are_coalesced_and_explicit_limits_are_not_lost(self):
        self.gateway.enroll("tenant-a", actor_id="operator")
        self.grant(self.deployment_b())
        self.gateway.enroll("tenant-a", actor_id="operator", limits={"rpm_limit": 7})
        self.assertEqual(self.gateway.status("tenant-a")["desired_version"], 3)
        self.assertTrue(self.gateway.process_one())
        self.assertEqual(len(self.control.creates), 1)
        self.assertEqual(self.active()["limits"]["rpm_limit"], 7)
        self.assertEqual(self.active()["routes"], ["route-a", "route-b"])
        self.assertEqual(
            len([row for row in self.rows(g.outbox) if row["status"] == "cancelled"]), 2
        )

    def test_policy_change_during_remote_create_cannot_activate_stale_snapshot(self):
        extra_deployment = self.deployment_b()
        self.gateway.enroll("tenant-a", actor_id="operator")
        self.control.on_create_key = lambda: self.grant(extra_deployment)
        self.assertTrue(self.gateway.process_one())
        self.assertFalse(
            any(row["status"] == "active" for row in self.rows(g.credentials))
        )
        self.assertEqual(self.gateway.status("tenant-a")["desired_version"], 2)
        self.assertTrue(self.gateway.process_one())
        self.assertEqual(self.active()["generation"], 2)
        self.assertEqual(self.active()["routes"], ["route-a", "route-b"])

    def test_unknown_create_is_read_back_once_before_newer_configuration(self):
        self.gateway.enroll("tenant-a", actor_id="operator")
        self.control.unknown_create, self.control.hide_keys = True, True
        self.assertTrue(self.gateway.process_one())
        unknown = next(
            row for row in self.rows(g.outbox) if row["status"] == "reconciling"
        )
        self.grant(self.deployment_b())
        self.assertFalse(self.gateway.process_one())
        self.assertEqual(len(self.control.creates), 1)
        self.control.unknown_create, self.control.hide_keys = False, False
        self.gateway.reconcile("tenant-a", unknown["id"], actor_id="operator")
        self.assertTrue(self.gateway.process_one())
        self.assertEqual(len(self.control.creates), 1)
        self.assertTrue(self.gateway.process_one())
        self.assertEqual(len(self.control.creates), 2)
        self.assertEqual(self.active()["routes"], ["route-a", "route-b"])

    def test_degraded_sync_does_not_hide_old_verified_route(self):
        self.enroll()
        extra = self.grant(self.deployment_b())
        self.control.unknown_create = True
        self.assertTrue(self.gateway.process_one())
        self.assertEqual(self.gateway.status("tenant-a")["status"], "degraded")
        self.assertTrue(self.gateway.availability("tenant-a", self.model)["ready"])
        self.assertFalse(self.gateway.availability("tenant-a", extra)["ready"])

    def test_absent_unknown_key_is_never_created_again(self):
        self.gateway.enroll("tenant-a", actor_id="operator")
        self.control.unknown_create, self.control.hide_keys = True, True
        self.assertTrue(self.gateway.process_one())
        job = next(row for row in self.rows(g.outbox) if row["status"] == "reconciling")
        for _ in range(2):
            self.gateway.reconcile("tenant-a", job["id"], actor_id="operator")
            self.assertTrue(self.gateway.process_one())
        self.assertEqual(len(self.control.creates), 1)
        self.assertEqual(
            self.gateway.status("tenant-a")["operation_counts"]["reconciling"], 1
        )

    def test_activation_rechecks_policy_even_if_hook_was_missed(self):
        self.gateway.enroll("tenant-a", actor_id="operator")

        def change_plan():
            with self.db.transaction("tenant:tenant-a") as connection:
                connection.execute(
                    nt.entitlements.update()
                    .where(nt.entitlements.c.tenant_id == "tenant-a")
                    .values(rpm=2)
                )

        self.control.on_create_key = change_plan
        self.assertTrue(self.gateway.process_one())
        self.assertFalse(
            any(row["status"] == "active" for row in self.rows(g.credentials))
        )
        self.assertEqual(self.gateway.status("tenant-a")["desired_version"], 2)
        self.assertTrue(self.gateway.process_one())
        self.assertTrue(self.gateway.process_one())
        self.assertEqual(self.active()["limits"]["rpm_limit"], 2)

    def test_disabling_one_model_preserves_other_route_until_replacement_verified(self):
        extra = self.grant(self.deployment_b())
        self.enroll()
        self.gateway.set_model_enabled(
            "tenant-a", self.model["id"], False, actor=self.actor
        )
        self.assertFalse(self.gateway.availability("tenant-a", self.model)["ready"])
        self.assertTrue(self.gateway.availability("tenant-a", extra)["ready"])
        self.assertTrue(self.gateway.process_one())
        self.assertEqual(self.active()["routes"], ["route-b"])

    def test_configuration_and_outbox_roll_back_with_authorized_grant(self):
        self.enroll()
        before = self.gateway.status("tenant-a")["desired_version"]
        extra = self.deployment_b()
        original_audit = self.gateway._audit

        def fail_convergence(*args, **kwargs):
            if args[3] == "gateway.configuration_requested":
                raise RuntimeError("isolated audit failure")
            return original_audit(*args, **kwargs)

        with (
            patch.object(self.gateway, "_audit", side_effect=fail_convergence),
            self.assertRaises(RuntimeError),
        ):
            self.grant(extra)
        self.assertEqual(self.gateway.status("tenant-a")["desired_version"], before)
        self.assertEqual(len(self.rows(t.models)), 1)
        self.assertEqual(len(self.rows(g.outbox)), 1)

    def test_disabling_last_route_revokes_key_and_enabling_converges_again(self):
        self.enroll()
        original = self.active()
        self.gateway.set_model_enabled(
            "tenant-a", self.model["id"], False, actor=self.actor
        )
        self.assertEqual(self.gateway.status("tenant-a")["status"], "disabled")
        self.assertTrue(self.gateway.process_one())
        self.assertIn(original["external_key_id"], self.control.deletes)
        self.assertEqual(self.gateway.status("tenant-a")["status"], "disabled")
        self.gateway.set_model_enabled(
            "tenant-a", self.model["id"], True, actor=self.actor
        )
        self.assertTrue(self.gateway.process_one())
        self.assertTrue(self.gateway.availability("tenant-a", self.model)["ready"])

    def test_plan_lowering_blocks_old_limits_until_new_key_is_verified(self):
        self.enroll()
        with self.db.transaction("tenant:tenant-a") as connection:
            connection.execute(
                nt.entitlements.update()
                .where(nt.entitlements.c.tenant_id == "tenant-a")
                .values(rpm=3, tpm=900, max_concurrent_runs=1, max_budget="2")
            )
            self.gateway.converge_configuration(connection, "tenant-a")
        self.assertFalse(self.gateway.availability("tenant-a", self.model)["ready"])
        self.assertTrue(self.gateway.process_one())
        self.assertEqual(
            self.active()["limits"],
            {
                "rpm_limit": 3,
                "tpm_limit": 900,
                "max_parallel_requests": 1,
                "max_budget": 2.0,
            },
        )
        self.assertTrue(self.gateway.availability("tenant-a", self.model)["ready"])

    def test_shared_deployment_update_converges_all_tenants_atomically(self):
        self.grant(self.deployment, tenant="tenant-b")
        self.enroll("tenant-a")
        self.enroll("tenant-b")
        changed = self.gateway.update_deployment(
            self.deployment["id"], {"internal_route": "new-route"}, actor_id="operator"
        )
        self.assertEqual(changed["config_version"], 2)
        self.assertEqual(
            [
                self.gateway.status(tenant)["desired_version"]
                for tenant in ("tenant-a", "tenant-b")
            ],
            [2, 2],
        )
        self.assertFalse(self.gateway.availability("tenant-a", self.model)["ready"])
        self.assertTrue(self.gateway.process_one())
        self.assertTrue(self.gateway.process_one())
        self.assertEqual(
            [self.active(tenant)["routes"] for tenant in ("tenant-a", "tenant-b")],
            [["new-route"], ["new-route"]],
        )

    def test_revoked_binding_is_not_reopened_by_policy_hook(self):
        self.enroll()
        self.gateway.revoke("tenant-a", actor_id="operator")
        self.assertTrue(self.gateway.process_one())
        self.grant(self.deployment_b())
        self.assertEqual(self.gateway.status("tenant-a")["status"], "revoked")
        self.assertFalse(self.gateway.process_one())

    def test_stale_platform_permission_cannot_change_global_deployment(self):
        with self.db.transaction() as connection:
            connection.execute(
                nt.platform_roles.update()
                .where(nt.platform_roles.c.user_id == "operator")
                .values(revoked_at=datetime.now(UTC).isoformat())
            )
        with self.assertRaises(PlatformError):
            self.gateway.update_deployment(
                self.deployment["id"],
                {"internal_route": "forbidden"},
                actor_id="operator",
            )
        self.assertEqual(self.rows(g.deployments)[0]["internal_route"], "route-a")

    def test_master_credential_never_enters_persisted_records_or_status(self):
        self.enroll()
        self.grant(self.deployment_b())
        persisted = json.dumps(
            {table.name: self.rows(table) for table in g.TABLES}, default=str
        )
        self.assertNotIn(self.settings.gateway_control_key, persisted)
        self.assertNotIn(
            self.settings.gateway_control_key,
            json.dumps(self.gateway.status("tenant-a")),
        )


if __name__ == "__main__":
    unittest.main()
